# Developer install (per-user, no root) and packaging helpers.

UUID        := ai-token-monitor@theyisuskill.github.io
DBUS_NAME   := io.github.theyisuskill.AITokenMonitor
EXT_DIR     := $(HOME)/.local/share/gnome-shell/extensions/$(UUID)
UNIT_DIR    := $(HOME)/.config/systemd/user
DBUS_DIR    := $(HOME)/.local/share/dbus-1/services
BIN         := $(HOME)/.local/bin/ai-token-monitor

.PHONY: help install-user install-daemon install-extension install-services \
        uninstall-user enable restart pack srpm copr check

help:
	@echo "Targets:"
	@echo "  install-user      daemon (pipx) + extension + systemd/D-Bus units"
	@echo "  uninstall-user    remove everything installed by install-user"
	@echo "  enable            enable the GNOME extension"
	@echo "  restart           restart the daemon"
	@echo "  pack              build the extension zip for extensions.gnome.org"
	@echo "  srpm              build a source RPM from packaging/*.spec"
	@echo "  copr              submit the source RPM to Copr (see packaging/COPR.md)"
	@echo "  check             quick syntax/backfill smoke test"

install-user: install-daemon install-services install-extension
	@echo ""
	@echo "Done. Now run:"
	@echo "  systemctl --user daemon-reload && systemctl --user enable --now ai-token-monitor.service"
	@echo "  make enable   # then log out/in on Wayland"

install-daemon:
	pipx install --force --system-site-packages ./daemon

install-services:
	install -d $(UNIT_DIR) $(DBUS_DIR)
	sed 's|/usr/bin/ai-token-monitor|$(BIN)|' data/ai-token-monitor.service \
	    > $(UNIT_DIR)/ai-token-monitor.service
	sed 's|/usr/bin/ai-token-monitor|$(BIN)|' data/$(DBUS_NAME).service \
	    > $(DBUS_DIR)/$(DBUS_NAME).service

install-extension:
	install -d $(EXT_DIR) $(EXT_DIR)/icons
	install -m0644 extension/extension.js extension/prefs.js \
	    extension/metadata.json extension/stylesheet.css $(EXT_DIR)/
	install -m0644 extension/icons/*.svg extension/icons/NOTICE $(EXT_DIR)/icons/
	@for po in extension/po/*.po; do \
	    lang=$$(basename $$po .po); \
	    install -d $(EXT_DIR)/locale/$$lang/LC_MESSAGES; \
	    msgfmt $$po -o $(EXT_DIR)/locale/$$lang/LC_MESSAGES/$(UUID).mo; \
	done

enable:
	gnome-extensions enable $(UUID)

restart:
	systemctl --user daemon-reload
	systemctl --user restart ai-token-monitor.service

uninstall-user:
	-systemctl --user disable --now ai-token-monitor.service
	-gnome-extensions disable $(UUID)
	-pipx uninstall ai-token-monitor
	rm -rf $(EXT_DIR)
	rm -f $(UNIT_DIR)/ai-token-monitor.service $(DBUS_DIR)/$(DBUS_NAME).service

pack:
	gnome-extensions pack --force --podir=po --extra-source=icons extension/
	@echo "-> $(UUID).shell-extension.zip"

srpm:
	rpmbuild -bs packaging/ai-token-monitor.spec \
	    --define "_sourcedir $(PWD)" --define "_srcrpmdir $(PWD)/packaging"

# Source0 is the GitHub tarball for the tag in Version:, so tag and push first.
copr: srpm
	copr-cli build ai-token-monitor $(lastword $(sort $(wildcard packaging/*.src.rpm)))

check:
	python3 -m compileall -q daemon/src
	PYTHONPATH=daemon/src python3 -m ai_token_monitor --backfill --verbose
