#!/usr/bin/env python3
"""
tt-lcd-tower500.py — driver for the Thermaltake Tower 500 bar LCD (264a:233d,
480x128), as found on AI02. Protocol ported from JohnathanKong/Tower-500-LCD-
Controller (GPL-3.0): two HID interfaces — :1.0 command, :1.1 frame — image is
JPEG, chunked. Distinct from the RC Pro (232a) raw-pixel protocol.

Env knobs for bring-up:
  TT_NO_REPORT_ID=1   -> do NOT prepend the hidraw report-id 0 byte (fallback framing)
  TT_ONCE=1           -> render one frame and exit (bring-up test)
"""
import os, sys, glob, io, math, time
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

VID, PID = 0x264a, 0x233d
W, H = 480, 128
COMMAND_SIZE = 440
FRAME_PACKET_SIZE = 1024
FRAME_DATA_SIZE = 1020
INTERVAL = 2.0
PREPEND_ID = (os.environ.get("TT_NO_REPORT_ID") != "1")

def _usb_ids_for(sys_hidraw):
    p = os.path.realpath(sys_hidraw + "/device")
    up = p
    while up != "/":
        vf = os.path.join(up, "idVendor")
        if os.path.exists(vf):
            v = open(vf).read().strip().lower()
            d = open(os.path.join(up, "idProduct")).read().strip().lower()
            return v, d, p
        up = os.path.dirname(up)
    return None, None, p

def find_iface(iface):
    """iface = ':1.0' (command) or ':1.1' (frame)"""
    want = f"{VID:04x}", f"{PID:04x}"
    for hr in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        v, d, p = _usb_ids_for(hr)
        if (v, d) == want and iface in p:
            return "/dev/" + os.path.basename(hr)
    return None

def _write(fd, report):
    os.write(fd, (b"\x00" + report) if PREPEND_ID else report)

def cmd_pkt(seq):
    return bytes(seq).ljust(COMMAND_SIZE, b"\x00")

class Tower500:
    def __init__(self):
        self.cmd_path = find_iface(":1.0")
        self.frm_path = find_iface(":1.1")
        if not (self.cmd_path and self.frm_path):
            raise RuntimeError(f"interfaces not found: cmd={self.cmd_path} frm={self.frm_path}")
        print(f"tt-lcd-tower500: cmd={self.cmd_path} frame={self.frm_path} "
              f"prepend_report_id={PREPEND_ID}")
        self.cmd = os.open(self.cmd_path, os.O_RDWR)
        self.frm = os.open(self.frm_path, os.O_RDWR)

    def init(self):
        for op in (0x85, 0x87, 0x85, 0x87, 0x84, 0x81):
            _write(self.cmd, cmd_pkt((op, 0x01, 0x00, 0x80)))
            time.sleep(0.05)
        _write(self.cmd, cmd_pkt((0x12, 0x01, 0x00, 0x80, 0x64)))
        time.sleep(0.1)
        print("tt-lcd-tower500: init handshake sent")

    def send_image(self, img):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=92, subsampling=0)
        jpeg = buf.getvalue()
        n = math.ceil(len(jpeg) / FRAME_DATA_SIZE)
        for i in range(n):
            chunk = jpeg[i * FRAME_DATA_SIZE:(i + 1) * FRAME_DATA_SIZE]
            hdr = bytes((0x08, n, 0x00, 0x80)) if i == 0 else bytes((0x08, i, 0x00, 0x00))
            _write(self.frm, (hdr + chunk).ljust(FRAME_PACKET_SIZE, b"\x00"))
        print(f"tt-lcd-tower500: sent frame, jpeg={len(jpeg)}B in {n} packets")

# ── Rendering: identical to ai01's tt-lcd-rc-pro.py make_frame() ──────────────
FONT_L = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def get_temps():
    import psutil
    t    = psutil.sensors_temperatures()
    cpu  = next((e.current for e in t.get("k10temp", []) if e.label == "Tctl"), None)
    gpu  = next((e.current for e in t.get("amdgpu",  []) if e.label == "edge" and e.high == 100.0), None)
    nvme = next((e.current for e in t.get("nvme",    []) if e.label == "Composite"), None)
    return cpu, gpu, nvme

def c_to_f(c):
    return c * 9 / 5 + 32

def temp_color(t, warn=158, crit=185):   # °F: 70°C=158, 85°C=185
    if t is None: return "#888888"
    if c_to_f(t) >= crit: return "#ff3333"
    if c_to_f(t) >= warn: return "#ffaa00"
    return "#44ff88"

def bar(draw, x, y, w, h, pct, color):
    draw.rectangle([x, y, x + w, y + h], fill="#1a1a1a")
    fw = max(0, int(w * min(pct, 100) / 100))
    if fw:
        draw.rectangle([x, y, x + fw, y + h], fill=color)
    draw.rectangle([x, y, x + w, y + h], outline="#333333")

def render():
    import psutil
    cpu_temp, gpu_temp, nvme_temp = get_temps()
    cpu_pct = psutil.cpu_percent(interval=None)
    ram     = psutil.virtual_memory()

    img  = Image.new("RGB", (W, H), "#080808")
    draw = ImageDraw.Draw(img)
    try:
        fl = ImageFont.truetype(FONT_L, 30)
        fm = ImageFont.truetype(FONT_R, 16)
        fs = ImageFont.truetype(FONT_R, 13)
    except Exception:
        fl = fm = fs = ImageFont.load_default()

    draw.line([(238, 6), (238, 122)], fill="#2a2a2a", width=1)

    draw.text((8, 4),  "CPU TEMP", font=fs, fill="#555555")
    draw.text((8, 20), f"{c_to_f(cpu_temp):.1f}°" if cpu_temp else "N/A",
              font=fl, fill=temp_color(cpu_temp))
    draw.text((8, 58), f"LOAD  {cpu_pct:.0f}%", font=fm, fill="#aaaaaa")
    bar(draw, 8, 78, 222, 10, cpu_pct, "#3377ff")
    ram_gb  = ram.used  / 1024**3
    ram_tot = ram.total / 1024**3
    draw.text((8, 94),  f"RAM   {ram_gb:.1f}/{ram_tot:.0f} GB", font=fm, fill="#aaaaaa")
    bar(draw, 8, 112, 222, 10, ram.percent, "#8844cc")

    draw.text((248, 4),  "GPU TEMP", font=fs, fill="#555555")
    draw.text((248, 20), f"{c_to_f(gpu_temp):.1f}°" if gpu_temp else "N/A",
              font=fl, fill=temp_color(gpu_temp, warn=167, crit=194))
    draw.text((248, 62), "NVME", font=fs, fill="#555555")
    draw.text((248, 78), f"{c_to_f(nvme_temp):.1f}°" if nvme_temp else "N/A",
              font=fm, fill=temp_color(nvme_temp, warn=131, crit=158))

    now = datetime.now()
    draw.text((248, 100), now.strftime("%H:%M:%S"), font=fm, fill="#444444")
    draw.text((352, 104), now.strftime("%m/%d"),    font=fs, fill="#333333")
    return img

def main():
    dev = Tower500()
    dev.init()
    if os.environ.get("TT_ONCE") == "1":
        dev.send_image(render())
        print("TT_ONCE done")
        return
    while True:
        try:
            dev.send_image(render())
        except OSError as e:
            # let systemd restart us so the device fds are reopened & re-init'd
            print(f"write error, exiting for restart: {e}", file=sys.stderr)
            sys.exit(1)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
