#!/usr/bin/env bash
# install.sh — set up Thermaltake LCD Linux controllers
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Thermaltake LCD Linux installer ==="
echo

# 1. Python dependencies
echo "[1/4] Installing Python dependencies..."
pip install --user -r "$REPO_DIR/requirements.txt"

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
    > ~/.config/systemd/user/tt-lcd-rc.service

sed "s|%h/thermaltake-lcd-linux|$REPO_DIR|g" \
    "$REPO_DIR/systemd/tt-lcd-aio.service" \
    > ~/.config/systemd/user/tt-lcd-aio.service

systemctl --user daemon-reload

# 4. Enable whichever devices are present
echo "[4/4] Enabling services for detected devices..."
ENABLED=0

if python3 -c "
import glob, os, sys
for p in glob.glob('/sys/class/hidraw/hidraw*'):
    try:
        if '264A:232A' in open(os.path.join(p,'device','uevent')).read().upper():
            sys.exit(0)
    except: pass
sys.exit(1)
" 2>/dev/null; then
    systemctl --user enable --now tt-lcd-rc.service
    echo "      RC Pro 3.9\" service enabled and started."
    ENABLED=$((ENABLED+1))
else
    echo "      RC Pro 3.9\" not detected — skipping (enable manually if needed)."
fi

if python3 -c "
import glob, os, sys
for p in glob.glob('/sys/class/hidraw/hidraw*'):
    try:
        if '264A:2328' in open(os.path.join(p,'device','uevent')).read().upper():
            sys.exit(0)
    except: pass
sys.exit(1)
" 2>/dev/null; then
    systemctl --user enable --now tt-lcd-aio.service
    echo "      AIO service enabled and started."
    ENABLED=$((ENABLED+1))
else
    echo "      AIO not detected — skipping (enable manually if needed)."
fi

echo
if [ $ENABLED -eq 0 ]; then
    echo "No Thermaltake LCD devices detected."
    echo "Connect your device, check udev rules, and start the service manually:"
    echo "  systemctl --user start tt-lcd-rc.service"
    echo "  systemctl --user start tt-lcd-aio.service"
else
    echo "Done! $ENABLED service(s) running."
    echo "Logs: journalctl --user -u tt-lcd-rc.service -f"
    echo "      journalctl --user -u tt-lcd-aio.service -f"
fi
