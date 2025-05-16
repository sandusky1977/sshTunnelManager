#!/bin/bash
# Setup script for SSH Tunnel Manager

echo "Setting up SSH Tunnel Manager..."

# Make scripts executable
chmod +x ssh_tunnel_manager_launcher.py
chmod +x ssh_tunnel_manager_app.py

# Create desktop entry
DESKTOP_FILE="$HOME/.local/share/applications/ssh-tunnel-manager.desktop"

mkdir -p "$HOME/.local/share/applications"

# Get absolute path to script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Name=SSH Tunnel Manager
Comment=Manage SSH tunnels for remote access
Exec=$SCRIPT_DIR/ssh_tunnel_manager_launcher.py
Icon=network-server
Terminal=false
Type=Application
Categories=Network;RemoteAccess;
StartupNotify=false
X-GNOME-Autostart-enabled=true
EOF

# Make desktop file executable
chmod +x "$DESKTOP_FILE"

# Create autostart entry
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cp "$DESKTOP_FILE" "$AUTOSTART_DIR/"

echo "Setup complete! SSH Tunnel Manager has been installed."
echo "You can find it in your application menu or start it from the terminal:"
echo "python3 $SCRIPT_DIR/ssh_tunnel_manager_launcher.py"
echo ""
echo "The application will start automatically when you log in."