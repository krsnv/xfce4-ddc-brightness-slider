#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_PATH="/usr/local/bin/ddc-brightness-slider.py"
DESKTOP_PATH="$HOME/.config/autostart/ddc-brightness-slider.desktop"
APPS_PATH="$HOME/.local/share/applications/ddc-brightness-slider.desktop"

echo "== XFCE4 DDC Brightness Slider Installer =="
echo

echo "[1/5] Checking dependencies..."
MISSING=""

if ! command -v ddccontrol &>/dev/null; then
    MISSING="$MISSING ddccontrol"
fi

if ! python3 -c "import gi; gi.require_version('Gtk', '3.0')" 2>/dev/null; then
    MISSING="$MISSING python3-gi"
fi

if [ -n "$MISSING" ]; then
    echo "  Missing packages:$MISSING"
    echo "  Installing..."
    sudo apt-get update -qq
    sudo apt-get install -y $MISSING gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
else
    echo "  All dependencies satisfied."
fi

echo "[2/5] Checking I2C permissions..."

if ! lsmod | grep -q i2c_dev; then
    echo "  Loading i2c-dev kernel module..."
    sudo modprobe i2c-dev
    echo "i2c-dev" | sudo tee /etc/modules-load.d/i2c-dev.conf > /dev/null
fi

if ! getent group i2c > /dev/null 2>&1; then
    echo "  Creating 'i2c' group..."
    sudo groupadd i2c
fi

UDEV_RULE="/etc/udev/rules.d/99-i2c.rules"
if [ ! -f "$UDEV_RULE" ]; then
    echo "  Creating udev rule for I2C device permissions..."
    echo 'KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"' | sudo tee "$UDEV_RULE" > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "  Created $UDEV_RULE"
fi

if ! groups "$USER" | grep -q '\bi2c\b'; then
    echo "  Adding $USER to 'i2c' group..."
    sudo usermod -aG i2c "$USER"
    NEED_RELOGIN=1
    echo "  You'll need to log out and back in for group changes to take effect."
    echo "  (or run: newgrp i2c)"
else
    echo "  User is already in 'i2c' group."
fi

echo "[3/5] Installing to $INSTALL_PATH ..."
sudo cp "$SCRIPT_DIR/ddc-brightness-slider.py" "$INSTALL_PATH"
sudo chmod +x "$INSTALL_PATH"

echo "[4/5] Setting up autostart..."
mkdir -p "$(dirname "$DESKTOP_PATH")"
cp "$SCRIPT_DIR/ddc-brightness-slider.desktop" "$DESKTOP_PATH"

mkdir -p "$(dirname "$APPS_PATH")"
cp "$SCRIPT_DIR/ddc-brightness-slider.desktop" "$APPS_PATH"

echo "[5/5] Detecting monitors on I2C bus..."
echo
for dev in /dev/i2c-*; do
    echo -n "  Probing $dev ... "
    if ddccontrol -r 0x10 "dev:$dev" 2>/dev/null | grep -q '+/'; then
        VAL=$(ddccontrol -r 0x10 "dev:$dev" 2>/dev/null | grep -oP '\+/\K\d+')
        echo "Monitor found! Current brightness: ${VAL}%"
        FOUND_DEV="$dev"
    else
        echo "—"
    fi
done

echo
echo "Installation complete"
echo
if [ -n "$FOUND_DEV" ]; then
    echo "Detected monitor on: $FOUND_DEV"
    if [ "$FOUND_DEV" != "/dev/i2c-3" ]; then
        echo "  Note: default device is /dev/i2c-3, but your monitor is on $FOUND_DEV"
        echo "  Run with: ddc-brightness-slider.py --device $FOUND_DEV"
    fi
fi
echo
echo "Usage:"
echo "  ddc-brightness-slider.py                      # Tray icon (default)"
echo "  ddc-brightness-slider.py --standalone         # Floating window"
echo "  ddc-brightness-slider.py --device /dev/i2c-5  # Different I2C bus"
echo
echo "The app will auto-start on next login."
echo "To launch now:  ddc-brightness-slider.py &"
