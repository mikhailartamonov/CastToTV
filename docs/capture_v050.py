"""One-shot screenshot capture for v0.5.0 GUI (4 images: main, devices, dongle, cast)."""
from __future__ import annotations
import os
import sys
import time
import subprocess
import ctypes
import pyautogui
from PIL import ImageGrab, Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(REPO, "docs", "images")
SAMPLE_VIDEO = r"C:\Users\d3x\Videos\2026-03-01 05-54-29.mp4"
LOG_PATH = os.path.join(REPO, "cast_log.txt")

DISCOVER_BG = (0x00, 0xFF, 0xFF)  # cyan
DONGLE_BG   = (0xFF, 0x99, 0x00)  # orange (WiFi dongle button)
CAST_BG     = (0xFF, 0x66, 0x00)  # darker orange (CAST)
FILE_GREEN  = (0x00, 0xFF, 0x00)  # [...] next to FILE entry

ctypes.windll.user32.SetProcessDPIAware()


def find_window_rect(title_substr: str):
    EnumWindows = ctypes.windll.user32.EnumWindows
    GetWindowTextW = ctypes.windll.user32.GetWindowTextW
    GetWindowRect = ctypes.windll.user32.GetWindowRect
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    found = []

    def cb(hwnd, _):
        if not IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        GetWindowTextW(hwnd, buf, 256)
        if title_substr.lower() in buf.value.lower():
            class RECT(ctypes.Structure):
                _fields_ = [('l', ctypes.c_long), ('t', ctypes.c_long),
                            ('r', ctypes.c_long), ('b', ctypes.c_long)]
            r = RECT()
            GetWindowRect(hwnd, ctypes.byref(r))
            found.append((hwnd, buf.value, r.l, r.t, r.r, r.b))
            return False
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    return found[0] if found else None


def find_color_centre(img: Image.Image, target_rgb, tol=18, min_count=20, region=None):
    px = img.load()
    w, h = img.size
    matches = []
    tr, tg, tb = target_rgb
    y0, y1 = (0, h) if region is None else region
    for y in range(y0, min(y1, h)):
        for x in range(0, w):
            r, g, b = px[x, y][:3]
            if abs(r-tr) <= tol and abs(g-tg) <= tol and abs(b-tb) <= tol:
                matches.append((x, y))
    if len(matches) < min_count:
        return None
    xs = [p[0] for p in matches]
    ys = [p[1] for p in matches]
    return (sum(xs)//len(xs), sum(ys)//len(ys))


def grab(rect, path):
    l, t, r, b = rect
    ImageGrab.grab(bbox=(l, t, r, b)).save(path)
    print(f"  saved {os.path.basename(path)} ({r-l}x{b-t})")


def wait_log_contains(needle, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(LOG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                if needle in f.read():
                    return True
        except FileNotFoundError:
            pass
        time.sleep(0.5)
    return False


def main():
    os.makedirs(IMG_DIR, exist_ok=True)

    print("[1] launching GUI…")
    proc = subprocess.Popen([sys.executable, "cast_to_tv.py"], cwd=REPO)
    time.sleep(4)

    win = find_window_rect("Caster")
    if not win:
        print("ERROR: window not found"); proc.kill(); return 1
    hwnd, title, l, t, r, b = win
    rect = (l, t, r, b)
    print(f"[2] window: {title} @ {rect}")

    # main.png — initial state
    grab(rect, os.path.join(IMG_DIR, "main.png"))

    # DISCOVER
    img = ImageGrab.grab(bbox=rect)
    disc = find_color_centre(img, DISCOVER_BG)
    if not disc:
        print("WARN: DISCOVER button not found by colour")
    else:
        cx, cy = l + disc[0], t + disc[1]
        print(f"[3] click DISCOVER @ {(cx,cy)}")
        pyautogui.click(cx, cy)
        if wait_log_contains("renderer(s) in", timeout=30):
            print("[3a] discovery completed")
            time.sleep(0.5)
        else:
            print("[3a] discovery timeout, capturing anyway")
        grab(rect, os.path.join(IMG_DIR, "devices.png"))

    # DONGLE WiFi (just log shows 'Dongle not found' — fine)
    img = ImageGrab.grab(bbox=rect)
    dg = find_color_centre(img, DONGLE_BG)
    if not dg:
        print("WARN: DONGLE button not found")
    else:
        cx, cy = l + dg[0], t + dg[1]
        print(f"[4] click DONGLE @ {(cx,cy)}")
        pyautogui.click(cx, cy)
        time.sleep(2.0)
        grab(rect, os.path.join(IMG_DIR, "dongle.png"))

    # FILE entry: find [...] green button (small, near the file row in upper half of window)
    img = ImageGrab.grab(bbox=rect)
    # Restrict search to upper-middle area to dodge the seek-buttons' green text colour
    fb = find_color_centre(img, FILE_GREEN, tol=8, min_count=30, region=(350, 500))
    if not fb:
        print("WARN: FILE [...] anchor not found")
    else:
        ex = l + fb[0] - 120
        ey = t + fb[1]
        print(f"[5] click in FILE entry @ {(ex,ey)}")
        pyautogui.tripleClick(ex, ey)
        time.sleep(0.3)
        pyautogui.write(SAMPLE_VIDEO, interval=0.005)
        time.sleep(0.4)

        img = ImageGrab.grab(bbox=rect)
        cb = find_color_centre(img, CAST_BG)
        if cb:
            ccx, ccy = l + cb[0], t + cb[1]
            print(f"[6] click CAST @ {(ccx,ccy)}")
            pyautogui.click(ccx, ccy)
            if wait_log_contains("Streaming started!", timeout=20):
                print("[6a] cast started")
                time.sleep(1.0)
            else:
                print("[6a] cast timeout, capturing anyway")
            grab(rect, os.path.join(IMG_DIR, "cast.png"))
        else:
            print("WARN: CAST button not found")

    print("[7] closing GUI")
    try:
        proc.kill()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
