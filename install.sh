#!/usr/bin/env bash
# Lautschrift installer — installs system dependencies, sets up permissions,
# creates a Python virtualenv, and downloads the Parakeet model.
#
#   ./install.sh             install everything, start manually afterwards
#   ./install.sh --service   additionally enable the systemd user service
#
# Supported package managers: dnf (Fedora), apt (Debian/Ubuntu),
# pacman (Arch), zypper (openSUSE). Anything else: see the manual steps
# in the README.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*"; }

NEED_RELOGIN=0

# --- 1. System packages ------------------------------------------------------
if command -v dnf >/dev/null 2>&1; then
    say "Installing system packages (dnf) …"
    sudo dnf install -y wl-clipboard ydotool pipewire-utils \
        python3 python3-gobject gtk3 xorg-x11-server-Xwayland sound-theme-freedesktop
elif command -v apt-get >/dev/null 2>&1; then
    say "Installing system packages (apt) …"
    sudo apt-get update
    sudo apt-get install -y wl-clipboard ydotool pipewire-bin \
        python3 python3-venv python3-gi gir1.2-gtk-3.0 libgtk-3-0 xwayland \
        sound-theme-freedesktop
elif command -v pacman >/dev/null 2>&1; then
    say "Installing system packages (pacman) …"
    sudo pacman -S --needed --noconfirm wl-clipboard ydotool pipewire \
        python python-gobject gtk3 xorg-xwayland sound-theme-freedesktop
elif command -v zypper >/dev/null 2>&1; then
    say "Installing system packages (zypper) …"
    sudo zypper --non-interactive install wl-clipboard ydotool pipewire-tools \
        python3 python3-gobject typelib-1_0-Gtk-3_0 libgtk-3-0 xwayland \
        sound-theme-freedesktop
else
    warn "Unknown package manager — please install these yourself:"
    warn "  wl-clipboard, ydotool, PipeWire CLI tools (pw-record/pw-play),"
    warn "  GTK3 + PyGObject, XWayland, Python 3.10+, freedesktop sound theme"
fi

# --- 2. Permissions: 'input' group + /dev/uinput ------------------------------
# evdev (global hotkey) needs read access to /dev/input/*  -> group 'input'.
if ! id -nG "$USER" | grep -qw input; then
    say "Adding $USER to the 'input' group (required for the global hotkey) …"
    sudo usermod -aG input "$USER"
    NEED_RELOGIN=1
fi

# ydotool (paste injection) needs write access to /dev/uinput. On many
# distros only root has that; this udev rule hands it to the 'input' group.
if [ "$(stat -c '%G' /dev/uinput 2>/dev/null || echo none)" != "input" ]; then
    say "Installing udev rule: /dev/uinput -> group 'input' …"
    echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
        | sudo tee /etc/udev/rules.d/80-lautschrift-uinput.rules >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger /dev/uinput 2>/dev/null || sudo modprobe uinput || true
fi

# --- 3. Python virtualenv ------------------------------------------------------
# --system-site-packages so the venv can see the distro's PyGObject (gi).
if [ ! -e .venv/bin/python ]; then
    say "Creating Python virtualenv …"
    if command -v uv >/dev/null 2>&1; then
        uv venv --system-site-packages .venv
    else
        python3 -m venv --system-site-packages .venv
    fi
fi
say "Installing Python dependencies …"
if command -v uv >/dev/null 2>&1; then
    uv pip install --python .venv/bin/python -r requirements.txt
else
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
fi

# --- 4. Model -------------------------------------------------------------------
say "Downloading the Parakeet TDT v3 model (~465 MB, skipped if present) …"
./scripts/download-model.sh

# --- 5. Optional: systemd user service -------------------------------------------
if [ "${1:-}" = "--service" ]; then
    say "Installing systemd user service …"
    mkdir -p "$HOME/.config/systemd/user"
    sed "s|@DIR@|$HERE|g" lautschrift.service \
        > "$HOME/.config/systemd/user/lautschrift.service"
    systemctl --user daemon-reload
    systemctl --user enable --now lautschrift.service
    say "Service running. Logs: journalctl --user -u lautschrift -f"
fi

# --- Done -------------------------------------------------------------------------
echo
say "Done."
if [ "$NEED_RELOGIN" = 1 ]; then
    warn "You were just added to the 'input' group — log out and back in"
    warn "once, otherwise the hotkey and paste injection will not work."
fi
if [ "${1:-}" != "--service" ]; then
    echo "Start manually with:   .venv/bin/python lautschrift.py"
    echo "Or enable autostart:   ./install.sh --service"
fi
echo "Default hotkey: Copilot key (F23). Different key? Find its code with"
echo "  .venv/bin/python detect_key.py   and set LAUT_TRIGGER_CODE."
