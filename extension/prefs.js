// AI Token Monitor — Preferences window (GNOME 45+, ESM)
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Thin D-Bus client: plans and budget mode live in the daemon's config
// (~/.config/ai-token-monitor/ui.yaml, written via SetSettings), so the
// gauge scaling adapts to whatever subscription the user actually has.

import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences, gettext as _} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const BUS_NAME = 'io.github.theyisuskill.AITokenMonitor';
const OBJECT_PATH = '/io/github/theyisuskill/AITokenMonitor';

const SETTINGS_IFACE = `
<node>
  <interface name="io.github.theyisuskill.AITokenMonitor1">
    <method name="GetSettings">
      <arg type="s" direction="out" name="settings"/>
    </method>
    <method name="SetSettings">
      <arg type="s" direction="in" name="settings"/>
      <arg type="s" direction="out" name="result"/>
    </method>
  </interface>
</node>`;

const SettingsProxy = Gio.DBusProxy.makeProxyWrapper(SETTINGS_IFACE);

const TOOL_LABELS = {
    claude_code: {title: 'Claude Code', subtitle: 'Anthropic subscription'},
    gemini_cli: {title: 'agy (Antigravity)', subtitle: 'Google AI subscription'},
    codex: {title: 'Codex', subtitle: 'ChatGPT subscription'},
};

function planLabel(plan) {
    // 'max_20x' -> 'Max 20x', 'pro' -> 'Pro'
    return plan.split('_')
        .map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

export default class AITokenMonitorPrefs extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const page = new Adw.PreferencesPage({
            title: _('Limits'),
            icon_name: 'power-profile-performance-symbolic',
        });
        window.add(page);

        const plansGroup = new Adw.PreferencesGroup({
            title: _('Subscription plans'),
            description: _('The 5-hour and weekly limit bars are scaled to your tier'),
        });
        page.add(plansGroup);

        const modeGroup = new Adw.PreferencesGroup({
            title: _('Budget mode'),
        });
        page.add(modeGroup);

        const displayGroup = new Adw.PreferencesGroup({
            title: _('Display'),
        });
        page.add(displayGroup);

        new SettingsProxy(Gio.DBus.session, BUS_NAME, OBJECT_PATH,
            (proxy, error) => {
                if (error) {
                    plansGroup.add(new Adw.ActionRow({
                        title: _('Daemon offline'),
                        subtitle: 'systemctl --user start ai-token-monitor',
                    }));
                    return;
                }
                proxy.GetSettingsRemote(([json]) => {
                    let settings;
                    try {
                        settings = JSON.parse(json);
                    } catch (e) {
                        return;
                    }
                    if (!settings?.presets)
                        return;
                    this._buildRows(proxy, settings, plansGroup, modeGroup);
                    this._buildDisplayRows(proxy, settings, displayGroup);
                });
            });
    }

    _buildDisplayRows(proxy, settings, group) {
        const ui = settings.ui ?? {};

        const panelModes = ['percent', 'icon', 'today'];
        const panelRow = new Adw.ComboRow({
            title: _('Top bar'),
            subtitle: _('What the panel indicator shows next to the icon'),
            model: Gtk.StringList.new([_('Percent used'), _('Icon only'), _("Today's spend")]),
        });
        const current = panelModes.indexOf(ui.panel);
        panelRow.selected = current >= 0 ? current : 0;
        panelRow.connect('notify::selected', () => {
            proxy.SetSettingsRemote(JSON.stringify({
                ui: {panel: panelModes[panelRow.selected]},
            }), () => {});
        });
        group.add(panelRow);

        const alertsRow = new Adw.SwitchRow({
            title: _('Limit notifications'),
            subtitle: _('Notify when a bar crosses 70%, 90% or 100%'),
            active: ui.alerts !== false,
        });
        alertsRow.connect('notify::active', () => {
            proxy.SetSettingsRemote(JSON.stringify({
                ui: {alerts: alertsRow.active},
            }), () => {});
        });
        group.add(alertsRow);
    }

    _buildRows(proxy, settings, plansGroup, modeGroup) {
        for (const [tool, preset] of Object.entries(settings.presets)) {
            const labels = TOOL_LABELS[tool] ?? {title: tool, subtitle: ''};
            const plans = preset.plans;
            const row = new Adw.ComboRow({
                title: labels.title,
                subtitle: labels.subtitle ? _(labels.subtitle) : '',
                model: Gtk.StringList.new(plans.map(planLabel)),
            });
            const current = plans.indexOf(settings.plans[tool]);
            row.selected = current >= 0 ? current : 0;
            // Connect after the initial selection so it doesn't echo back.
            row.connect('notify::selected', () => {
                proxy.SetSettingsRemote(JSON.stringify({
                    plans: {[tool]: plans[row.selected]},
                }), () => {});
            });
            plansGroup.add(row);
        }

        const modes = ['preset', 'auto'];
        const modeRow = new Adw.ComboRow({
            title: _('Bar scaling'),
            subtitle: _('Preset: use the plan tiers above · Auto: calibrate to your own peak usage'),
            model: Gtk.StringList.new([_('Preset'), _('Auto')]),
        });
        const mode = modes.indexOf(settings.budget_mode);
        modeRow.selected = mode >= 0 ? mode : 0;
        modeRow.connect('notify::selected', () => {
            proxy.SetSettingsRemote(JSON.stringify({
                budget_mode: modes[modeRow.selected],
            }), () => {});
        });
        modeGroup.add(modeRow);
    }
}
