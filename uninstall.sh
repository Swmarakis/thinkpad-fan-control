#!/usr/bin/env bash
#
# Uninstaller for ThinkPad Fan Control.
# Run with: sudo ./uninstall.sh
#
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root:  sudo ./uninstall.sh" >&2
    exit 1
fi

echo "==> Stopping and disabling service"
systemctl disable --now thinkpad-fand.service 2>/dev/null || true
rm -f /etc/systemd/system/thinkpad-fand.service
systemctl daemon-reload

# Make sure the fan is back under firmware control.
echo "level auto" > /proc/acpi/ibm/fan 2>/dev/null || true

echo "==> Removing files"
rm -f /usr/bin/thinkpad-fand /usr/local/bin/thinkpad-fand
rm -f /usr/bin/thinkpad-fan-gui /usr/local/bin/thinkpad-fan-gui
rm -f /usr/share/applications/thinkpad-fan-control.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/thinkpad-fan-control.svg
rm -f /usr/share/icons/hicolor/256x256/apps/thinkpad-fan-control.png
rm -f /usr/share/pixmaps/thinkpad-fan-control.png
rm -rf /run/thinkpad-fan

# Remove the per-user "launch at login" entry, if present.
REAL_USER="${SUDO_USER:-}"
if [[ -n "$REAL_USER" ]]; then
    rm -f "/home/$REAL_USER/.config/autostart/thinkpad-fan-control.desktop"
fi

read -r -p "Also remove /etc/thinkpad-fan (your saved config)? [y/N] " ans
if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
    rm -rf /etc/thinkpad-fan
fi

echo "==> Uninstalled. (The modprobe fan_control option was left in place.)"
