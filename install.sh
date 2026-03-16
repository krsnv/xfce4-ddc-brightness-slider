#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_PATH="/usr/local/bin/ddc-brightness-slider.py"
DESKTOP_PATH="$HOME/.config/autostart/ddc-brightness-slider.desktop"
APPS_PATH="$HOME/.local/share/applications/ddc-brightness-slider.desktop"

# Detect distro
detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        case "$ID" in
            arch|manjaro|endeavouros|garuda|artix|cachyos)
                echo "arch"
                ;;
            debian|ubuntu|linuxmint|pop|elementary|zorin)
                echo "debian"
                ;;
            *)
                if [ -n "$ID_LIKE" ]; then
                    case "$ID_LIKE" in
                        *arch*) echo "arch" ;;
                        *debian*|*ubuntu*) echo "debian" ;;
                        *) echo "unknown" ;;
                    esac
                else
                    echo "unknown"
                fi
                ;;
        esac
    elif command -v pacman &>/dev/null; then
        echo "arch"
    elif command -v apt-get &>/dev/null; then
        echo "debian"
    else
        echo "unknown"
    fi
}

DISTRO=$(detect_distro)

echo "== DDC Brightness Slider Installer =="
echo "  Detected distro family: $DISTRO"
echo

install_packages_arch() {
    local pkgs=("$@")
    # Split into repo and AUR packages
    local repo_pkgs=()
    local aur_pkgs=()
    for pkg in "${pkgs[@]}"; do
        if pacman -Si "$pkg" &>/dev/null; then
            repo_pkgs+=("$pkg")
        else
            aur_pkgs+=("$pkg")
        fi
    done
    if [ ${#repo_pkgs[@]} -gt 0 ]; then
        sudo pacman -S --needed --noconfirm "${repo_pkgs[@]}"
    fi
    if [ ${#aur_pkgs[@]} -gt 0 ]; then
        if command -v yay &>/dev/null; then
            yay -S --needed --noconfirm "${aur_pkgs[@]}"
        elif command -v paru &>/dev/null; then
            paru -S --needed --noconfirm "${aur_pkgs[@]}"
        else
            echo "  AUR packages needed: ${aur_pkgs[*]}"
            echo "  Please install an AUR helper (yay or paru) or install them manually."
            exit 1
        fi
    fi
}

install_packages_debian() {
    sudo apt-get update -qq
    sudo apt-get install -y "$@"
}

echo "[1/5] Checking dependencies..."
MISSING_CMDS=""
MISSING_PY_GI=0

if ! command -v ddccontrol &>/dev/null; then
    MISSING_CMDS="$MISSING_CMDS ddccontrol"
fi

if ! python3 -c "import gi; gi.require_version('Gtk', '3.0')" 2>/dev/null; then
    MISSING_PY_GI=1
fi

if [ -n "$MISSING_CMDS" ] || [ "$MISSING_PY_GI" -eq 1 ]; then
    echo "  Missing dependencies detected. Installing..."
    case "$DISTRO" in
        arch)
            PKGS=()
            [[ "$MISSING_CMDS" == *ddccontrol* ]] && PKGS+=("ddccontrol")
            [ "$MISSING_PY_GI" -eq 1 ] && PKGS+=("python-gobject" "gtk3" "libayatana-appindicator")
            install_packages_arch "${PKGS[@]}"
            ;;
        debian)
            PKGS=""
            [[ "$MISSING_CMDS" == *ddccontrol* ]] && PKGS="$PKGS ddccontrol"
            [ "$MISSING_PY_GI" -eq 1 ] && PKGS="$PKGS python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1"
            install_packages_debian $PKGS
            ;;
        *)
            echo "  Unsupported distro. Please install manually:"
            echo "    - ddccontrol"
            echo "    - python3 GTK3 bindings (python-gobject / python3-gi)"
            echo "    - libayatana-appindicator"
            exit 1
            ;;
    esac
else
    echo "  All dependencies satisfied."
fi

if ! command -v redshift &>/dev/null; then
    echo "  Optional: 'redshift' not found (needed for color temperature presets)."
    read -rp "  Install redshift? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        case "$DISTRO" in
            arch)   install_packages_arch redshift ;;
            debian) install_packages_debian redshift ;;
            *)      echo "  Please install redshift manually." ;;
        esac
    fi
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
