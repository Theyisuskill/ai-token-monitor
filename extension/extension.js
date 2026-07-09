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

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
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

const TOOLS = [
    {
        id: 'claude_code',
        label: 'Claude Code',
        color: '#ff8866',
        budget5h: 'claude_5h',
        budgetWeekly: 'claude_weekly',
        // Fallbacks match the daemon's lowest plan tier (only used when
        // talking to an older daemon that doesn't resolve budgets).
        fallback5h: 15.0,
        fallbackWeekly: 75.0,
    },
    {
        id: 'gemini_cli',
        label: 'agy',
        color: '#66b3ff',
        budget5h: 'gemini_5h',
        budgetWeekly: 'gemini_weekly',
        fallback5h: 3.0,
        fallbackWeekly: 8.0,
    },
];

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

function severityClass(pct) {
    if (pct >= 90)
        return 'danger';
    if (pct >= 70)
        return 'warn';
    return 'ok';
}

const ProgressBarRow = GObject.registerClass(
class ProgressBarRow extends PopupMenu.PopupBaseMenuItem {
    _init(title, cost, budget, tokens) {
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
        const track = new St.BoxLayout({style_class: 'ai-progress-track'});
        const fillWidth = pct > 0
            ? Math.max(4, Math.round(TRACK_WIDTH * pct / 100))
            : 0;
        const fill = new St.BoxLayout({
            style_class: `ai-progress-fill ai-fill-${sev}`,
        });
        fill.set_style(`width: ${fillWidth}px;`);
        track.add_child(fill);

        const subtitle = new St.Label({
            text: budget > 0
                ? `${formatCost(cost)} of ${formatCost(budget)}  ·  ${formatTokens(tokens)} tokens`
                : `${formatTokens(tokens)} tokens`,
            style_class: 'ai-progress-subtitle',
        });

        vbox.add_child(titleRow);
        vbox.add_child(track);
        vbox.add_child(subtitle);
        this.add_child(vbox);
    }
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'AI Token Monitor');

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
        this._rebuildMenu();
    }

    _toolData(period, toolName) {
        const pData = this._snapshot?.[period];
        const tData = pData?.tools?.find(t => t.tool === toolName);
        return tData ?? {cost_usd: 0, total_tokens: 0};
    }

    _budget(key, fallback) {
        const v = this._snapshot?.budgets?.[key];
        return Number.isFinite(v) && v > 0 ? v : fallback;
    }

    /** Highest usage across all limit bars, for the at-a-glance panel label. */
    _maxPressure() {
        let max = null;
        for (const tool of TOOLS) {
            const pairs = [
                ['five_hours', this._budget(tool.budget5h, tool.fallback5h)],
                ['week', this._budget(tool.budgetWeekly, tool.fallbackWeekly)],
            ];
            for (const [period, budget] of pairs) {
                if (!(budget > 0))
                    continue;
                const pct = this._toolData(period, tool.id).cost_usd / budget * 100;
                if (max === null || pct > max)
                    max = pct;
            }
        }
        return max;
    }

    _updatePanel() {
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
            const offline = new PopupMenu.PopupMenuItem('Daemon offline',
                {reactive: false});
            this.menu.addMenuItem(offline);
            const hint = new PopupMenu.PopupMenuItem(
                'systemctl --user start ai-token-monitor', {reactive: false});
            hint.label.add_style_class_name('ai-progress-subtitle');
            this.menu.addMenuItem(hint);
            return;
        }

        TOOLS.forEach((tool, index) => {
            if (index > 0)
                this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

            const week = this._toolData('week', tool.id);
            const fiveH = this._toolData('five_hours', tool.id);

            // Header: colored dot + tool name, weekly spend on the right.
            const header = new PopupMenu.PopupBaseMenuItem({reactive: false});
            header.add_child(new St.Label({
                text: '●',
                style: `color: ${tool.color};`,
                style_class: 'ai-tool-dot',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            header.add_child(new St.Label({
                text: tool.label,
                style_class: 'ai-tool-name',
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER,
            }));
            header.add_child(new St.Label({
                text: `${formatCost(week.cost_usd)} / wk`,
                style_class: 'ai-tool-cost',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            this.menu.addMenuItem(header);

            this.menu.addMenuItem(new ProgressBarRow(
                'Session (5h)', fiveH.cost_usd,
                this._budget(tool.budget5h, tool.fallback5h),
                fiveH.total_tokens));
            this.menu.addMenuItem(new ProgressBarRow(
                'Weekly', week.cost_usd,
                this._budget(tool.budgetWeekly, tool.fallbackWeekly),
                week.total_tokens));
        });

        // Footer: calendar-period spend and last sync time.
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const today = this._snapshot.today?.totals?.cost_usd ?? 0;
        const month = this._snapshot.month?.totals?.cost_usd ?? 0;
        const footer = new PopupMenu.PopupBaseMenuItem({reactive: false});
        footer.add_child(new St.Label({
            text: `Today ${formatCost(today)}  ·  Month ${formatCost(month)}`,
            style_class: 'ai-footer',
            x_expand: true,
        }));
        const updated = new Date((this._snapshot.updated ?? 0) * 1000);
        footer.add_child(new St.Label({
            text: `Synced ${updated.getHours()}:${String(updated.getMinutes()).padStart(2, '0')}`,
            style_class: 'ai-footer',
        }));
        this.menu.addMenuItem(footer);
    }

    destroy() {
        this._destroyed = true;
        this._cancellable.cancel();
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
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
        this._indicator = new Indicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
