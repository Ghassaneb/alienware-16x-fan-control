#!/usr/bin/env bash
# Installer for alienware-16x-fan-control.
# Usage: sudo ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$DIR/fan_helper.sh"
APP="$DIR/fan_control.py"
SUDOERS_FILE=/etc/sudoers.d/fancontroller
MODULES_FILE=/etc/modules-load.d/acpi_call.conf

[ "$(id -u)" -eq 0 ] || { echo "Run this with sudo: sudo $0" >&2; exit 1; }

# The desktop entry must belong to the desktop user, not root.
TARGET_USER="${SUDO_USER:-$(stat -c '%U' "$DIR")}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[ -n "$TARGET_HOME" ] || { echo "Cannot resolve home for user $TARGET_USER" >&2; exit 1; }

echo "==> Target user: $TARGET_USER ($TARGET_HOME)"

echo "==> Installing dependencies"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 acpi-call-dkms
elif command -v pacman >/dev/null 2>&1; then
    pacman -S --needed --noconfirm acpi_call-lts python-gobject gtk4 libadwaita
else
    echo "Unsupported distro: install PyGObject, GTK4 and libadwaita GIR files manually." >&2
fi

echo "==> Loading acpi_call"
modprobe acpi_call
echo acpi_call > "$MODULES_FILE"
[ -e /proc/acpi/call ] || { echo "acpi_call did not create /proc/acpi/call" >&2; exit 1; }

echo "==> Installing helper permissions"
chmod +x "$HELPER"
cat > "$SUDOERS_FILE" <<EOF
$TARGET_USER ALL=(ALL) NOPASSWD: $HELPER
EOF
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null

echo "==> Installing desktop entry"
ICON_PATH="$TARGET_HOME/.local/share/icons/hicolor/scalable/apps/fan-control.svg"
DESKTOP_DIR="$TARGET_HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR" "$(dirname "$ICON_PATH")"
cat > "$ICON_PATH" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <circle cx="32" cy="32" r="26" fill="none" stroke="#3789dd" stroke-width="5"
          stroke-dasharray="122 41" stroke-linecap="round"
          transform="rotate(135 32 32)"/>
  <circle cx="32" cy="32" r="4" fill="#3789dd"/>
  <path d="M32 32 L32 14" stroke="#3789dd" stroke-width="4" stroke-linecap="round"/>
</svg>
SVG
cat > "$DESKTOP_DIR/fan-control.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Fan Control
Comment=Alienware fan speed and thermal profile control
Exec=/usr/bin/python3 $APP
Icon=$ICON_PATH
Terminal=false
Categories=System;Settings;HardwareSettings;
StartupNotify=true
StartupWMClass=com.hbwal.fancontrol
EOF
chown -R "$TARGET_USER": "$TARGET_HOME/.local/share/applications/fan-control.desktop" \
    "$TARGET_HOME/.local/share/icons/hicolor/scalable/apps/fan-control.svg" 2>/dev/null || true

echo "==> Testing ACPI fan control"
if sudo -u "$TARGET_USER" sudo -n "$HELPER" probe; then
    echo "    helper runs with sudo (NOPASSWD ok)"
else
    echo "    warning: passwordless helper invocation failed" >&2
fi
if OUT="$(sudo -u "$TARGET_USER" sudo -n "$HELPER" profile balanced 2>&1)"; then
    echo "    ACPI method call ok (WMAX response: ${OUT:-empty})"
else
    echo "    warning: ACPI call failed: $OUT" >&2
    echo "    the WMI method may differ on this model — check the README discovery notes" >&2
fi

cat <<EOF

Done. Launch with:
    python3 $APP
or from the application menu ("Fan Control").

Notes:
  * acpi_call is now loaded at boot via $MODULES_FILE
  * sudoers rule: $SUDOERS_FILE (only $HELPER, no password)
  * kernel updates: acpi-call-dkms rebuilds automatically (dkms)
EOF
