%global ext_uuid ai-token-monitor@theyisuskill.github.io
%global dbus_name io.github.theyisuskill.AITokenMonitor

Name:           ai-token-monitor
Version:        0.1.0
Release:        1%{?dist}
Summary:        Unified token-usage monitor for local AI CLI tools
License:        GPL-3.0-or-later
URL:            https://github.com/theyisuskill/ai-token-monitor
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros

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

# GNOME Shell extension
install -d %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}
install -pm0644 extension/extension.js extension/metadata.json \
    extension/stylesheet.css \
    %{buildroot}%{_datadir}/gnome-shell/extensions/%{ext_uuid}/

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
* Wed Jul 08 2026 theyisuskill <theyisuskill@gmail.com> - 0.1.0-1
- Initial package
