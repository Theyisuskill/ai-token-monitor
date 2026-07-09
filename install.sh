#!/bin/bash
set -e

# Emulate `make install-user` without pipx or make
echo "Installing ai-token-monitor..."

UUID="ai-token-monitor@franycraft.github.io"
DBUS_NAME="io.github.franycraft.AITokenMonitor"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
UNIT_DIR="$HOME/.config/systemd/user"
DBUS_DIR="$HOME/.local/share/dbus-1/services"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/ai-token-monitor"
APP_DIR="$HOME/.local/share/ai-token-monitor-app"

# 1. Install daemon files
echo "-> Installing daemon to $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
cp -r daemon/src/* "$APP_DIR/"

echo "-> Creating executable wrapper at $BIN"
mkdir -p "$BIN_DIR"
cat > "$BIN" << 'EOF'
#!/bin/bash
export PYTHONPATH="$HOME/.local/share/ai-token-monitor-app:$PYTHONPATH"
exec python3 -m ai_token_monitor "$@"
EOF
chmod +x "$BIN"

# 2. Install services
echo "-> Installing systemd and D-Bus services"
mkdir -p "$UNIT_DIR" "$DBUS_DIR"
sed "s|/usr/bin/ai-token-monitor|$BIN|" data/ai-token-monitor.service > "$UNIT_DIR/ai-token-monitor.service"
sed "s|/usr/bin/ai-token-monitor|$BIN|" data/$DBUS_NAME.service > "$DBUS_DIR/$DBUS_NAME.service"

# 3. Install GNOME extension
echo "-> Installing GNOME extension"
mkdir -p "$EXT_DIR"
install -m0644 extension/extension.js extension/metadata.json extension/stylesheet.css "$EXT_DIR/"

echo ""
echo "Installation complete! To activate everything, run:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now ai-token-monitor.service"
echo "  gnome-extensions enable $UUID"
echo "  (If you are on Wayland, you must log out and log back in to load the extension)"
