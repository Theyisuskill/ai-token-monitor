// AI Token Monitor — GNOME Shell extension (GNOME 45+, ESM)
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Thin D-Bus client: all heavy lifting (inotify, parsing, SQLite) lives in
// the ai-token-monitor daemon. Creating the proxy D-Bus-activates it.

import GObject from 'gi://GObject';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const BUS_NAME = 'io.github.theyisuskill.AITokenMonitor';
const OBJECT_PATH = '/io/github/theyisuskill/AITokenMonitor';

const MONITOR_IFACE = `
<node>
  <interface name="io.github.theyisuskill.AITokenMonitor1">
    <method name="GetSummary">
      <arg type="s" direction="in" name="period"/>
      <arg type="s" direction="out" name="summary"/>
    </method>
    <method name="GetSnapshot">
      <arg type="s" direction="out" name="snapshot"/>
    </method>
    <method name="Refresh">
      <arg type="s" direction="out" name="snapshot"/>
    </method>
    <signal name="UsageUpdated">
      <arg type="s" name="snapshot"/>
    </signal>
  </interface>
</node>`;

const MonitorProxy = Gio.DBusProxy.makeProxyWrapper(MONITOR_IFACE);

// Fallback poll, in case a signal is missed (daemon restart, race at login).
// Opening the menu always forces a fresh rescan, so this is just a safety net.
const REFRESH_INTERVAL_S = 120;

// St CSS cannot size widgets with percentage widths, so the progress fill is
// computed in pixels against this fixed track width (must match the
// .ai-progress-track width in stylesheet.css).
const TRACK_WIDTH = 300;

// Weekly window span, for the pace line on a real weekly bar.
const WEEK_SPAN_S = 7 * 24 * 3600;

// Presentation for known tools. Which tools actually appear is decided by
// the daemon's snapshot (every tool with recorded usage), so a user with one
// subscription sees one section and a user with three sees three. Unknown
// tools (third-party adapters) get a generic style via toolStyle().
const TOOL_STYLES = {
    claude_code: {label: 'Claude Code', short: 'Claude', color: '#ff8866', prefix: 'claude'},
    gemini_cli: {label: 'agy', short: 'agy', color: '#66b3ff', prefix: 'gemini'},
    codex: {label: 'Codex', short: 'Codex', color: '#7bd8b0', prefix: 'codex'},
    opencode: {label: 'OpenCode', short: 'OpenCode', color: '#e6b34d', prefix: 'opencode'},
};
const TOOL_ORDER = Object.keys(TOOL_STYLES);

function toolStyle(id) {
    const known = TOOL_STYLES[id];
    if (known)
        return {short: known.label, ...known};
    const label = id.split('_')
        .map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    return {label, short: label, color: '#9a9aa5', prefix: id};
}

// Per-provider web links, opened in the default browser from the card footer —
// CodexBar's "Usage Dashboard" / "Status Page" rows. Only the ones that map
// cleanly to a real page are included (no "Add Account": this app reads
// whatever credential is on disk, it has no multi-account concept). URLs match
// the ones CodexBar uses.
const TOOL_LINKS = {
    claude_code: {
        dashboard: 'https://claude.ai/settings/usage',
        status: 'https://status.claude.com/',
    },
    gemini_cli: {  // Antigravity / Gemini — usage lives in the IDE; status only
        status: 'https://status.cloud.google.com',
    },
    codex: {
        dashboard: 'https://chatgpt.com/codex/settings/usage',
        status: 'https://status.openai.com',
    },
    opencode: {
        dashboard: 'https://opencode.ai',
    },
};

function formatTokens(n) {
    if (!Number.isFinite(n))
        return '0';
    if (n >= 1e9)
        return `${(n / 1e9).toFixed(2)}B`;
    if (n >= 1e6)
        return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e3)
        return `${(n / 1e3).toFixed(1)}K`;
    return `${n}`;
}

function formatCost(v) {
    return `$${(Number.isFinite(v) ? v : 0).toFixed(2)}`;
}

/** Fill %s placeholders left-to-right (tiny printf for translated strings). */
function fmt(template, ...args) {
    let i = 0;
    return template.replace(/%s/g, () => String(args[i++]));
}

function severityClass(pct) {
    if (pct >= 90)
        return 'danger';
    if (pct >= 70)
        return 'warn';
    return 'ok';
}

/** "at this pace" time-to-limit, from a burn rate in $/hour. */
function etaText(cost, budget, ratePerHour) {
    if (!(budget > 0))
        return '';
    if (cost >= budget)
        return _('limit reached');
    if (!(ratePerHour > 0.005))
        return '';  // idle or negligible burn: no meaningful projection
    const hours = (budget - cost) / ratePerHour;
    let span;
    if (hours < 1)
        span = `${Math.max(1, Math.round(hours * 60))}m`;
    else if (hours < 48)
        span = `${Math.round(hours)}h`;
    else
        span = `${Math.round(hours / 24)}d`;
    return fmt(_('≈%s left'), span);
}

/** Human time span: "45m", "3h 20m", "5d 22h". */
function spanStr(seconds) {
    const minutes = Math.max(1, Math.floor(seconds / 60));
    if (minutes < 60)
        return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) {
        const m = minutes % 60;
        return m ? `${hours}h ${String(m).padStart(2, '0')}m` : `${hours}h`;
    }
    const days = Math.floor(hours / 24);
    const h = hours % 24;
    return h ? `${days}d ${h}h` : `${days}d`;
}

/** Subtitle tail for an anchored window bar (5h session or weekly): the
 * time left until the provider resets it, plus a burn-rate warning ONLY
 * when the limit would be hit before that reset — a projection that lands
 * after the reset is meaningless, the window empties first.
 * Returns {text, warn}; warn tints the subtitle amber. */
function windowText(entry, budget, ratePerHour) {
    if (entry.session_active === false)
        return {text: _('no active session'), warn: false};
    if (!entry.resets_at) {  // old daemon without anchoring
        return {text: etaText(entry.cost_usd, budget, ratePerHour),
            warn: false};
    }
    const remaining = entry.resets_at - Date.now() / 1000;
    // approx: the provider can re-anchor this window server-side (Google
    // has globally reset Antigravity quotas), so the countdown is a guess.
    const resets = fmt(_('resets in %s'),
        (entry.approx ? '≈' : '') + spanStr(remaining));
    if (budget > 0 && entry.cost_usd >= budget)
        return {text: `${_('limit reached')} · ${resets}`, warn: true};
    if (budget > 0 && ratePerHour > 0.005) {
        const etaSecs = (budget - entry.cost_usd) / ratePerHour * 3600;
        if (etaSecs < remaining) {
            return {text: `${fmt(_('≈%s to limit'), spanStr(etaSecs))} · ${resets}`,
                warn: true};
        }
    }
    return {text: resets, warn: false};
}

/** Subtitle for a bar driven by the provider's REAL limit: the exact reset
 * countdown from resets_at. No burn-rate guessing — the % is authoritative,
 * so the only thing to add is when the provider resets the window. */
/** Local-time YYYY-MM-DD, matching the daemon's daily_series() keys. */
function localDayKey(date) {
    return `${date.getFullYear()}-` +
        `${String(date.getMonth() + 1).padStart(2, '0')}-` +
        `${String(date.getDate()).padStart(2, '0')}`;
}

function realWindowText(real) {
    if (!real || !real.resets_at)
        return '';
    const remaining = real.resets_at - Date.now() / 1000;
    if (remaining <= 0)
        return _('resetting…');
    return fmt(_('resets in %s'), spanStr(remaining));
}

/** Full subtitle for a provider-real bar: the reset countdown, plus the
 * daemon's burn-rate projection when the window runs out BEFORE the reset,
 * plus the data age when the poller is failing and the % is the last known
 * one (the bar shows a grey dot in that case). */
function realDetailText(real, live) {
    let text = realWindowText(real);
    const now = Date.now() / 1000;
    if (real?.depletes_at && real.depletes_at > now)
        text += `  ·  ${fmt(_('runs out in %s'), spanStr(real.depletes_at - now))}`;
    if (live?.stale && live.data_fetched_at) {
        const ago = Math.max(0, now - live.data_fetched_at);
        text += `  ·  ${fmt(_('data from %s ago'), spanStr(ago))}`;
    }
    return text;
}

/** 'default_claude_max_20x' -> 'Max 20x'; '' when unknown. Shown top-right of a
 * provider card like CodexBar's plan badge. */
function planLabel(tier) {
    if (!tier)
        return '';
    const t = String(tier)
        .replace(/^default_/, '').replace(/^claude_/, '')
        .replace(/_/g, ' ').trim();
    return t.replace(/\b([a-z])/g, (_m, c) => c.toUpperCase());
}

/** CodexBar-style pace: how the real usage compares to even consumption across
 * the window. Negative = under the even-pace line (usage will last to reset).
 * Returns {text, hot} — hot marks over-pace so the caller can tint the row. */
function paceText(usedPct, resetsAt, spanSeconds) {
    if (!(spanSeconds > 0) || !resetsAt)
        return null;
    const now = Date.now() / 1000;
    const elapsed = Math.max(0, Math.min(spanSeconds, spanSeconds - (resetsAt - now)));
    if (elapsed < spanSeconds * 0.03)
        return null;  // too early in the window to be meaningful
    const diff = Math.round(usedPct - elapsed / spanSeconds * 100);
    return diff <= 0
        ? {text: fmt(_('pace %s%% · lasts to reset'), diff), hot: false}
        : {text: fmt(_('pace +%s%% · running hot'), diff), hot: true};
}

const ProgressBarRow = GObject.registerClass(
class ProgressBarRow extends PopupMenu.PopupBaseMenuItem {
    /** opts: {warn, realPct, realStale, realDot, onActivate}. onActivate
     * makes the row clickable and runs INSTEAD of the default item
     * activation, so the menu stays open (tab navigation, not an action). */
    _init(title, cost, budget, tokens, extra = '', color = null, opts = {}) {
        const {warn: extraWarn = false, realPct = null, realStale = false,
            realDot = true, onActivate = null} = opts;
        super._init({reactive: !!onActivate, can_focus: !!onActivate});
        this._onActivate = onActivate;

        // A provider-real percentage (from a live poller) drives the bar
        // directly; otherwise fall back to the dollar-scaled estimate.
        const real = Number.isFinite(realPct);
        let pct = 0;
        if (real)
            pct = Math.min(100, Math.max(0, realPct));
        else if (budget > 0)
            pct = Math.min(100, Math.max(0, (cost / budget) * 100));
        const sev = severityClass(pct);

        const vbox = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'ai-progress-container',
        });

        // Title row: window name left, percentage right (severity-colored).
        // A teal "live" dot marks a bar sourced from the provider's real
        // limit rather than the local estimate.
        const titleRow = new St.BoxLayout();
        titleRow.add_child(new St.Label({
            text: title,
            style_class: 'ai-progress-title',
            x_expand: true,
        }));
        if (real && realDot) {
            // Teal dot = fresh provider data; grey = the poller is failing
            // and this is the last % it managed to fetch. realDot=false lets
            // a caller drive the bar by % without claiming provider truth
            // (Summary rows that fall back to the estimate).
            titleRow.add_child(new St.Label({
                text: '●',
                style_class: realStale ? 'ai-live-badge-stale' : 'ai-live-badge',
                y_align: Clutter.ActorAlign.CENTER,
            }));
        }
        titleRow.add_child(new St.Label({
            text: (real || budget > 0) ? `${Math.round(pct)}%` : formatCost(cost),
            style_class: `ai-progress-percent ai-text-${sev}`,
        }));

        // Track + fill. Fill width is computed in px (St has no % widths).
        // While usage is healthy the fill wears the tool's brand color; past
        // 70/90% the severity classes (amber/red) take over — danger wins.
        const track = new St.BoxLayout({style_class: 'ai-progress-track'});
        const fillWidth = pct > 0
            ? Math.max(4, Math.round(TRACK_WIDTH * pct / 100))
            : 0;
        // Healthy fill wears CodexBar's teal (.ai-fill-ok); the warn/danger
        // severities take over past 70/90%. The tool's brand color lives on
        // the header dot, so the meters stay uniform like CodexBar's.
        const fill = new St.BoxLayout({
            style_class: `ai-progress-fill ai-fill-${sev}`,
        });
        fill.set_style(`width: ${fillWidth}px;`);
        track.add_child(fill);

        // Fill-in sweep on first map (every menu open rebuilds the rows). A
        // paint-level scale, so the CSS width above keeps owning the layout.
        if (fillWidth > 0 && St.Settings.get().enable_animations) {
            fill.set_pivot_point(0, 0.5);
            fill.scale_x = 0;
            const mapId = fill.connect('notify::mapped', () => {
                if (!fill.mapped)
                    return;
                fill.disconnect(mapId);
                fill.ease({
                    scale_x: 1,
                    duration: 350,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                });
            });
        }

        let detail;
        if (cost <= 0 && tokens <= 0 && extra) {
            detail = extra;  // idle window: skip the "$0.00 · 0 tokens" noise
        } else {
            // With a real % the dollar budget is meaningless (the number isn't
            // derived from it), so show spend + tokens as context, not "of $Y".
            if (real)
                detail = fmt(_('%s  ·  %s tokens'),
                    formatCost(cost), formatTokens(tokens));
            else
                detail = budget > 0
                    ? fmt(_('%s of %s  ·  %s tokens'),
                        formatCost(cost), formatCost(budget), formatTokens(tokens))
                    : fmt(_('%s tokens'), formatTokens(tokens));
            if (extra)
                detail += `  ·  ${extra}`;
        }
        const subtitle = new St.Label({
            text: detail,
            style_class: extraWarn
                ? 'ai-progress-subtitle ai-text-warn'
                : 'ai-progress-subtitle',
        });

        vbox.add_child(titleRow);
        vbox.add_child(track);
        vbox.add_child(subtitle);
        this.add_child(vbox);
    }

    activate(event) {
        if (this._onActivate) {
            this._onActivate();
            return;  // skip itemActivated: keep the menu open
        }
        super.activate(event);
    }
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'AI Token Monitor');
        this._extension = extension;

        // Bundled brand SVGs (icons/<tool>-symbolic.svg, see icons/NOTICE),
        // cached by tool id; null marks a tool with no bundled icon so the
        // colored-dot fallback kicks in without re-statting the file.
        this._iconCache = new Map();

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        // Speedometer-style gauge (power-profiles) until usage data arrives;
        // then the most-pressured provider's brand glyph takes over.
        this._defaultPanelGicon = Gio.ThemedIcon.new_with_default_fallbacks(
            'power-profile-performance-symbolic');
        this._panelIcon = new St.Icon({
            gicon: this._defaultPanelGicon,
            fallback_icon_name: 'org.gnome.SystemMonitor-symbolic',
            style_class: 'system-status-icon',
        });
        box.add_child(this._panelIcon);
        this._label = new St.Label({
            text: '',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'ai-panel-label',
        });
        box.add_child(this._label);
        this.add_child(box);

        this._proxy = null;
        this._signalId = 0;
        this._ownerId = 0;
        this._snapshot = null;
        this._destroyed = false;
        this._proxyPending = false;
        this._cancellable = new Gio.Cancellable();
        // Last alert bucket per "tool:period" (0 <70%, 1 ≥70, 2 ≥90, 3 ≥100);
        // notifying only on upward transitions gives natural hysteresis.
        this._alertState = new Map();
        // Active 5h sessions from the previous snapshot (tool → resets_at),
        // diffed to announce "fresh window" when one expires.
        this._lastSessions = new Map();
        this._resetTimerId = 0;
        // Switcher: which tab is open — 'summary' (unified KPI view) or a tool
        // id (that provider's detailed card). Persists while the indicator
        // lives so reopening the menu keeps your place.
        this._selected = 'summary';

        this._rebuildMenu();
        this._initProxy();

        // Opening the menu always re-syncs: the daemon rescans its logs and
        // returns a fresh snapshot, so the data is live without any button.
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._syncNow();
        });

        this._timerId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, REFRESH_INTERVAL_S, () => {
                if (this._proxy)
                    this._refresh();
                else
                    this._initProxy();
                return GLib.SOURCE_CONTINUE;
            });
    }

    _initProxy() {
        if (this._proxyPending)
            return;
        this._proxyPending = true;
        new MonitorProxy(Gio.DBus.session, BUS_NAME, OBJECT_PATH,
            (proxy, error) => {
                this._proxyPending = false;
                if (this._destroyed)
                    return;
                if (error) {
                    if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        console.error(`[ai-token-monitor] proxy failed: ${error.message}`);
                    this._setUnavailable();
                    return;
                }
                this._proxy = proxy;
                this._signalId = proxy.connectSignal('UsageUpdated',
                    (_p, _sender, [json]) => this._applySnapshot(json));
                this._ownerId = proxy.connect('notify::g-name-owner', () => {
                    if (this._destroyed)
                        return;
                    if (proxy.g_name_owner)
                        this._refresh();
                    else
                        this._setUnavailable();
                });
                this._refresh();
            }, this._cancellable);
    }

    /** Force a daemon rescan and apply the resulting snapshot. */
    _syncNow() {
        if (!this._proxy) {
            this._initProxy();
            return;
        }
        this._proxy.RefreshRemote((result, error) => {
            if (this._destroyed)
                return;
            if (error) {
                if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED)) {
                    console.warn(`[ai-token-monitor] Refresh failed: ${error.message}`);
                    this._refresh();  // fall back to a passive read
                }
                return;
            }
            this._applySnapshot(result[0]);
        }, this._cancellable);
    }

    _refresh() {
        if (!this._proxy)
            return;
        this._proxy.GetSnapshotRemote((result, error) => {
            if (this._destroyed)
                return;
            if (error) {
                if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED)) {
                    console.warn(`[ai-token-monitor] GetSnapshot failed: ${error.message}`);
                    this._setUnavailable();
                }
                return;
            }
            this._applySnapshot(result[0]);
        }, this._cancellable);
    }

    _setUnavailable() {
        this._label.text = '';
        this._panelIcon.gicon = this._defaultPanelGicon;
        this._snapshot = null;
        this._rebuildMenu();
    }

    _applySnapshot(json) {
        let snapshot;
        try {
            snapshot = JSON.parse(json);
        } catch (e) {
            console.error(`[ai-token-monitor] bad snapshot: ${e}`);
            return;
        }
        this._snapshot = snapshot;
        this._updatePanel();
        this._checkAlerts();
        this._checkSessionResets();
        this._scheduleResetTimer();
        this._rebuildMenu();
    }

    /** Announce a fresh 5h window when a session that was counting down has
     * expired. Diff-based (not timer-based) so it also works after suspend,
     * where GLib timers stall — the 120s poll or any signal catches up. */
    _checkSessionResets() {
        const now = Date.now() / 1000;
        const current = new Map();
        for (const t of this._snapshot?.five_hours?.tools ?? []) {
            if (t.session_active && t.resets_at)
                current.set(t.tool, t.resets_at);
        }
        if (this._snapshot?.ui?.alerts !== false) {
            for (const [tool, resetsAt] of this._lastSessions) {
                if (resetsAt <= now && current.get(tool) !== resetsAt) {
                    Main.notify(
                        fmt(_('%s — session reset'), toolStyle(tool).label),
                        _('A fresh 5-hour window is available'));
                }
            }
        }
        this._lastSessions = current;
    }

    /** Precision wake-up at the next session reset; it only refreshes — the
     * snapshot diff above does the announcing. */
    _scheduleResetTimer() {
        if (this._resetTimerId) {
            GLib.source_remove(this._resetTimerId);
            this._resetTimerId = 0;
        }
        const now = Date.now() / 1000;
        let next = null;
        for (const resetsAt of this._lastSessions.values()) {
            if (resetsAt > now && (next === null || resetsAt < next))
                next = resetsAt;
        }
        if (next === null)
            return;
        this._resetTimerId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, Math.max(2, Math.round(next - now) + 2),
            () => {
                this._resetTimerId = 0;
                this._refresh();
                return GLib.SOURCE_REMOVE;
            });
    }

    /** Desktop notification when a limit bar crosses 70/90/100%. */
    _checkAlerts() {
        if (this._snapshot?.ui?.alerts === false)
            return;
        for (const id of this._activeTools()) {
            const style = toolStyle(id);
            for (const [period, label, budgetKey] of [
                ['five_hours', _('Session (5h)'), `${style.prefix}_5h`],
                ['week', _('Weekly'), `${style.prefix}_weekly`],
            ]) {
                // Alert on the provider's real % when available (more accurate
                // than the estimate); otherwise fall back to cost/budget.
                let pct = this._realPct(period, id);
                let body;
                if (pct === null) {
                    const budget = this._budget(budgetKey);
                    if (!(budget > 0))
                        continue;
                    const cost = this._toolData(period, id).cost_usd;
                    pct = cost / budget * 100;
                    body = fmt(_('%s of %s used'),
                        formatCost(cost), formatCost(budget));
                } else {
                    body = fmt(_('%s%% used'), Math.round(pct));
                }
                const bucket = pct >= 100 ? 3 : pct >= 90 ? 2 : pct >= 70 ? 1 : 0;
                const key = `${id}:${period}`;
                const prev = this._alertState.get(key) ?? 0;
                if (bucket > prev) {
                    Main.notify(
                        fmt(_('%s — %s at %s%%'),
                            style.label, label, Math.round(pct)),
                        body);
                }
                this._alertState.set(key, bucket);

                // One-shot heads-up (per window instance) when the daemon's
                // burn-rate projection says the limit runs out BEFORE the
                // provider resets it.
                const real = this._toolData(period, id).real;
                if (real?.depletes_at && real.resets_at) {
                    const dKey = `${key}:depleted:${Math.round(real.resets_at)}`;
                    if (!this._alertState.has(dKey)) {
                        this._alertState.set(dKey, 1);
                        const now = Date.now() / 1000;
                        Main.notify(
                            fmt(_('%s — %s may run out before reset'),
                                style.label, label),
                            fmt(_('at this pace it runs out in %s (resets in %s)'),
                                spanStr(real.depletes_at - now),
                                spanStr(real.resets_at - now)));
                    }
                }
            }
        }
    }

    _toolData(period, toolName) {
        const pData = this._snapshot?.[period];
        const tData = pData?.tools?.find(t => t.tool === toolName);
        return tData ?? {cost_usd: 0, total_tokens: 0};
    }

    /** The provider's real used-% for a window if a live poller supplied it,
     * else null (caller falls back to the dollar estimate). */
    _realPct(period, toolName) {
        const v = this._toolData(period, toolName).real?.used_percent;
        return Number.isFinite(v) ? v : null;
    }

    _budget(key) {
        const v = this._snapshot?.budgets?.[key];
        return Number.isFinite(v) && v > 0 ? v : 0;
    }

    /** Tools to render: whatever the daemon has usage data for, in a stable
     * order. Falls back to scanning the period payloads for older daemons
     * that don't send a `tools` list. */
    _activeTools() {
        let ids = this._snapshot?.tools;
        if (!Array.isArray(ids)) {
            const seen = new Set();
            for (const period of ['five_hours', 'today', 'week', 'month'])
                this._snapshot?.[period]?.tools?.forEach(t => seen.add(t.tool));
            ids = [...seen];
        }
        return ids.slice().sort((a, b) => {
            const ia = TOOL_ORDER.indexOf(a), ib = TOOL_ORDER.indexOf(b);
            return (ia < 0 ? TOOL_ORDER.length : ia) -
                   (ib < 0 ? TOOL_ORDER.length : ib) || a.localeCompare(b);
        });
    }

    /** The bundled brand SVG for a tool, or null (third-party adapters fall
     * back to the colored dot). Cached: one stat per tool per session. */
    _brandIconFile(id) {
        if (!this._iconCache.has(id)) {
            const f = Gio.File.new_for_path(
                `${this._extension.path}/icons/${id}-symbolic.svg`);
            this._iconCache.set(id, f.query_exists(null) ? f : null);
        }
        return this._iconCache.get(id);
    }

    _brandIcon(id, styleClass, color = null) {
        const file = this._brandIconFile(id);
        if (!file)
            return null;
        return new St.Icon({
            gicon: new Gio.FileIcon({file}),
            style_class: styleClass,
            style: color ? `color: ${color};` : '',
            y_align: Clutter.ActorAlign.CENTER,
        });
    }

    _updatePanel() {
        // The most-pressured provider owns the panel: its brand glyph next
        // to the % (monochrome, theme-colored like any status icon).
        const top = this._topPressure(this._activeTools());
        const file = top && this._brandIconFile(top.id);
        this._panelIcon.gicon = file
            ? new Gio.FileIcon({file})
            : this._defaultPanelGicon;

        const mode = this._snapshot?.ui?.panel ?? 'percent';
        if (mode === 'icon') {
            this._label.text = '';
            return;
        }
        if (mode === 'today') {
            this._label.text =
                formatCost(this._snapshot?.today?.totals?.cost_usd ?? 0);
            this._label.style_class = 'ai-panel-label';
            return;
        }
        if (!top) {
            this._label.text = '';
            return;
        }
        const pct = Math.min(999, Math.round(top.pct));
        this._label.text = `${pct}%`;
        this._label.style_class =
            `ai-panel-label ai-text-${severityClass(top.pct)}`;
    }

    /** Tab bar: a "Summary" KPI tab, then one tab per active tool. Clicking a
     * tab swaps the card below without growing the popup (CodexBar-style). */
    _addTabBar(active, sel) {
        const item = new PopupMenu.PopupBaseMenuItem(
            {reactive: false, can_focus: false, style_class: 'ai-tabbar-item'});
        const row = new St.BoxLayout({style_class: 'ai-tabbar', x_expand: true});
        const addTab = (id, label, dotColor) => {
            const btn = new St.Button({
                style_class: sel === id ? 'ai-tab ai-tab-active' : 'ai-tab',
                x_expand: false,
                can_focus: true,
            });
            const box = new St.BoxLayout({style_class: 'ai-tab-box'});
            const icon = dotColor
                ? this._brandIcon(id, 'ai-tab-icon', dotColor) : null;
            if (icon) {
                box.add_child(icon);
            } else if (dotColor) {
                box.add_child(new St.Label({
                    text: '●', style: `color: ${dotColor};`,
                    style_class: 'ai-tab-dot', y_align: Clutter.ActorAlign.CENTER,
                }));
            }
            box.add_child(new St.Label({
                text: label, style_class: 'ai-tab-label',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            btn.set_child(box);
            btn.connect('clicked', () => {
                this._selected = id;
                this._rebuildMenu();
            });
            row.add_child(btn);
        };
        addTab('summary', _('Summary'), null);
        for (const id of active)
            addTab(id, toolStyle(id).short, toolStyle(id).color);
        item.add_child(row);
        this.menu.addMenuItem(item);
    }

    /** Week-over-week spend change in %: the last 7 calendar days vs the 7
     * before them, from the daemon's 14-day daily series. Null until the
     * previous week has any spend to compare against. */
    _weekDelta() {
        const byDay = new Map(
            (this._snapshot?.daily ?? []).map(d => [d.day, d.cost_usd ?? 0]));
        if (!byDay.size)
            return null;
        const dayCost = ago =>
            byDay.get(localDayKey(new Date(Date.now() - ago * 86400000))) ?? 0;
        let cur = 0, prev = 0;
        for (let i = 0; i < 7; i++)
            cur += dayCost(i);
        for (let i = 7; i < 14; i++)
            prev += dayCost(i);
        if (!(prev > 0))
            return null;
        return (cur - prev) / prev * 100;
    }

    /** A tool's most-pressured window (real % preferred): {label, pct,
     * resets_at, real, realBlock, cost, tokens}. Tools with no real % and no
     * budgeted window (OpenCode) fall back to a cost-only weekly entry so
     * every provider still gets a Summary "Limits" row. */
    _worstWindow(id) {
        const prefix = toolStyle(id).prefix;
        let best = null;
        for (const [period, suffix, label] of [
            ['five_hours', '_5h', _('Session (5h)')],
            ['week', '_weekly', _('Weekly')],
        ]) {
            const entry = this._toolData(period, id);
            let pct = this._realPct(period, id);
            let resets = entry.real?.resets_at;
            const realBlock = pct !== null ? entry.real : null;
            if (pct === null) {
                const b = this._budget(`${prefix}${suffix}`);
                if (!(b > 0))
                    continue;
                pct = entry.cost_usd / b * 100;
                resets = entry.resets_at;
            }
            if (!best || pct > best.pct) {
                best = {label, pct, resets_at: resets, real: !!realBlock,
                    realBlock, cost: entry.cost_usd, tokens: entry.total_tokens};
            }
        }
        if (!best) {
            const entry = this._toolData('week', id);
            best = {label: _('Weekly'), pct: null, resets_at: entry.resets_at,
                real: false, realBlock: null,
                cost: entry.cost_usd, tokens: entry.total_tokens};
        }
        return best;
    }

    /** The single most-pressured window across all tools (cost-only rows,
     * which have no %, never win). */
    _topPressure(active) {
        let top = null;
        for (const id of active) {
            const w = this._worstWindow(id);
            if (w.pct !== null && (!top || w.pct > top.pct))
                top = {id, ...w};
        }
        return top;
    }

    /** Summary tab: cross-provider KPIs (spend today/week/month), the single
     * most-pressured limit, and the active providers — a unified glance that
     * doesn't repeat each tool's window bars. The 7-day sparkline + footer are
     * added after this by _rebuildMenu. */
    _addSummary(active) {
        const totals = p => this._snapshot?.[p]?.totals ?? {cost_usd: 0, total_tokens: 0};
        const spend = (label, p, note = '', noteClass = undefined) =>
            this._addKeyValueRow(label,
                `${formatCost(totals(p).cost_usd)}  ·  ${formatTokens(totals(p).total_tokens)}`,
                note, noteClass);

        this._addSectionLabel(_('Spend · all providers'));
        spend(_('Today'), 'today');

        // Week-over-week trend (up = spending more = amber) and a simple
        // linear month-end projection give the raw totals some context.
        const delta = this._weekDelta();
        spend(_('This week'), 'week',
            delta === null
                ? '' : `${delta >= 0 ? '↑' : '↓'}${Math.abs(Math.round(delta))}%`,
            delta !== null && delta >= 0
                ? 'ai-kv-note ai-text-warn' : 'ai-kv-note ai-delta-down');
        const now = new Date();
        const dayOfMonth = now.getDate();
        const daysInMonth =
            new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
        const monthCost = totals('month').cost_usd;
        spend(_('This month'), 'month',
            dayOfMonth >= 3 && monthCost > 0
                ? fmt(_('≈ %s at close'),
                    formatCost(monthCost / dayOfMonth * daysInMonth))
                : '');

        // One compact bar per provider — its most-pressured window — sorted
        // most-critical first (cost-only rows land last). The teal live dot
        // only marks provider-real %; clicking a row opens that provider's
        // tab without closing the popup.
        const limits = active
            .map(id => ({id, w: this._worstWindow(id)}))
            .sort((a, b) => (b.w.pct ?? -1) - (a.w.pct ?? -1));
        if (limits.length) {
            this._addSectionLabel(_('Limits'));
            for (const {id, w} of limits) {
                const s = toolStyle(id);
                const liveInfo = this._snapshot?.live?.[id];
                this.menu.addMenuItem(new ProgressBarRow(
                    `${s.short} · ${w.label}`, w.cost ?? 0, 0, w.tokens ?? 0,
                    w.realBlock
                        ? realDetailText(w.realBlock, liveInfo)
                        : w.resets_at
                            ? realWindowText({resets_at: w.resets_at}) : '',
                    s.color, {
                        warn: !!w.realBlock?.depletes_at,
                        realPct: w.pct,
                        realStale: !!liveInfo?.stale,
                        realDot: w.real,
                        onActivate: () => {
                            this._selected = id;
                            this._rebuildMenu();
                        },
                    }));
            }
        }

        this._addSpendSplit(active);
        this._addLinksSection(active);
    }

    /** Thin stacked bar of the rolling week's spend by provider (brand
     * colors) with an icon + amount legend — who is eating the money. */
    _addSpendSplit(active) {
        const split = active
            .map(id => ({id, cost: this._toolData('week', id).cost_usd}))
            .filter(r => r.cost > 0)
            .sort((a, b) => b.cost - a.cost);
        if (!split.length)
            return;
        const total = split.reduce((sum, r) => sum + r.cost, 0);
        this._addSectionLabel(_('This week · by provider'));

        const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
        const vbox = new St.BoxLayout({
            vertical: true, x_expand: true,
            style_class: 'ai-progress-container',
        });
        const track = new St.BoxLayout({style_class: 'ai-progress-track'});
        const widths = split.map(r =>
            Math.max(2, Math.round(TRACK_WIDTH * r.cost / total)));
        widths[0] += TRACK_WIDTH - widths.reduce((a, b) => a + b, 0);
        split.forEach((r, i) => {
            const seg = new St.BoxLayout({style_class: 'ai-split-seg'});
            // St doesn't clip children by the parent's radius, so round only
            // the outer corners of the first/last segment.
            const l = i === 0 ? 3 : 0;
            const rr = i === split.length - 1 ? 3 : 0;
            seg.set_style(`background-color: ${toolStyle(r.id).color}; ` +
                `width: ${widths[i]}px; ` +
                `border-radius: ${l}px ${rr}px ${rr}px ${l}px;`);
            track.add_child(seg);
        });

        const legend = new St.BoxLayout({style_class: 'ai-legend'});
        for (const r of split) {
            const s = toolStyle(r.id);
            const cell = new St.BoxLayout({style_class: 'ai-legend-cell'});
            cell.add_child(this._brandIcon(r.id, 'ai-tab-icon', s.color) ??
                new St.Label({
                    text: '●', style: `color: ${s.color};`,
                    style_class: 'ai-tab-dot',
                    y_align: Clutter.ActorAlign.CENTER,
                }));
            cell.add_child(new St.Label({
                text: formatCost(r.cost), style_class: 'ai-legend-label',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            legend.add_child(cell);
        }

        vbox.add_child(track);
        vbox.add_child(legend);
        item.add_child(vbox);
        this.menu.addMenuItem(item);
    }

    _addSectionLabel(text) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
        item.add_child(new St.Label({
            text, style_class: 'ai-section-label', x_expand: true,
        }));
        this.menu.addMenuItem(item);
    }

    _addKeyValueRow(label, value, note = '', noteClass = 'ai-kv-note') {
        const row = new PopupMenu.PopupBaseMenuItem({reactive: false});
        row.add_child(new St.Label({
            text: label, style_class: 'ai-model-name', x_expand: true,
        }));
        row.add_child(new St.Label({text: value, style_class: 'ai-model-cost'}));
        if (note) {
            row.add_child(new St.Label({
                text: note, style_class: noteClass,
                y_align: Clutter.ActorAlign.CENTER,
            }));
        }
        this.menu.addMenuItem(row);
    }

    /** Per-provider web links (usage dashboard / status page) as one compact
     * row per provider, opened in the default browser. Lives on the Summary
     * tab (and the stacked layout's tail) so provider cards stay focused on
     * usage. */
    _addLinksSection(active) {
        const rows = active
            .map(id => [id, TOOL_LINKS[id]])
            .filter(([, links]) => links?.dashboard || links?.status);
        if (!rows.length)
            return;
        this._addSectionLabel(_('Links'));
        for (const [id, links] of rows) {
            const style = toolStyle(id);
            const row = new PopupMenu.PopupBaseMenuItem({reactive: false});
            row.add_child(this._brandIcon(id, 'ai-brand-icon', style.color) ??
                new St.Label({
                    text: '●', style: `color: ${style.color};`,
                    style_class: 'ai-tool-dot', y_align: Clutter.ActorAlign.CENTER,
                }));
            row.add_child(new St.Label({
                text: style.short, style_class: 'ai-model-name',
                x_expand: true, y_align: Clutter.ActorAlign.CENTER,
            }));
            const addLink = (label, url) => {
                const btn = new St.Button({
                    label, style_class: 'ai-link-button', can_focus: true,
                });
                btn.connect('clicked', () => {
                    this.menu.close();
                    try {
                        Gio.AppInfo.launch_default_for_uri(url, null);
                    } catch (e) {
                        console.warn(`[ai-token-monitor] open ${url}: ${e}`);
                    }
                });
                row.add_child(btn);
            };
            if (links.dashboard)
                addLink(_('dashboard'), links.dashboard);
            if (links.status)
                addLink(_('status'), links.status);
            this.menu.addMenuItem(row);
        }
    }

    /** One provider's detailed card, CodexBar-style: header with plan tier;
     * Session + Weekly bars (per-pool for grouped tools, provider-real when a
     * live poller supplied it, with a pace line); a per-model breakdown (real
     * per-model caps, else the local cost breakdown); an optional Extra-usage
     * bar; and today / this-month cost. */
    _addProviderDetail(id) {
        const style = toolStyle(id);
        const week = this._toolData('week', id);
        const fiveH = this._toolData('five_hours', id);
        const live = this._snapshot?.live?.[id];

        // Header: brand glyph (dot fallback) + name (left), plan tier (or
        // weekly spend) on the right.
        const header = new PopupMenu.PopupBaseMenuItem({reactive: false});
        header.add_child(this._brandIcon(id, 'ai-brand-icon', style.color) ??
            new St.Label({
                text: '●', style: `color: ${style.color};`,
                style_class: 'ai-tool-dot', y_align: Clutter.ActorAlign.CENTER,
            }));
        header.add_child(new St.Label({
            text: style.label, style_class: 'ai-tool-name',
            x_expand: true, y_align: Clutter.ActorAlign.CENTER,
        }));
        // Prefer the plan actually driving the budgets (explicit config, else
        // credential-detected) over the raw credential tier — the tier can
        // lag the configured plan (org Max 20x still reports max_5x).
        const tier = planLabel(this._snapshot?.plans?.[id]) ||
            planLabel(live?.plan_tier);
        header.add_child(new St.Label({
            text: tier || fmt(_('%s / wk'), formatCost(week.cost_usd)),
            style_class: 'ai-tool-cost', y_align: Clutter.ActorAlign.CENTER,
        }));
        this.menu.addMenuItem(header);

        // When the live poller is failing, the bars below silently fall back
        // to the dollar estimate — say so, with the poller's own reason.
        if (live?.status && live.status !== 'ok') {
            const err = new PopupMenu.PopupBaseMenuItem({reactive: false});
            err.add_child(new St.Label({
                text: fmt(_('live limits unavailable: %s'), String(live.status)),
                style_class: 'ai-live-status',
                x_expand: true,
            }));
            this.menu.addMenuItem(err);
        }

        const hourRate = this._toolData('hour', id).cost_usd;
        const dayRate = this._toolData('day', id).cost_usd / 24;
        const budget5h = this._budget(`${style.prefix}_5h`);
        const budgetWk = this._budget(`${style.prefix}_weekly`);

        // Grouped tools (Antigravity: Gemini vs Claude & GPT) get a pair per
        // pool, each preferring the provider's real per-pool % when the live
        // poller supplied one.
        const stale = !!live?.stale;
        const fiveGroups = fiveH.groups ?? [];
        const weekByKey = new Map((week.groups ?? []).map(g => [g.key, g]));
        if (fiveGroups.length >= 2) {
            for (const g5 of fiveGroups) {
                const gw = weekByKey.get(g5.key) ?? {cost_usd: 0, total_tokens: 0};
                const r5 = g5.real, rw = gw.real;
                const s = windowText(g5, budget5h, 0);
                const w = windowText(gw, budgetWk, 0);
                this.menu.addMenuItem(new ProgressBarRow(
                    `${_('Session (5h)')} · ${g5.label}`,
                    g5.cost_usd, budget5h, g5.total_tokens,
                    r5 ? realDetailText(r5, live) : s.text, style.color, {
                        warn: r5 ? !!r5.depletes_at : s.warn,
                        realPct: r5 ? r5.used_percent : null,
                        realStale: stale,
                    }));
                this.menu.addMenuItem(new ProgressBarRow(
                    `${_('Weekly')} · ${g5.label}`,
                    gw.cost_usd, budgetWk, gw.total_tokens,
                    rw ? realDetailText(rw, live) : w.text, style.color, {
                        warn: rw ? !!rw.depletes_at : w.warn,
                        realPct: rw ? rw.used_percent : null,
                        realStale: stale,
                    }));
            }
        } else {
            const real5 = fiveH.real, realWk = week.real;
            const session = windowText(fiveH, budget5h, hourRate);
            const weekly = windowText(week, budgetWk, dayRate);
            this.menu.addMenuItem(new ProgressBarRow(
                _('Session (5h)'), fiveH.cost_usd, budget5h, fiveH.total_tokens,
                real5 ? realDetailText(real5, live) : session.text, style.color, {
                    warn: real5 ? !!real5.depletes_at : session.warn,
                    realPct: real5 ? real5.used_percent : null,
                    realStale: stale,
                }));
            // Weekly, with a CodexBar-style pace line once the real % is known.
            let wkText = realWk ? realDetailText(realWk, live) : weekly.text;
            let wkWarn = realWk ? !!realWk.depletes_at : weekly.warn;
            if (realWk) {
                const p = paceText(realWk.used_percent, realWk.resets_at, WEEK_SPAN_S);
                if (p) {
                    wkText += `  ·  ${p.text}`;
                    wkWarn ||= p.hot;
                }
            }
            this.menu.addMenuItem(new ProgressBarRow(
                _('Weekly'), week.cost_usd, budgetWk, week.total_tokens,
                wkText, style.color, {
                    warn: wkWarn,
                    realPct: realWk ? realWk.used_percent : null,
                    realStale: stale,
                }));

            // Monthly window — for plans that also meter a monthly limit, like
            // OpenCode Go (Continuous / Weekly / Monthly). The provider's real
            // monthly % lives behind a login, so this shows the calendar-month
            // spend, scaled by an optional budgets.<prefix>_monthly if set.
            const budgetMo = this._budget(`${style.prefix}_monthly`);
            if (id === 'opencode' || budgetMo > 0) {
                const month = this._toolData('month', id);
                this.menu.addMenuItem(new ProgressBarRow(
                    _('Monthly'), month.cost_usd, budgetMo, month.total_tokens,
                    budgetMo > 0 ? '' : _('this calendar month'),
                    style.color));
            }
        }

        // Per-model breakdown: prefer the provider's REAL per-model caps
        // (Claude's weekly-scoped models, e.g. Opus/Sonnet), rendered as their
        // own bars; otherwise fall back to the local per-model cost breakdown.
        const scoped = week.real_scoped ?? [];
        if (scoped.length) {
            for (const m of scoped) {
                this.menu.addMenuItem(new ProgressBarRow(
                    m.label, 0, 0, 0,
                    m.resets_at ? realWindowText(m) : '', style.color,
                    {realPct: m.used_percent, realStale: stale}));
            }
        } else if ((week.models ?? []).length) {
            this._addSectionLabel(_('By model'));
            for (const m of week.models)
                this._addKeyValueRow(
                    m.model,
                    `${formatCost(m.cost_usd)}  ·  ${formatTokens(m.total_tokens)}`);
        }

        // Extra usage (Claude credit overage) — only when the plan enables it.
        const xu = live?.extra_usage;
        if (xu?.enabled && Number.isFinite(xu.used_percent)) {
            const used = Number.isFinite(xu.used_credits) ? xu.used_credits / 100 : 0;
            const limit = Number.isFinite(xu.monthly_limit) ? xu.monthly_limit / 100 : 0;
            this.menu.addMenuItem(new ProgressBarRow(
                _('Extra usage'), 0, 0, 0,
                limit > 0
                    ? fmt(_('%s of %s this month'), formatCost(used), formatCost(limit))
                    : formatCost(used),
                style.color, {realPct: xu.used_percent}));
        }

        // Cost: today and this month.
        this._addSectionLabel(_('Cost'));
        const today = this._toolData('today', id);
        const month = this._toolData('month', id);
        this._addKeyValueRow(_('Today'),
            `${formatCost(today.cost_usd)}  ·  ${formatTokens(today.total_tokens)}`);
        this._addKeyValueRow(_('This month'),
            `${formatCost(month.cost_usd)}  ·  ${formatTokens(month.total_tokens)}`);
    }

    _rebuildMenu() {
        this.menu.removeAll();

        if (!this._snapshot) {
            const offline = new PopupMenu.PopupMenuItem(_('Daemon offline'),
                {reactive: false});
            this.menu.addMenuItem(offline);
            const hint = new PopupMenu.PopupMenuItem(
                'systemctl --user start ai-token-monitor', {reactive: false});
            hint.label.add_style_class_name('ai-progress-subtitle');
            this.menu.addMenuItem(hint);
            this._addSettingsItem();
            return;
        }

        const active = this._activeTools();
        if (!active.length) {
            const empty = new PopupMenu.PopupMenuItem(_('No usage recorded yet'),
                {reactive: false});
            this.menu.addMenuItem(empty);
            const hint = new PopupMenu.PopupMenuItem(
                _('Use Claude Code, agy, Codex or OpenCode and it will appear here'),
                {reactive: false});
            hint.label.add_style_class_name('ai-progress-subtitle');
            this.menu.addMenuItem(hint);
            this._addSettingsItem();
            return;
        }

        // Layout: "switcher" (default) shows a provider tab bar with one card
        // at a time — the compact CodexBar-style view; "stacked" lists every
        // provider's full section at once (the original behaviour).
        const layout = this._snapshot.ui?.layout ?? 'switcher';
        // Provider tabs stay focused on the card; the links, sparkline,
        // footer and Preferences all live on the Summary tab only.
        let showTail = true;
        if (layout === 'stacked') {
            active.forEach((id, index) => {
                if (index > 0)
                    this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
                this._addProviderDetail(id);
            });
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            this._addLinksSection(active);
        } else {
            // Switcher: a Summary KPI tab plus one card per tool. Default to
            // Summary; keep the user's pick once they choose a tab.
            let sel = this._selected;
            if (sel !== 'summary' && !active.includes(sel))
                sel = this._selected = 'summary';
            this._addTabBar(active, sel);
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            if (sel === 'summary')
                this._addSummary(active);
            else
                this._addProviderDetail(sel);
            showTail = sel === 'summary';
        }

        if (!showTail)
            return;

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addSparkline();
        // Footer: poller health on the left (only when something is failing —
        // the spend numbers already live in the Summary KPIs), update age on
        // the right.
        const failing = Object.entries(this._snapshot.live ?? {})
            .filter(([, v]) => v?.status && v.status !== 'ok')
            .map(([tool, v]) => `${toolStyle(tool).short}: ${v.status}`);
        const footer = new PopupMenu.PopupBaseMenuItem({reactive: false});
        footer.add_child(new St.Label({
            text: failing.length ? `⚠ ${failing.join('  ·  ')}` : '',
            style_class: 'ai-footer ai-text-warn',
            x_expand: true,
        }));
        const updatedAgo = Math.max(
            0, Date.now() / 1000 - (this._snapshot.updated ?? 0));
        footer.add_child(new St.Label({
            text: fmt(_('updated %s ago'), spanStr(updatedAgo)),
            style_class: 'ai-footer',
        }));
        this.menu.addMenuItem(footer);
        this._addSettingsItem();
    }

    /** Preferences row at the bottom of the menu — the only in-widget path
     * to the plan/budget-mode/display settings, so users don't need to know
     * the Extensions app or `gnome-extensions prefs` even exists. */
    _addSettingsItem() {
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const item = new PopupMenu.PopupImageMenuItem(
            _('Preferences…'), 'preferences-system-symbolic');
        item.connect('activate', () => this._extension.openPreferences());
        this.menu.addMenuItem(item);
    }

    /** Mini bar chart of the last 7 days' spend, stacked per tool in brand
     * colors. Always renders seven labeled day slots (empty days get a dim
     * baseline stub) so the shape reads as a calendar week even with sparse
     * data, and a header anchors the scale (tallest bar = peak day). */
    _addSparkline() {
        const byDay = new Map(
            (this._snapshot.daily ?? []).map(d => [d.day, d]));
        if (!byDay.size)
            return;

        const days = [];
        for (let i = 6; i >= 0; i--) {
            const date = new Date(Date.now() - i * 86400000);
            days.push({date, data: byDay.get(localDayKey(date)), today: i === 0});
        }
        const max = Math.max(...days.map(d => d.data?.cost_usd ?? 0), 0.01);
        const tools = this._activeTools();

        const title = new PopupMenu.PopupBaseMenuItem({reactive: false});
        title.add_child(new St.Label({
            text: _('Last 7 days'),
            style_class: 'ai-progress-title',
            x_expand: true,
        }));
        const delta = this._weekDelta();
        if (delta !== null) {
            title.add_child(new St.Label({
                text: `${delta >= 0 ? '↑' : '↓'}${Math.abs(Math.round(delta))}%   `,
                style_class: delta >= 0
                    ? 'ai-progress-subtitle ai-text-warn'
                    : 'ai-progress-subtitle ai-delta-down',
            }));
        }
        title.add_child(new St.Label({
            text: fmt(_('peak %s / day'), formatCost(max)),
            style_class: 'ai-progress-subtitle',
        }));
        this.menu.addMenuItem(title);

        const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
        const row = new St.BoxLayout({
            style_class: 'ai-spark-row',
            x_expand: true,
        });
        for (const {date, data, today} of days) {
            const col = new St.BoxLayout({
                vertical: true,
                style_class: 'ai-spark-col',
                x_expand: true,
            });
            const bars = new St.BoxLayout({
                vertical: true,
                style_class: 'ai-spark-bars',
            });
            bars.add_child(new St.Widget({y_expand: true}));  // bottom-align
            let drew = false;
            const byTool = data?.by_tool ?? {};
            // Stack in reverse so tools[0] sits at the base.
            for (const tool of [...tools].reverse()) {
                const cost = byTool[tool] ?? 0;
                if (!(cost > 0))
                    continue;
                bars.add_child(new St.Widget({
                    style_class: 'ai-spark-seg',
                    style: `height: ${Math.max(1, Math.round(22 * cost / max))}px;` +
                        ` background-color: ${toolStyle(tool).color};`,
                    x_expand: true,
                }));
                drew = true;
            }
            if (!drew && data?.cost_usd > 0) {  // old daemon without by_tool
                bars.add_child(new St.Widget({
                    style_class: 'ai-spark-bar',
                    style: `height: ${Math.max(2, Math.round(22 * data.cost_usd / max))}px;`,
                    x_expand: true,
                }));
                drew = true;
            }
            if (!drew) {
                bars.add_child(new St.Widget({
                    style_class: 'ai-spark-empty',
                    x_expand: true,
                }));
            }
            col.add_child(bars);

            let dayName;
            try {
                dayName = date.toLocaleDateString(undefined, {weekday: 'short'});
            } catch {
                dayName = '';
            }
            col.add_child(new St.Label({
                text: `${dayName} ${date.getDate()}`.trim(),
                style_class: today
                    ? 'ai-spark-day ai-spark-today'
                    : 'ai-spark-day',
                x_align: Clutter.ActorAlign.CENTER,
            }));
            row.add_child(col);
        }
        item.add_child(row);
        this.menu.addMenuItem(item);
    }

    destroy() {
        this._destroyed = true;
        this._cancellable.cancel();
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
        if (this._resetTimerId) {
            GLib.source_remove(this._resetTimerId);
            this._resetTimerId = 0;
        }
        if (this._proxy) {
            if (this._signalId)
                this._proxy.disconnectSignal(this._signalId);
            if (this._ownerId)
                this._proxy.disconnect(this._ownerId);
            this._proxy = null;
        }
        super.destroy();
    }
});

export default class AITokenMonitorExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
