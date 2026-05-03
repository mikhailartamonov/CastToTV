"""Programmatically drive the CastToTV GUI to take screenshots.

Imports the app, patches the file-picker dialog to return a fixed path,
schedules `do_dongle_setup` and `do_cast` via `root.after`, captures the
window via PIL.ImageGrab between steps.
"""
from __future__ import annotations
import os
import sys
import time
import threading
import ctypes
import tkinter as tk
import tkinter.filedialog
from PIL import ImageGrab

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(REPO, "docs", "images")
SAMPLE_VIDEO = r"C:\Users\d3x\Videos\2026-03-01 05-54-29.mp4"
DONGLE_PATH = os.path.join(IMG_DIR, "dongle.png")
CAST_PATH = os.path.join(IMG_DIR, "cast.png")

os.makedirs(IMG_DIR, exist_ok=True)
ctypes.windll.user32.SetProcessDPIAware()

# Patch the file dialog before importing the app
tkinter.filedialog.askopenfilename = lambda **kw: SAMPLE_VIDEO if "Video" in (kw.get("filetypes") or [["Video"]])[0][0] else ""

sys.path.insert(0, REPO)
import cast_to_tv  # noqa: E402


def find_window_rect(title_substr: str):
    user32 = ctypes.windll.user32
    EnumWindows = user32.EnumWindows
    GetWindowTextW = user32.GetWindowTextW
    GetWindowRect = user32.GetWindowRect
    IsWindowVisible = user32.IsWindowVisible
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int))

    found = []

    def cb(hwnd, _):
        if not IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        GetWindowTextW(hwnd, buf, 256)
        if title_substr in buf.value:
            class RECT(ctypes.Structure):
                _fields_ = [('l', ctypes.c_long), ('t', ctypes.c_long),
                            ('r', ctypes.c_long), ('b', ctypes.c_long)]
            rect = RECT()
            GetWindowRect(hwnd, ctypes.byref(rect))
            found.append((rect.l, rect.t, rect.r, rect.b))
            return False
        return True

    EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None


def capture(out_path, pad=80):
    rect = find_window_rect("D3x LG Caster")
    if not rect:
        print(f"[capture] window not found, skip {out_path}")
        return
    l, t, r, b = rect
    bbox = (max(0, l - pad), max(0, t - 50), r + pad, b + 100)
    ImageGrab.grab(bbox=bbox).save(out_path)
    print(f"[capture] saved {out_path} ({bbox[2]-bbox[0]} x {bbox[3]-bbox[1]})")


def main():
    root = tk.Tk()
    app = cast_to_tv.KeygenApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    # Make the window foreground for clean captures.
    root.update()
    root.attributes('-topmost', True)
    root.update_idletasks()

    # Step A — wait for SSDP scan to populate, then trigger dongle dialog
    def step_dongle():
        try:
            app.do_dongle_setup()
        except Exception as e:
            print("dongle err:", e)
        root.after(2500, capture_dongle)

    def capture_dongle():
        capture(DONGLE_PATH)
        root.after(800, close_dialog)

    def close_dialog():
        # The dongle dialog is a Tk Toplevel — find and destroy it.
        for w in root.winfo_children():
            if isinstance(w, tk.Toplevel):
                w.destroy()
        root.after(800, step_cast)

    def step_cast():
        # Set IP + file directly (skipping discover + file picker)
        app.ip_entry.delete(0, 'end')
        app.ip_entry.insert(0, "192.168.100.28")
        app.file_entry.delete(0, 'end')
        app.file_entry.insert(0, SAMPLE_VIDEO)
        try:
            app.do_manual_connect()
        except Exception as e:
            print("connect err:", e)
        # Wait for connect (port scan ~10s) then cast
        root.after(15000, lambda: (app.do_cast() if hasattr(app, 'do_cast') else None))
        # Capture mid-stream after cast completes (~10s after cast click)
        root.after(28000, capture_cast)

    def capture_cast():
        capture(CAST_PATH, pad=20)
        root.after(800, app.on_close)

    # Kick off after 16s so SSDP discovery has time
    root.after(16000, step_dongle)
    root.mainloop()


if __name__ == "__main__":
    main()
