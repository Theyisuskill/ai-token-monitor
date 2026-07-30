%global ext_uuid ai-token-monitor@theyisuskill.github.io
%global dbus_name io.github.theyisuskill.AITokenMonitor

Name:           ai-token-monitor
Version:        0.4.0
Release:        1%{?dist}
Summary:        Unified token-usage monitor for local AI CLI tools
License:        GPL-3.0-or-later
URL:            https://github.com/theyisuskill/ai-token-monitor
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros
# msgfmt, to compile the extension's gettext catalogs at build time.
BuildRequires:  gettext

Requires:       python3-gobject
Requires:       python3-dasbus
Requires:       python3-pyyaml
Requires:       dbus-common

%description
Lightweight daemon that watches the local logs of AI CLI tools (Claude Code,
Gemini CLI, ...) via inotify, normalizes token usage into a SQLite database,
and exposes consolidated metrics over the D-Bus session bus.

%package -n gnome-shell-extension-%{name}
Summary:        GNOME Shell top-bar widget for ai-token-monitor
Requires:       %{name} = %{version}-%{release}
Requires:       gnome-shell >= 45

%description -n gnome-shell-extension-%{name}
GNOME Shell extension that shows unified token usage and estimated cost of
local AI CLI tools in the top bar, fed by the ai-token-monitor daemon.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
cd daemon && %pyproject_buildrequires

%build
cd daemon && %pyproject_wheel

%install
cd daemon && %pyproject_install && cd ..
%pyproject_save_files ai_token_monitor

# systemd user unit + D-Bus activation
install -Dpm0644 data/ai-token-monitor.service \
    %{buildroot}%{_userunitdir}/ai-token-monitor.service
install -Dpm0644 data/%{dbus_name}.service \
    %{buildroot}%{_datadir}/dbus-1/services/%{dbus_name}.service

# example config
install -Dpm0644 data/config.example.yaml \
    %{buildroot}%{_docdir}/%{name}/config.example.yaml

# GNOME Shell extension. Must stay in sync with the Makefile's
# install-extension target: prefs.js (the Preferences window), icons/ (brand
# glyphs used by the tabs and the icon panel mode) and the compiled gettext
# catalogs are all load-bearing — an extension shipped without them silently
# loses Preferences, its icons and every translation.
install -d %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}/icons
install -pm0644 extension/extension.js extension/prefs.js \
    extension/metadata.json extension/stylesheet.css \
    %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}/
install -pm0644 extension/icons/*.svg extension/icons/NOTICE \
    %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}/icons/
for po in extension/po/*.po; do
    lang=$(basename "$po" .po)
    install -d %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}/locale/$lang/LC_MESSAGES
    msgfmt "$po" -o %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}/locale/$lang/LC_MESSAGES/%{ext_uuid}.mo
done

%post
%systemd_user_post ai-token-monitor.service

%preun
%systemd_user_preun ai-token-monitor.service

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%doc %{_docdir}/%{name}/config.example.yaml
%{_bindir}/ai-token-monitor
%{_userunitdir}/ai-token-monitor.service
%{_datadir}/dbus-1/services/%{dbus_name}.service

%files -n gnome-shell-extension-%{name}
%license LICENSE
%{_datadir}/gnome-shell/extensions/%{ext_uuid}/

%changelog
* Tue Jul 14 2026 theyisuskill <theyisuskill@gmail.com> - 0.4.0-1
- Summary-centric popup rework: KPI context, clickable Limits mini-bars, spend
  split, brand icons per provider, and an honest "updated Xm ago" footer
- Live-limit resilience: keep serving the last good real % through transient
  poll failures (stale marker), burn-rate depletion projection, Antigravity
  per-pool percentages, and plan auto-detection
- Daemon serves 14 days of daily_series for week-over-week comparison
- Stale/depletion cues and bar animations; complete Spanish translation

* Thu Jul 09 2026 theyisuskill <theyisuskill@gmail.com> - 0.3.0-1
- Anchor the 5h and weekly windows to each tool's real session start; both
  bars count down to the exact reset, and burn-rate projections only show
  when the limit lands before it
- Fresh-window notification when a session resets; --waybar output for
  Sway/Hyprland bars; optional retention_days pruning
- Spanish translation (gettext); readable 7-day sparkline with labeled
  calendar days; README screenshot

* Thu Jul 09 2026 theyisuskill <theyisuskill@gmail.com> - 0.2.0-1
- Rolling 5h/weekly windows matching provider limits; plan-aware budgets
  (preset per tier or auto-calibrated) with a Preferences window
- Antigravity: real model extraction from gen_metadata (accurate pricing);
  new --reparse migration; OpenAI Codex CLI adapter; dynamic tool sections
- Limit notifications, burn-rate projections, per-model breakdown,
  brand-colored bars and a stacked 7-day sparkline
- Test suite + CI

* Wed Jul 08 2026 theyisuskill <theyisuskill@gmail.com> - 0.1.0-1
- Initial package
