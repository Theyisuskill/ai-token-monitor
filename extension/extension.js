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
const REFRESH_INTERVAL_S = 120;

const TOOL_LABELS = {
    claude_code: 'Claude Code',
    gemini_cli: 'agy',
};

function toolLabel(name) {
    return TOOL_LABELS[name] ??
        name.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
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

const ProgressBarRow = GObject.registerClass(
class ProgressBarRow extends PopupMenu.PopupBaseMenuItem {
    _init(title, cost, budget, resetText) {
        super._init({reactive: false});
        
        let percentage = 0;
        if (budget > 0) {
            percentage = Math.min(100, Math.max(0, (cost / budget) * 100));
        }

        const vbox = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'ai-progress-container',
            style: 'margin-left: 12px; margin-right: 12px;'
        });

        // Top row: Title and Percentage
        const titleRow = new St.BoxLayout();
        const titleLabel = new St.Label({
            text: title,
            style_class: 'ai-progress-title',
            x_expand: true
        });
        const percentLabel = new St.Label({
            text: budget > 0 ? `${Math.round(percentage)}% used` : `${formatCost(cost)}`,
            style_class: 'ai-progress-percent'
        });
        titleRow.add_child(titleLabel);
        titleRow.add_child(percentLabel);
        
        // Progress bar track
        const track = new St.BoxLayout({
            style_class: 'ai-progress-track',
            y_expand: true,
            x_expand: true
        });
        
        // Progress bar fill (we use width as a percentage via custom drawing or fixed size)
        // Since St widgets don't support fractional width simply via CSS percentage,
        // we set a custom width based on parent allocation, or use a fixed width.
        // For simplicity in extensions, we often use inline styles.
        const fill = new St.BoxLayout({
            style_class: percentage >= 100 ? 'ai-progress-fill ai-progress-full' : 'ai-progress-fill'
        });
        // We set inline style for the fill width. St.Widget inline_style is supported in modern GNOME.
        fill.set_style(`width: ${percentage}%;`);
        
        track.add_child(fill);
        
        // Subtitle (Resets info)
        const subtitleLabel = new St.Label({
            text: resetText,
            style_class: 'ai-progress-subtitle'
        });

        vbox.add_child(titleRow);
        vbox.add_child(track);
        vbox.add_child(subtitleLabel);
        
        this.add_child(vbox);
    }
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'AI Token Monitor');

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        box.add_child(new St.Icon({
            icon_name: 'utilities-system-monitor-symbolic',
            style_class: 'system-status-icon',
        }));
        this._label = new St.Label({
            text: '…',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'ai-token-monitor-label',
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
        // Minimalist top bar: just the icon, no text label
        this._label.text = '';
        this._rebuildMenu();
    }

    _rebuildMenu() {
        this.menu.removeAll();

        if (!this._snapshot) {
            this.menu.addMenuItem(new PopupMenu.PopupMenuItem(
                'Daemon offline...', {reactive: false}));
            this._addActions();
            return;
        }

        const getToolData = (period, toolName) => {
            const pData = this._snapshot[period];
            if (!pData || !pData.tools)
                return { cost_usd: 0, total_tokens: 0 };
            const tData = pData.tools.find(t => t.tool === toolName);
            return tData || { cost_usd: 0, total_tokens: 0 };
        };

        const budgets = this._snapshot.budgets || {};
        const tools = ['claude_code', 'gemini_cli'];

        tools.forEach((tool, index) => {
            if (index > 0) {
                this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            }

            const displayName = toolLabel(tool);
            const header = new PopupMenu.PopupMenuItem(displayName.toUpperCase(), { reactive: false });
            const color = tool === 'claude_code' ? '#ff8866' : '#66b3ff';
            header.label.set_style(`font-weight: bold; color: ${color}; font-size: 13px;`);
            this.menu.addMenuItem(header);

            const last5h = getToolData('five_hours', tool);
            const weekly = getToolData('week', tool);

            // Budgets come resolved from the daemon (plan-aware). These fallbacks
            // only apply if talking to an older daemon; they match the lowest tier.
            let limit5h = tool === 'claude_code' ? (budgets.claude_5h ?? 15.0) : (budgets.gemini_5h ?? 3.0);
            let limitWeekly = tool === 'claude_code' ? (budgets.claude_weekly ?? 75.0) : (budgets.gemini_weekly ?? 8.0);

            // Five Hour Limit
            this.menu.addMenuItem(new ProgressBarRow(
                'Five Hour Limit', 
                last5h.cost_usd, 
                limit5h,
                `Used: ${formatCost(last5h.cost_usd)} of ${formatCost(limit5h)} (${formatTokens(last5h.total_tokens)} tokens)`
            ));

            // Weekly Limit
            this.menu.addMenuItem(new ProgressBarRow(
                'Weekly Limit', 
                weekly.cost_usd, 
                limitWeekly,
                `Used: ${formatCost(weekly.cost_usd)} of ${formatCost(limitWeekly)} (${formatTokens(weekly.total_tokens)} tokens)`
            ));
        });

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addActions();
    }

    _addActions() {
        const refresh = new PopupMenu.PopupMenuItem('Refresh Sync');
        refresh.connect('activate', () => {
            if (!this._proxy)
                return;
            this._proxy.RefreshRemote((result, error) => {
                if (this._destroyed || error)
                    return;
                this._applySnapshot(result[0]);
            }, this._cancellable);
        });
        this.menu.addMenuItem(refresh);
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
