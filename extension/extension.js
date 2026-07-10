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

// Presentation for known tools. Which tools actually appear is decided by
// the daemon's snapshot (every tool with recorded usage), so a user with one
// subscription sees one section and a user with three sees three. Unknown
// tools (third-party adapters) get a generic style via toolStyle().
const TOOL_STYLES = {
    claude_code: {label: 'Claude Code', color: '#ff8866', prefix: 'claude'},
    gemini_cli: {label: 'agy', color: '#66b3ff', prefix: 'gemini'},
    codex: {label: 'Codex', color: '#7bd8b0', prefix: 'codex'},
};
const TOOL_ORDER = Object.keys(TOOL_STYLES);

function toolStyle(id) {
    return TOOL_STYLES[id] ?? {
        label: id.split('_')
            .map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' '),
        color: '#9a9aa5',
        prefix: id,
    };
}

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

/** Blend a #rrggbb color toward white by factor f (0..1). */
function lighten(hex, f) {
    const n = parseInt(hex.slice(1), 16);
    const ch = shift => Math.min(255,
        Math.round(((n >> shift) & 0xff) + (255 - ((n >> shift) & 0xff)) * f));
    return `#${((ch(16) << 16) | (ch(8) << 8) | ch(0))
        .toString(16).padStart(6, '0')}`;
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

const ProgressBarRow = GObject.registerClass(
class ProgressBarRow extends PopupMenu.PopupBaseMenuItem {
    _init(title, cost, budget, tokens, extra = '', color = null,
        extraWarn = false) {
        super._init({reactive: false});

        let pct = 0;
        if (budget > 0)
            pct = Math.min(100, Math.max(0, (cost / budget) * 100));
        const sev = severityClass(pct);

        const vbox = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'ai-progress-container',
        });

        // Title row: window name left, percentage right (severity-colored).
        const titleRow = new St.BoxLayout();
        titleRow.add_child(new St.Label({
            text: title,
            style_class: 'ai-progress-title',
            x_expand: true,
        }));
        titleRow.add_child(new St.Label({
            text: budget > 0 ? `${Math.round(pct)}%` : formatCost(cost),
            style_class: `ai-progress-percent ai-text-${sev}`,
        }));

        // Track + fill. Fill width is computed in px (St has no % widths).
        // While usage is healthy the fill wears the tool's brand color; past
        // 70/90% the severity classes (amber/red) take over — danger wins.
        const track = new St.BoxLayout({style_class: 'ai-progress-track'});
        const fillWidth = pct > 0
            ? Math.max(4, Math.round(TRACK_WIDTH * pct / 100))
            : 0;
        const branded = sev === 'ok' && color;
        const fill = new St.BoxLayout({
            style_class: branded
                ? 'ai-progress-fill'
                : `ai-progress-fill ai-fill-${sev}`,
        });
        let style = `width: ${fillWidth}px;`;
        if (branded) {
            style += ` background-gradient-direction: horizontal;` +
                ` background-gradient-start: ${color};` +
                ` background-gradient-end: ${lighten(color, 0.25)};`;
        }
        fill.set_style(style);
        track.add_child(fill);

        let detail;
        if (cost <= 0 && tokens <= 0 && extra) {
            detail = extra;  // idle window: skip the "$0.00 · 0 tokens" noise
        } else {
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
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'AI Token Monitor');
        this._extension = extension;

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        box.add_child(new St.Icon({
            // Speedometer-style gauge (power-profiles); themed fallback for
            // systems that don't ship it.
            gicon: Gio.ThemedIcon.new_with_default_fallbacks(
                'power-profile-performance-symbolic'),
            fallback_icon_name: 'org.gnome.SystemMonitor-symbolic',
            style_class: 'system-status-icon',
        }));
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
                const budget = this._budget(budgetKey);
                if (!(budget > 0))
                    continue;
                const cost = this._toolData(period, id).cost_usd;
                const pct = cost / budget * 100;
                const bucket = pct >= 100 ? 3 : pct >= 90 ? 2 : pct >= 70 ? 1 : 0;
                const key = `${id}:${period}`;
                const prev = this._alertState.get(key) ?? 0;
                if (bucket > prev) {
                    Main.notify(
                        fmt(_('%s — %s at %s%%'),
                            style.label, label, Math.round(pct)),
                        fmt(_('%s of %s used'),
                            formatCost(cost), formatCost(budget)));
                }
                this._alertState.set(key, bucket);
            }
        }
    }

    _toolData(period, toolName) {
        const pData = this._snapshot?.[period];
        const tData = pData?.tools?.find(t => t.tool === toolName);
        return tData ?? {cost_usd: 0, total_tokens: 0};
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

    /** Highest usage across all limit bars, for the at-a-glance panel label. */
    _maxPressure() {
        let max = null;
        for (const id of this._activeTools()) {
            const prefix = toolStyle(id).prefix;
            for (const [period, budget] of [
                ['five_hours', this._budget(`${prefix}_5h`)],
                ['week', this._budget(`${prefix}_weekly`)],
            ]) {
                if (!(budget > 0))
                    continue;
                const pct = this._toolData(period, id).cost_usd / budget * 100;
                if (max === null || pct > max)
                    max = pct;
            }
        }
        return max;
    }

    _updatePanel() {
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
        const pressure = this._maxPressure();
        if (pressure === null) {
            this._label.text = '';
            return;
        }
        const pct = Math.min(999, Math.round(pressure));
        this._label.text = `${pct}%`;
        this._label.style_class =
            `ai-panel-label ai-text-${severityClass(pressure)}`;
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
                _('Use Claude Code, agy or Codex and it will appear here'),
                {reactive: false});
            hint.label.add_style_class_name('ai-progress-subtitle');
            this.menu.addMenuItem(hint);
            this._addSettingsItem();
            return;
        }

        active.forEach((id, index) => {
            if (index > 0)
                this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

            const style = toolStyle(id);
            const week = this._toolData('week', id);
            const fiveH = this._toolData('five_hours', id);

            // Header: colored dot + tool name, weekly spend on the right.
            const header = new PopupMenu.PopupBaseMenuItem({reactive: false});
            header.add_child(new St.Label({
                text: '●',
                style: `color: ${style.color};`,
                style_class: 'ai-tool-dot',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            header.add_child(new St.Label({
                text: style.label,
                style_class: 'ai-tool-name',
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER,
            }));
            header.add_child(new St.Label({
                text: fmt(_('%s / wk'), formatCost(week.cost_usd)),
                style_class: 'ai-tool-cost',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            this.menu.addMenuItem(header);

            // Burn rates from the short rolling windows (absent on old daemons).
            const hourRate = this._toolData('hour', id).cost_usd;
            const dayRate = this._toolData('day', id).cost_usd / 24;

            const budget5h = this._budget(`${style.prefix}_5h`);
            const budgetWk = this._budget(`${style.prefix}_weekly`);
            // Both windows are anchored to first use, so each bar's tail
            // counts down to its real reset (burn-rate ETA only appears if
            // the limit would be hit before then, tinted amber).
            //
            // Tools that meter independent model-family pools (Antigravity:
            // Gemini vs Claude & GPT) get one bar pair per pool, mirroring
            // the provider's own quota screen.
            const fiveGroups = fiveH.groups ?? [];
            const weekByKey = new Map((week.groups ?? []).map(g => [g.key, g]));
            if (fiveGroups.length >= 2) {
                for (const g5 of fiveGroups) {
                    const gw = weekByKey.get(g5.key) ??
                        {cost_usd: 0, total_tokens: 0};
                    // No per-pool burn rate: rate 0 keeps projections off.
                    const s = windowText(g5, budget5h, 0);
                    const w = windowText(gw, budgetWk, 0);
                    this.menu.addMenuItem(new ProgressBarRow(
                        `${_('Session (5h)')} · ${g5.label}`,
                        g5.cost_usd, budget5h, g5.total_tokens,
                        s.text, style.color, s.warn));
                    this.menu.addMenuItem(new ProgressBarRow(
                        `${_('Weekly')} · ${g5.label}`,
                        gw.cost_usd, budgetWk, gw.total_tokens,
                        w.text, style.color, w.warn));
                }
            } else {
                const session = windowText(fiveH, budget5h, hourRate);
                const weekly = windowText(week, budgetWk, dayRate);
                this.menu.addMenuItem(new ProgressBarRow(
                    _('Session (5h)'), fiveH.cost_usd, budget5h,
                    fiveH.total_tokens, session.text, style.color,
                    session.warn));
                this.menu.addMenuItem(new ProgressBarRow(
                    _('Weekly'), week.cost_usd, budgetWk, week.total_tokens,
                    weekly.text, style.color, weekly.warn));
            }

            // Top models this week, tucked into a collapsible row.
            const models = week.models ?? [];
            if (models.length) {
                const sub = new PopupMenu.PopupSubMenuMenuItem(_('By model'));
                for (const m of models) {
                    const row = new PopupMenu.PopupBaseMenuItem({reactive: false});
                    row.add_child(new St.Label({
                        text: m.model,
                        style_class: 'ai-model-name',
                        x_expand: true,
                    }));
                    row.add_child(new St.Label({
                        text: `${formatCost(m.cost_usd)}  ·  ${formatTokens(m.total_tokens)}`,
                        style_class: 'ai-model-cost',
                    }));
                    sub.menu.addMenuItem(row);
                }
                this.menu.addMenuItem(sub);
            }
        });

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addSparkline();
        const today = this._snapshot.today?.totals?.cost_usd ?? 0;
        const month = this._snapshot.month?.totals?.cost_usd ?? 0;
        const footer = new PopupMenu.PopupBaseMenuItem({reactive: false});
        footer.add_child(new St.Label({
            text: fmt(_('Today %s  ·  Month %s'),
                formatCost(today), formatCost(month)),
            style_class: 'ai-footer',
            x_expand: true,
        }));
        const updated = new Date((this._snapshot.updated ?? 0) * 1000);
        footer.add_child(new St.Label({
            text: fmt(_('Synced %s'),
                `${updated.getHours()}:${String(updated.getMinutes()).padStart(2, '0')}`),
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
            const key = `${date.getFullYear()}-` +
                `${String(date.getMonth() + 1).padStart(2, '0')}-` +
                `${String(date.getDate()).padStart(2, '0')}`;
            days.push({date, data: byDay.get(key), today: i === 0});
        }
        const max = Math.max(...days.map(d => d.data?.cost_usd ?? 0), 0.01);
        const tools = this._activeTools();

        const title = new PopupMenu.PopupBaseMenuItem({reactive: false});
        title.add_child(new St.Label({
            text: _('Last 7 days'),
            style_class: 'ai-progress-title',
            x_expand: true,
        }));
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
