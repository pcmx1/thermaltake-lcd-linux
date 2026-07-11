#!/usr/bin/env bash
# install.sh — set up Thermaltake LCD Linux controllers
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
START_SERVICES=1

if [ "${1:-}" = "--no-start" ]; then
    START_SERVICES=0
elif [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    echo "Usage: bash install.sh [--no-start]"
    echo
    echo "  --no-start  Install dependencies, udev rules, and user services,"
    echo "              but do not enable or start LCD services."
    exit 0
elif [ $# -gt 0 ]; then
    echo "Unknown option: $1"
    echo "Usage: bash install.sh [--no-start]"
    exit 2
fi

echo "=== Thermaltake LCD Linux installer ==="
echo

# 1. Python dependencies
echo "[1/4] Installing Python dependencies in a local virtualenv..."
if ! python3 -m venv "$VENV_DIR"; then
    echo
    echo "Could not create a Python virtualenv."
    echo "On Ubuntu, install venv support with:"
    echo "  sudo apt install python3-venv"
    exit 1
fi
"$VENV_DIR/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"

# 2. udev rules
echo "[2/4] Installing udev rules (requires sudo)..."
sudo cp "$REPO_DIR/udev/99-thermaltake-lcd.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "      Udev rules installed."
echo "      Make sure your user is in the 'plugdev' group:"
echo "        sudo usermod -aG plugdev \$USER  (then log out and back in)"

# 3. systemd user services
echo "[3/4] Installing systemd user services..."
mkdir -p ~/.config/systemd/user

# Patch ExecStart paths to point at the actual repo location
sed "s|%h/thermaltake-lcd-linux|$REPO_DIR|g" \
    "$REPO_DIR/systemd/tt-lcd-rc.service" \
    | sed "s|/usr/bin/python3|$VENV_DIR/bin/python|g" \
    > ~/.config/systemd/user/tt-lcd-rc.service

sed "s|%h/thermaltake-lcd-linux|$REPO_DIR|g" \
    "$REPO_DIR/systemd/tt-lcd-aio.service" \
    | sed "s|/usr/bin/python3|$VENV_DIR/bin/python|g" \
    > ~/.config/systemd/user/tt-lcd-aio.service

systemctl --user daemon-reload

# 4. Enable whichever devices are present
if [ "$START_SERVICES" -eq 1 ]; then
    echo "[4/4] Enabling services for detected devices..."
else
    echo "[4/4] Checking detected devices without starting services..."
fi
ENABLED=0

if "$VENV_DIR/bin/python" -c "
import glob, os, sys
for p in glob.glob('/sys/class/hidraw/hidraw*'):
    try:
        uevent = open(os.path.join(p,'device','uevent')).read().upper()
        patterns = (
            '264A:232A', '264A:233D',
            '0000264A:0000232A', '0000264A:0000233D',
            'V0000264AP0000232A', 'V0000264AP0000233D',
        )
        if any(pattern in uevent for pattern in patterns):
            sys.exit(0)
    except: pass
sys.exit(1)
" 2>/dev/null; then
    if [ "$START_SERVICES" -eq 1 ]; then
        systemctl --user enable --now tt-lcd-rc.service
        echo "      RC/Bar 3.9\" service enabled and started."
        ENABLED=$((ENABLED+1))
    else
        echo "      RC/Bar 3.9\" detected. Start manually with:"
        echo "        systemctl --user start tt-lcd-rc.service"
    fi
else
    echo "      RC/Bar 3.9\" not detected — skipping (enable manually if needed)."
fi

if "$VENV_DIR/bin/python" -c "
import glob, os, sys
for p in glob.glob('/sys/class/hidraw/hidraw*'):
    try:
        uevent = open(os.path.join(p,'device','uevent')).read().upper()
        patterns = (
            '264A:2328', '264A:233C',
            '0000264A:00002328', '0000264A:0000233C',
            'V0000264AP00002328', 'V0000264AP0000233C',
        )
        if any(pattern in uevent for pattern in patterns):
            sys.exit(0)
    except: pass
sys.exit(1)
" 2>/dev/null; then
    if [ "$START_SERVICES" -eq 1 ]; then
        systemctl --user enable --now tt-lcd-aio.service
        echo "      AIO/Round service enabled and started."
        ENABLED=$((ENABLED+1))
    else
        echo "      AIO/Round detected. Start manually with:"
        echo "        systemctl --user start tt-lcd-aio.service"
    fi
else
    echo "      AIO/Round not detected — skipping (enable manually if needed)."
fi

echo
if [ "$START_SERVICES" -eq 0 ]; then
    echo "Installed without starting services."
    echo "When ready, start one display first and watch logs:"
    echo "  systemctl --user start tt-lcd-aio.service"
    echo "  journalctl --user -u tt-lcd-aio.service -f"
    echo
    echo "For the 3.9\" bar display:"
    echo "  systemctl --user start tt-lcd-rc.service"
    echo "  journalctl --user -u tt-lcd-rc.service -f"
elif [ $ENABLED -eq 0 ]; then
    echo "No Thermaltake LCD devices detected."
    echo "Connect your device, check udev rules, and start the service manually:"
    echo "  systemctl --user start tt-lcd-rc.service"
    echo "  systemctl --user start tt-lcd-aio.service"
else
    echo "Done! $ENABLED service(s) running."
    echo "Logs: journalctl --user -u tt-lcd-rc.service -f"
    echo "      journalctl --user -u tt-lcd-aio.service -f"
fi
