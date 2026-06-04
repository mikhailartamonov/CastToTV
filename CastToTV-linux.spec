# -*- mode: python ; coding: utf-8 -*-
# Ubuntu/Linux one-file build with ffmpeg + ffprobe + yt-dlp bundled inside.
# Build:  .venv/bin/pyinstaller CastToTV-linux.spec --noconfirm
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Pull in the optional cast backends and their dynamic submodules / data files.
for pkg in ('pychromecast', 'pyatv', 'zeroconf', 'aiohttp', 'cryptography', 'miniaudio', 'PIL'):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # a missing optional dep just means that backend is unavailable in the build

# Bundle the helper binaries at the app root so resolve_binary() finds them via sys._MEIPASS.
binaries += [
    ('vendor/ffmpeg', '.'),
    ('vendor/ffprobe', '.'),
    ('vendor/yt-dlp', '.'),
]

a = Analysis(
    ['cast_to_tv.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CastToTV',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX can corrupt the bundled static ffmpeg; not worth the risk
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
