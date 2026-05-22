#!/usr/bin/env python3
"""
Thermaltake AIO LCD — native Linux controller
==============================================
Display:  480 × 480 px (round, circular aperture)
Device:   USB HID 264a:2328  →  /dev/hidraw*  (auto-detected)
Protocol: JPEG split into 1016-byte HID Output Report chunks

Init sequence (from USB capture analysis):
  SET_REPORT 0x1a  →  GET_REPORT 0x07
  →  SET_REPORT 0x0c × 3  →  GET_REPORT 0x0f
  →  write chunks immediately (no CMD_1D required)

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
W, H     = 480, 480
CHUNK    = 1016
INTERVAL = 2          # seconds between frame updates

FONT_L = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT_R = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

USB_VID, USB_PID = 0x264a, 0x2328   # Thermaltake AIO

# ── HID ioctl numbers (/usr/include/linux/hidraw.h) ───────────────────────────
HIDIOCSFEATURE = lambda n: (3 << 30) | (n << 16) | (0x48 << 8) | 0x06
HIDIOCGFEATURE = lambda n: (3 << 30) | (n << 16) | (0x48 << 8) | 0x07

# ── Feature Report payloads (64 bytes each, report ID 0x03) ───────────────────
CMD_1A      = bytes.fromhex('031a' + '00' * 62)
CMD_0C_480  = bytes.fromhex('030c6400000000e001e0010405000000' + '00' * 48)
CMD_0C_NEXT = bytes.fromhex('030c64ffffeaffffff' + '00' * 55)

assert all(len(c) == 64 for c in (CMD_1A, CMD_0C_480, CMD_0C_NEXT))


# ── Device discovery ──────────────────────────────────────────────────────────

def find_hidraw(vendor=USB_VID, product=USB_PID):
    """Return the /dev/hidraw* path for the given USB VID:PID, or None."""
    for path in glob.glob('/sys/class/hidraw/hidraw*'):
        try:
            uevent = open(os.path.join(path, 'device', 'uevent')).read()
            if f'{vendor:04X}:{product:04X}' in uevent.upper():
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
    One-time init sequence.  Write chunks immediately after — no CMD_1D
    required for the AIO (unlike the RC Pro).
    """
    print('  [init]', end=' ', flush=True)
    hid_set_feature(fd, CMD_1A);  time.sleep(0.05);  print('1a', end=' ', flush=True)
    try:
        r = hid_get_feature(fd, 0x07)
        print(f'rpt07={r[1:13].hex()}', end=' ', flush=True)
    except Exception as e:
        print(f'(rpt07:{e})', end=' ', flush=True)
    for _ in range(3):
        hid_set_feature(fd, CMD_0C_480);  time.sleep(0.02)
    print('0c×3', end=' ', flush=True)
    try:
        r = hid_get_feature(fd, 0x0f)
        print(f'rpt0f={r[1]:#04x}', end=' ', flush=True)
    except Exception as e:
        print(f'(rpt0f:{e})', end=' ', flush=True)
    print('→', end='', flush=True)


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


def temp_color(t, warn=70, crit=85):
    if t is None: return '#888888'
    if t >= crit: return '#ff3333'
    if t >= warn: return '#ffaa00'
    return '#44ff88'


# ── Frame renderer ────────────────────────────────────────────────────────────

def make_frame():
    cpu_temp, gpu_temp, nvme_temp = get_temps()
    cpu_pct = psutil.cpu_percent(interval=None)
    ram     = psutil.virtual_memory()
    now     = datetime.now()

    img  = Image.new('RGB', (W, H), '#000000')
    draw = ImageDraw.Draw(img)

    CX = W // 2   # horizontal center (240)

    try:
        fhuge = ImageFont.truetype(FONT_L, 72)
        fbig  = ImageFont.truetype(FONT_L, 28)
        fmed  = ImageFont.truetype(FONT_R, 18)
        fsm   = ImageFont.truetype(FONT_R, 15)
        ftiny = ImageFont.truetype(FONT_R, 12)
    except Exception:
        fhuge = fbig = fmed = fsm = ftiny = ImageFont.load_default()

    def ctext(y, text, font, fill):
        """Draw text centered on the circle's horizontal axis."""
        bb = draw.textbbox((0, 0), text, font=font)
        draw.text((CX - (bb[2] - bb[0]) // 2, y), text, font=font, fill=fill)

    def xtext(x, y, text, font, fill, anchor='left'):
        """Draw text left- or right-anchored at x."""
        if anchor == 'right':
            bb = draw.textbbox((0, 0), text, font=font)
            x -= (bb[2] - bb[0])
        draw.text((x, y), text, font=font, fill=fill)

    def cbar(y, w, hb, pct, color):
        """Centered progress bar."""
        x0 = CX - w // 2
        draw.rectangle([x0, y, x0 + w, y + hb], fill='#1a1a1a')
        fw = max(0, int(w * min(pct, 100) / 100))
        if fw:
            draw.rectangle([x0, y, x0 + fw, y + hb], fill=color)
        draw.rectangle([x0, y, x0 + w, y + hb], outline='#2a2a2a')

    def col_ctext(cx, y, text, font, fill):
        """Draw text centered on column axis cx."""
        bb = draw.textbbox((0, 0), text, font=font)
        draw.text((cx - (bb[2] - bb[0]) // 2, y), text, font=font, fill=fill)

    # ── dark filled circle background ─────────────────────────────────────────
    draw.ellipse([0, 0, W - 1, H - 1], fill='#080808')

    # ── CPU (top arc) ─────────────────────────────────────────────────────────
    ctext(28, 'CPU', fmed, '#505050')
    ctext(50, f'{cpu_temp:.1f}°' if cpu_temp else 'N/A',
          fhuge, temp_color(cpu_temp))

    # ── Load + RAM bars ───────────────────────────────────────────────────────
    BAR_W = 280

    y = 140
    xtext(CX - BAR_W // 2, y, 'LOAD', fsm, '#555555')
    xtext(CX + BAR_W // 2, y, f'{cpu_pct:.0f}%', fsm, '#888888', anchor='right')
    cbar(y + 17, BAR_W, 12, cpu_pct, '#3377ff')

    y = 182
    ram_gb  = ram.used  / 1024**3
    ram_tot = ram.total / 1024**3
    xtext(CX - BAR_W // 2, y, f'RAM  {ram_gb:.0f}/{ram_tot:.0f}G', fsm, '#555555')
    xtext(CX + BAR_W // 2, y, f'{ram.percent:.0f}%', fsm, '#888888', anchor='right')
    cbar(y + 17, BAR_W, 12, ram.percent, '#8844cc')

    # ── Center band: NVMe (left)  |  Clock (right) ────────────────────────────
    draw.line([(CX - 80, 222), (CX + 80, 222)], fill='#1e1e1e', width=1)
    draw.line([(CX - 80, 292), (CX + 80, 292)], fill='#1e1e1e', width=1)

    LX, RX = 100, 380
    nc = temp_color(nvme_temp, warn=55, crit=70)
    col_ctext(LX, 230, 'NVMe', fsm, '#404040')
    col_ctext(LX, 250, f'{nvme_temp:.1f}°' if nvme_temp else 'N/A', fbig, nc)

    col_ctext(RX, 230, now.strftime('%H:%M'), fbig, '#484848')
    col_ctext(RX, 264, now.strftime(':%S'),   fsm,  '#282828')

    # ── GPU (bottom arc) ──────────────────────────────────────────────────────
    ctext(302, f'{gpu_temp:.1f}°' if gpu_temp else 'N/A',
          fhuge, temp_color(gpu_temp, warn=75, crit=90))
    ctext(378, 'GPU', fmed, '#505050')

    # ── Date (bottom, near bezel) ─────────────────────────────────────────────
    ctext(403, now.strftime('%b %d'), ftiny, '#1e1e1e')

    # ── Clip to circle ────────────────────────────────────────────────────────
    mask = Image.new('L', (W, H), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, W - 1, H - 1], fill=255)
    black = Image.new('RGB', (W, H), '#000000')
    black.paste(img, mask=mask)
    img = black

    # Rotate 180° — the AIO cooler mounts the display inverted
    img = img.rotate(180)
    return img


def encode_jpeg(img):
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=92, subsampling=0)
    return buf.getvalue()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    hidraw = find_hidraw()
    if hidraw is None:
        print(f'ERROR: Thermaltake AIO (USB {USB_VID:04x}:{USB_PID:04x}) not found.')
        print('       Check udev rules and that the device is connected.')
        sys.exit(1)

    print(f'tt-lcd-aio: {hidraw}  {W}×{H}  update={INTERVAL}s')
    psutil.cpu_percent()   # warm-up call (first call always returns 0.0)
    time.sleep(0.5)

    fd = None
    first_frame        = True
    consecutive_errors = 0

    while True:
        try:
            if fd is None:
                if not os.path.exists(hidraw):
                    raise FileNotFoundError(f'{hidraw} not found')
                fd = os.open(hidraw, os.O_RDWR)
                first_frame = True

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
