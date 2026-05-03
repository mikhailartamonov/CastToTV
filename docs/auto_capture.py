"""Drive CastToTV GUI via pyautogui to capture remaining screenshots.

- dongle.png: click DONGLE WiFi (orange #FF9900), capture dialog, close.
- cast.png:   pick a video, click CAST (orange #FF6600), wait, capture.

Locate buttons by exact background colour (each button has a unique bg in the GUI).
"""

from __future__ import annotations
import os
import sys
import time
import subprocess
import pyautogui
import ctypes
from PIL import ImageGrab, Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(REPO, "docs", "images")
SAMPLE_VIDEO = r"C:\Users\d3x\Videos\2026-03-01 05-54-29.mp4"

DONGLE_BG = (0xFF, 0x99, 0x00)
CAST_BG   = (0xFF, 0x66, 0x00)

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


def find_window_rect(title_substr: str):
    """Return (left, top, right, bottom) of first top-level window matching title."""
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
            rect = RECT()
            GetWindowRect(hwnd, ctypes.byref(rect))
            found.append((hwnd, buf.value, rect.l, rect.t, rect.r, rect.b))
            return False
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    return found[0] if found else None


def find_color_centre(img: Image.Image, target_rgb, tol=18, min_count=20):
    """Return (cx, cy) image-relative centre of pixels matching target_rgb (within tol)."""
    px = img.load()
    w, h = img.size
    matches = []
    tr, tg, tb = target_rgb
    for y in range(0, h):
        for x in range(0, w):
            r, g, b = px[x, y][:3]
            if abs(r-tr) <= tol and abs(g-tg) <= tol and abs(b-tb) <= tol:
                matches.append((x, y))
    if len(matches) < min_count:
        return None
    xs = [p[0] for p in matches]
    ys = [p[1] for p in matches]
    return (sum(xs)//len(xs), sum(ys)//len(ys))


def capture_window(rect, out_path):
    l, t, r, b = rect
    img = ImageGrab.grab(bbox=(l, t, r, b))
    img.save(out_path)
    print(f"saved {out_path} ({img.size[0]}x{img.size[1]})")


def main():
    os.makedirs(IMG_DIR, exist_ok=True)

    print("[1] launching GUI…")
    proc = subprocess.Popen(["python", "cast_to_tv.py"], cwd=REPO)
    time.sleep(6)

    win = find_window_rect("Caster")
    if not win:
        print("ERROR: window not found"); proc.kill(); return 1
    hwnd, title, l, t, r, b = win
    print(f"[2] window: {title} @ {(l,t,r,b)}")

    # wait until SSDP scan completes (~20s after launch total)
    time.sleep(15)

    # debug: save the full window so we can see what colours are actually present
    win_img = ImageGrab.grab(bbox=(l, t, r, b))
    win_img.save(os.path.join(IMG_DIR, "_debug_window.png"))

    # ---------- DONGLE dialog ----------
    print("[3] looking for DONGLE WiFi button (orange #FF9900)…")
    centre = find_color_centre(win_img, DONGLE_BG)
    if not centre:
        print("WARN: dongle button not found by colour; skipping dongle.png")
    else:
        cx, cy = l + centre[0], t + centre[1]
        print(f"[4] click DONGLE @ {(cx, cy)}")
        pyautogui.click(cx, cy)
        time.sleep(1.5)

        # capture entire screen near the GUI (dialog may sit outside main window)
        # widen bbox by 200px on each side
        ImageGrab.grab(bbox=(max(0, l-200), max(0, t-50), r+200, b+150)).save(
            os.path.join(IMG_DIR, "dongle.png"))
        print(f"saved {os.path.join(IMG_DIR, 'dongle.png')}")
        # close dialog with Escape
        pyautogui.press('escape')
        time.sleep(0.8)

    # ---------- cast-in-progress ----------
    print("[5] sending video path to FILE entry…")
    # Strategy: click on the FILE entry by colour anchor — find the green [...] button
    # background #00FF00 next to FILE entry, then click left of it (in the entry).
    win_img = ImageGrab.grab(bbox=(l, t, r, b))
    file_btn_centre = find_color_centre(win_img, (0x00, 0xFF, 0x00), tol=8)
    # That match may include other green elements; use lower-half priority by filtering y > h/3
    # Simpler: take first hit, click 80px to the left of it (entry sits left of the [...] button).
    if not file_btn_centre:
        print("WARN: FILE [...] anchor not found; skipping cast.png")
    else:
        # The green [...] button — click 80 px to the LEFT of it to focus the entry
        ex = l + file_btn_centre[0] - 80
        ey = t + file_btn_centre[1]
        print(f"[6] click in FILE entry @ {(ex, ey)}")
        pyautogui.tripleClick(ex, ey)
        time.sleep(0.4)
        pyautogui.write(SAMPLE_VIDEO, interval=0.01)
        time.sleep(0.4)

        # Find CAST button (orange #FF6600)
        win_img = ImageGrab.grab(bbox=(l, t, r, b))
        cast_centre = find_color_centre(win_img, CAST_BG)
        if not cast_centre:
            print("WARN: CAST button not found by colour; skipping cast.png")
        else:
            ccx, ccy = l + cast_centre[0], t + cast_centre[1]
            print(f"[7] click CAST @ {(ccx, ccy)}")
            pyautogui.click(ccx, ccy)
            print("[8] waiting 8s for cast to start…")
            time.sleep(8)
            capture_window((l, t, r, b), os.path.join(IMG_DIR, "cast.png"))

    print("[9] closing GUI")
    try:
        proc.kill()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
