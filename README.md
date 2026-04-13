# CastToTV

DLNA video caster for LG WebOS TVs with external subtitle support.

Keygen 2005 style GUI with matrix rain and chiptune music.

## Features

- SSDP auto-discovery and network scan for TV
- DLNA port auto-detection
- Built-in HTTP server with Range request support (seeking)
- External subtitles (SRT, VTT, SUB, SMI) via `CaptionInfo.sec` + `sec:CaptionInfoEx`
- UTF-8 BOM auto-prepend for subtitle compatibility
- Stop/cast controls

## Usage

### Run from source
```
python cast_to_tv.py
```

### Build executable
```
pip install pyinstaller
build.bat
```
Or manually:
```
pyinstaller --onefile --windowed --name CastToTV cast_to_tv.py
```

Output: `dist/CastToTV.exe`

## How it works

1. Enter TV IP (or use SSDP / NET SCAN to find it)
2. Click FIND DLNA to detect the AVTransport service port
3. Select a video file
4. (Optional) Select a subtitle file (.srt, .vtt)
5. Click CAST

The app starts an HTTP server on port 8766, sends DLNA `SetAVTransportURI` + `Play` SOAP commands to the TV. Subtitles are delivered via `CaptionInfo.sec` HTTP response header and `sec:CaptionInfoEx` DIDL-Lite metadata.

## Requirements

- Python 3.10+ (tkinter included)
- Windows (uses `winsound` for chiptune music, optional)
- LG WebOS TV on the same network
