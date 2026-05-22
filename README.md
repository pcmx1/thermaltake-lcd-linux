# thermaltake-lcd-linux

Native Linux drivers for Thermaltake LCD displays — no Windows, no VM, no TT RGB Plus required.

Displays live CPU/GPU/NVMe temperatures, CPU load, and RAM usage on:

| Device | Display | Script |
|---|---|---|
| Thermaltake RC Pro (Tower 500 / 900) | 3.9" rectangular, 480×128 | `tt-lcd-rc-pro.py` |
| Thermaltake AIO cooler LCD | Round, 480×480 | `tt-lcd-aio.py` |

![RC Pro 3.9" display](https://github.com/pcmx1/thermaltake-lcd-linux/raw/main/doc/rc-pro-preview.png) ![AIO round display](https://github.com/pcmx1/thermaltake-lcd-linux/raw/main/doc/aio-preview.png)

---

## How it works

These devices are standard USB HID devices. The Windows software sends JPEG frames over HID Output Reports (interrupt OUT endpoint). By reverse-engineering a USB capture, the full protocol was determined:

- **Init**: a sequence of HID Feature Reports (SET/GET) wakes the controller and configures the display resolution
- **Frame data**: the JPEG is split into 1016-byte chunks, each prefixed with an 8-byte header and written to the hidraw fd
- **Last-chunk flag**: byte 3 of the header must be `0x01` on the final chunk (with the actual trailing byte count in bytes 4–5); the display renders only after receiving this flag
- **Timing** (RC Pro only): chunks must be written on the same open fd immediately after the `0x1d` SET_REPORT — any gap causes the interrupt OUT endpoint to NAK and time out

Both scripts are pure Python, require no kernel modules or special privileges beyond `plugdev` group membership, and auto-detect the hidraw device by USB VID:PID.

### RC Pro init sequence
```
SET_REPORT 0x18  →  SET_REPORT 0x1a  →  GET_REPORT 0x07
→  SET_REPORT 0x0c × 4  →  GET_REPORT 0x0f
→  SET_REPORT 0x1d  →  write chunks immediately
```

### AIO init sequence
```
SET_REPORT 0x1a  →  GET_REPORT 0x07
→  SET_REPORT 0x0c × 3  →  GET_REPORT 0x0f
→  write chunks immediately  (no 0x1d required)
```

### Chunk header format
```
Byte 0:    0x02        HID Output Report ID
Byte 1:    0x09        constant
Byte 2:    0x00        constant
Byte 3:    0x00/0x01   0 = normal chunk; 1 = last chunk (triggers render)
Bytes 4-5: LE uint16   1016 for normal chunks; actual trailing bytes for last chunk
Bytes 6-7: LE uint16   chunk index (0-based)
Bytes 8+:  1016 bytes  JPEG data (zero-padded on last chunk)
```

---

## Requirements

- Linux (tested on Ubuntu 24.04, kernel 6.x)
- Python 3.8+
- `Pillow` and `psutil` Python packages
- User in the `plugdev` group

### Sensor support

The scripts read temperatures via `psutil.sensors_temperatures()`. Out of the box:

| Sensor | psutil key | label |
|---|---|---|
| AMD CPU (k10temp) | `k10temp` | `Tctl` |
| AMD GPU (amdgpu) | `amdgpu` | `edge` |
| NVMe SSD | `nvme` | `Composite` |

For Intel CPUs or NVIDIA GPUs, edit the `get_temps()` function in the script — the psutil key and label will differ. Run `python3 -c "import psutil; print(psutil.sensors_temperatures())"` to see what's available on your system.

---

## Installation

```bash
git clone https://github.com/pcmx1/thermaltake-lcd-linux.git
cd thermaltake-lcd-linux
bash install.sh
```

The install script:
1. Installs Python dependencies (`pip install --user`)
2. Copies udev rules and reloads them (`sudo` required for this step only)
3. Installs and enables systemd user services for any detected devices

### Manual installation

```bash
# Python deps
pip install --user Pillow psutil

# udev rules (run once, requires sudo)
sudo cp udev/99-thermaltake-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# Add yourself to plugdev if not already a member
sudo usermod -aG plugdev $USER   # log out and back in after this

# systemd user services
cp systemd/tt-lcd-rc.service  ~/.config/systemd/user/
cp systemd/tt-lcd-aio.service ~/.config/systemd/user/
systemctl --user daemon-reload

# Enable whichever device(s) you have
systemctl --user enable --now tt-lcd-rc.service
systemctl --user enable --now tt-lcd-aio.service
```

### Running without systemd

```bash
python3 tt-lcd-rc-pro.py
python3 tt-lcd-aio.py
```

---

## Configuration

Edit the constants near the top of each script:

| Variable | Default | Description |
|---|---|---|
| `INTERVAL` | `2` | Seconds between frame updates |
| `FONT_L` | DejaVu Sans Bold | Bold font path (temperatures) |
| `FONT_R` | DejaVu Sans | Regular font path (labels, bars) |

---

## Useful commands

```bash
# Check service status
systemctl --user status tt-lcd-rc.service tt-lcd-aio.service

# Follow live logs
journalctl --user -u tt-lcd-rc.service -f
journalctl --user -u tt-lcd-aio.service -f

# Restart after editing a script
systemctl --user restart tt-lcd-rc.service
systemctl --user restart tt-lcd-aio.service
```

---

## Troubleshooting

**Device not found**
```
ERROR: Thermaltake RC Pro (USB 264a:232a) not found.
```
Check that the udev rules are installed, your user is in `plugdev`, and you have logged out and back in since adding yourself to the group.

**ETIMEDOUT on write**
The RC Pro's interrupt OUT endpoint timed out. This usually means the init sequence was interrupted or a previous run left the device in a bad state. The script will attempt a `USBDEVFS_RESET` automatically after two consecutive errors. You can also unplug and replug the USB cable.

**Permission denied on /dev/hidraw***
Your user is not in the `plugdev` group, or the udev rules haven't been reloaded. Run `sudo udevadm control --reload-rules && sudo udevadm trigger`, then confirm with `groups`.

**Display stays blank**
Check that the last-chunk flag is being sent (it should be in this version). Try restarting the service — if the device was in an intermediate state from a previous run, the reset on the second error will recover it.

**Wrong sensors / N/A displayed**
Run `python3 -c "import psutil; import pprint; pprint.pprint(psutil.sensors_temperatures())"` and update the `get_temps()` function to match your hardware's key and label names.

---

## Contributing

Pull requests welcome — especially for:
- Intel CPU / NVIDIA GPU sensor support
- Other Thermaltake LCD models (different VID:PID or display resolution)
- Improved layouts or configurable themes

---

## License

MIT — see [LICENSE](LICENSE).
