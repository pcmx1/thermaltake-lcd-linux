#!/usr/bin/env python3
"""
Thermaltake RC Pro 3.9" LCD — native Linux controller
======================================================
Display:  480 × 128 px (rectangular strip)
Device:   USB HID 264a:232a  →  /dev/hidraw*  (auto-detected)
Protocol: JPEG split into 1016-byte HID Output Report chunks

Init sequence (from USB capture analysis):
  SET_REPORT 0x18  →  SET_REPORT 0x1a  →  GET_REPORT 0x07
  →  SET_REPORT 0x0c × 4  →  GET_REPORT 0x0f
  →  SET_REPORT 0x1d  →  write chunks immediately (same fd, no gap)

Chunk header (8 bytes prepended to each 1016-byte JPEG slice):
  [0x02, 0x09, 0x00, flag, size_lo, size_hi, idx_lo, idx_hi]
  flag=0x00  normal chunk, size=1016
  flag=0x01  last chunk,   size=remaining JPEG bytes in this chunk
  The display renders only after receiving the last-chunk flag.
"""

import time
import io
import os
import sys
import glob
import fcntl
import psutil
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────
W, H     = 480, 128
CHUNK    = 1016
INTERVAL = 2          # seconds between frame updates

FONT_L = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT_R = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

USB_VID, USB_PID = 0x264a, 0x232a   # Thermaltake RC Pro

# ── HID ioctl numbers (/usr/include/linux/hidraw.h) ───────────────────────────
# _IOC(_IOC_WRITE|_IOC_READ, 'H', nr, size)  →  direction bits = 3
HIDIOCSFEATURE = lambda n: (3 << 30) | (n << 16) | (0x48 << 8) | 0x06
HIDIOCGFEATURE = lambda n: (3 << 30) | (n << 16) | (0x48 << 8) | 0x07

# ── Feature Report payloads (64 bytes each, report ID 0x03) ───────────────────
CMD_18      = bytes.fromhex('0318' + '00' * 62)           # wake LCD controller
CMD_1A      = bytes.fromhex('031a' + '00' * 62)           # enable display
CMD_0C_DIMS = bytes.fromhex(                               # configure 480×128
    '030c6400000000e00180000405000000' + '00' * 48)
CMD_0C_NEXT = bytes.fromhex('030c64ffffeaffffff' + '00' * 55)  # per-frame begin
CMD_1D      = bytes.fromhex('031d00ffffeaffffff' + '00' * 55)  # start transfer

assert all(len(c) == 64 for c in (CMD_18, CMD_1A, CMD_0C_DIMS, CMD_0C_NEXT, CMD_1D))


# ── Device discovery ──────────────────────────────────────────────────────────

def find_hidraw(vendor=USB_VID, product=USB_PID):
    """Return the /dev/hidraw* path for the given USB VID:PID, or None."""
    for path in glob.glob('/sys/class/hidraw/hidraw*'):
        try:
            uevent = open(os.path.join(path, 'device', 'uevent')).read()
            if f'{vendor:08X}:{product:08X}' in uevent.upper():
                return '/dev/' + os.path.basename(path)
        except Exception:
            pass
    return None


def find_usb_addr(vendor=USB_VID, product=USB_PID):
    """Return (bus, devnum) for USB reset, or (None, None)."""
    base = '/sys/bus/usb/devices'
    for entry in os.listdir(base):
        try:
            txt = open(os.path.join(base, entry, 'uevent')).read()
            if f'{vendor:04x}/{product:04x}' in txt:
                bus = int(open(os.path.join(base, entry, 'busnum')).read())
                dev = int(open(os.path.join(base, entry, 'devnum')).read())
                return bus, dev
        except Exception:
            pass
    return None, None


def usb_reset(bus, dev):
    """Send USBDEVFS_RESET to recover a NAK-stuck endpoint."""
    USBDEVFS_RESET = (0 << 30) | (0 << 16) | (ord('U') << 8) | 20
    path = f'/dev/bus/usb/{bus:03d}/{dev:03d}'
    try:
        fd = os.open(path, os.O_WRONLY)
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        os.close(fd)
        time.sleep(1.0)
        return True
    except Exception as e:
        print(f'\n  USB reset failed ({path}): {e}')
        return False


# ── HID helpers ───────────────────────────────────────────────────────────────

def hid_set_feature(fd, data):
    fcntl.ioctl(fd, HIDIOCSFEATURE(len(data)), bytearray(data))

def hid_get_feature(fd, report_id=0x0f):
    buf = bytearray(64)
    buf[0] = report_id
    fcntl.ioctl(fd, HIDIOCGFEATURE(64), buf)
    return bytes(buf)


def init_display(fd):
    """
    One-time init sequence.  Must be followed immediately (same fd, no close,
    no sleep) by send_chunks() — any gap after CMD_1D causes ETIMEDOUT.
    """
    print('  [init]', end=' ', flush=True)
    hid_set_feature(fd, CMD_18);  time.sleep(0.05);  print('18', end=' ', flush=True)
    hid_set_feature(fd, CMD_1A);  time.sleep(0.05);  print('1a', end=' ', flush=True)
    try:
        r = hid_get_feature(fd, 0x07)
        print(f'rpt07={r[1:13].hex()}', end=' ', flush=True)
    except Exception as e:
        print(f'(rpt07:{e})', end=' ', flush=True)
    for _ in range(4):
        hid_set_feature(fd, CMD_0C_DIMS);  time.sleep(0.02)
    print('0c×4', end=' ', flush=True)
    try:
        r = hid_get_feature(fd, 0x0f)
        print(f'rpt0f={r[1]:#04x}', end=' ', flush=True)
    except Exception as e:
        print(f'(rpt0f:{e})', end=' ', flush=True)
    hid_set_feature(fd, CMD_1D)
    print('1d→', end='', flush=True)


def begin_next_frame(fd):
    """Per-frame handshake for frame 2 onwards."""
    try:
        hid_get_feature(fd, 0x0f)
    except Exception:
        pass
    hid_set_feature(fd, CMD_0C_NEXT)
    time.sleep(0.02)


def send_chunks(fd, jpeg_bytes):
    """
    Write JPEG data as HID Output Report 2 chunks.

    Chunk header (8 bytes):
      byte 0:   0x02        Report ID
      byte 1:   0x09        constant
      byte 2:   0x00        constant
      byte 3:   0x00/0x01   0=normal, 1=last chunk (triggers display render)
      bytes 4-5 LE uint16   1016 for normal chunks; actual trailing bytes for last
      bytes 6-7 LE uint16   chunk index (0-based)
    """
    pad          = (-len(jpeg_bytes)) % CHUNK
    data         = jpeg_bytes + b'\x00' * pad
    total_chunks = len(data) // CHUNK

    for i in range(0, len(data), CHUNK):
        idx = i // CHUNK
        if idx == total_chunks - 1:
            trailing = len(jpeg_bytes) - i
            hdr = bytes([0x02, 0x09, 0x00, 0x01,
                         trailing & 0xff, (trailing >> 8) & 0xff,
                         idx & 0xff, (idx >> 8) & 0xff])
        else:
            hdr = bytes([0x02, 0x09, 0x00, 0x00, 0xf8, 0x03,
                         idx & 0xff, (idx >> 8) & 0xff])
        os.write(fd, hdr + data[i:i + CHUNK])


# ── Sensor helpers ────────────────────────────────────────────────────────────

def get_temps():
    t    = psutil.sensors_temperatures()
    cpu  = next((e.current for e in t.get('k10temp', []) if e.label == 'Tctl'), None)
    gpu  = next((e.current for e in t.get('amdgpu',  []) if e.label == 'edge' and e.high == 100.0), None)
    nvme = next((e.current for e in t.get('nvme',    []) if e.label == 'Composite'), None)
    return cpu, gpu, nvme


def c_to_f(c):
    return c * 9 / 5 + 32

def temp_color(t, warn=158, crit=185):   # °F: 70°C=158, 85°C=185
    if t is None: return '#888888'
    if c_to_f(t) >= crit: return '#ff3333'
    if c_to_f(t) >= warn: return '#ffaa00'
    return '#44ff88'


def bar(draw, x, y, w, h, pct, color):
    draw.rectangle([x, y, x + w, y + h], fill='#1a1a1a')
    fw = max(0, int(w * min(pct, 100) / 100))
    if fw:
        draw.rectangle([x, y, x + fw, y + h], fill=color)
    draw.rectangle([x, y, x + w, y + h], outline='#333333')


# ── Frame renderer ────────────────────────────────────────────────────────────

def make_frame():
    cpu_temp, gpu_temp, nvme_temp = get_temps()
    cpu_pct = psutil.cpu_percent(interval=None)
    ram     = psutil.virtual_memory()

    img  = Image.new('RGB', (W, H), '#080808')
    draw = ImageDraw.Draw(img)

    try:
        fl = ImageFont.truetype(FONT_L, 30)
        fm = ImageFont.truetype(FONT_R, 16)
        fs = ImageFont.truetype(FONT_R, 13)
    except Exception:
        fl = fm = fs = ImageFont.load_default()

    draw.line([(238, 6), (238, 122)], fill='#2a2a2a', width=1)

    # ── Left panel: CPU ───────────────────────────────────────────────────────
    draw.text((8, 4),  'CPU TEMP', font=fs, fill='#555555')
    draw.text((8, 20), f'{c_to_f(cpu_temp):.1f}°' if cpu_temp else 'N/A',
              font=fl, fill=temp_color(cpu_temp))

    draw.text((8, 58), f'LOAD  {cpu_pct:.0f}%', font=fm, fill='#aaaaaa')
    bar(draw, 8, 78, 222, 10, cpu_pct, '#3377ff')

    ram_gb  = ram.used  / 1024**3
    ram_tot = ram.total / 1024**3
    draw.text((8, 94),  f'RAM   {ram_gb:.1f}/{ram_tot:.0f} GB', font=fm, fill='#aaaaaa')
    bar(draw, 8, 112, 222, 10, ram.percent, '#8844cc')

    # ── Right panel: GPU + NVMe + time ────────────────────────────────────────
    draw.text((248, 4),  'GPU TEMP', font=fs, fill='#555555')
    draw.text((248, 20), f'{c_to_f(gpu_temp):.1f}°' if gpu_temp else 'N/A',
              font=fl, fill=temp_color(gpu_temp, warn=167, crit=194))

    draw.text((248, 62), 'NVME', font=fs, fill='#555555')
    draw.text((248, 78), f'{c_to_f(nvme_temp):.1f}°' if nvme_temp else 'N/A',
              font=fm, fill=temp_color(nvme_temp, warn=131, crit=158))

    now = datetime.now()
    draw.text((248, 100), now.strftime('%H:%M:%S'), font=fm, fill='#444444')
    draw.text((352, 104), now.strftime('%m/%d'),    font=fs, fill='#333333')

    return img


def encode_jpeg(img):
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=92, subsampling=0)
    return buf.getvalue()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    hidraw = find_hidraw()
    while hidraw is None:
        print(f'Waiting for Thermaltake RC Pro (USB {USB_VID:04x}:{USB_PID:04x})...')
        time.sleep(10)
        hidraw = find_hidraw()

    print(f'tt-lcd-rc-pro: {hidraw}  {W}×{H}  update={INTERVAL}s')
    psutil.cpu_percent()   # warm-up call (first call always returns 0.0)
    time.sleep(0.5)

    fd = None
    first_frame      = True
    consecutive_errors = 0

    while True:
        try:
            if fd is None:
                if not os.path.exists(hidraw):
                    raise FileNotFoundError(f'{hidraw} not found')
                fd = os.open(hidraw, os.O_RDWR)
                first_frame = True

            # Encode JPEG before any protocol commands (timing-critical after CMD_1D)
            jpeg = encode_jpeg(make_frame())

            if first_frame:
                init_display(fd)
                send_chunks(fd, jpeg)
                first_frame = False
                print('OK')
            else:
                begin_next_frame(fd)
                send_chunks(fd, jpeg)

            t   = psutil.sensors_temperatures()
            cpu = next((e.current for e in t.get('k10temp', []) if e.label == 'Tctl'), 0)
            gpu = next((e.current for e in t.get('amdgpu',  []) if e.label == 'edge' and e.high == 100), 0)
            print(f'\r  CPU {cpu:.1f}°  GPU {gpu:.1f}°  load {psutil.cpu_percent(interval=None):.0f}%   ',
                  end='', flush=True)

            consecutive_errors = 0
            time.sleep(INTERVAL)

        except (TimeoutError, OSError) as e:
            consecutive_errors += 1
            print(f'\n  Error #{consecutive_errors}: {e}')
            if fd is not None:
                try: os.close(fd)
                except Exception: pass
                fd = None

            if consecutive_errors >= 2:
                print('  Attempting USB reset...')
                bus, dev = find_usb_addr()
                if bus:
                    usb_reset(bus, dev)
                    print(f'  Reset bus{bus}/dev{dev:03d}, waiting...')
                    consecutive_errors = 0
                else:
                    print('  Could not locate USB device for reset')
                    time.sleep(5)

        except Exception as e:
            print(f'\n  Unexpected error: {e}')
            import traceback; traceback.print_exc()
            time.sleep(2)


if __name__ == '__main__':
    main()
