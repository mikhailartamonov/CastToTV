#!/usr/bin/env python3
"""
CastToTV — cast video, YouTube/Rutube and music to every screen and speaker in your home.

A single-file, multi-protocol media caster. It discovers DLNA/UPnP renderers (smart TVs,
HDMI dongles), Chromecast and AirPlay receivers on the LAN, then streams:
  * local files in almost any format (transcoded on the fly when a renderer is picky),
  * YouTube / Rutube links (resolved via yt-dlp),
  * music tracks,
to a single room — or fans the very same stream out to many rooms at once for
near-synchronous, walk-around-the-house playback.

KeyGen-2005-style Tkinter interface, ffmpeg/yt-dlp powered.
"""

VERSION = "0.6.0-beta"
DEBUG_VERBOSE = True  # set False to silence [DBG] lines

# Module-level file logger so non-GUI helpers (HTTP handler, SOAP) can log
# without needing the Tk callback. Path resolves next to the script.
_LOG_PATH = None  # set after imports below

def _file_log(msg):
    if not _LOG_PATH:
        return
    try:
        import time as _t
        with open(_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f"{_t.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import urllib.request
import html
import socket
import struct
import re
import time
import threading
import random
import os
import io
import sys
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin, urlparse
import subprocess
import json
import concurrent.futures
import functools
import shutil

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cast_log.txt')

# ============= BUNDLED-BINARY RESOLVER =============

_BINARY_CACHE = {}

def resolve_binary(name):
    """Locate a helper binary (ffmpeg/ffprobe/yt-dlp): bundled-first, then system PATH.

    In a PyInstaller build the helpers are unpacked next to the app (sys._MEIPASS or the
    executable dir), so the fat binary is fully self-contained. In a plain checkout we fall
    back to whatever is on PATH, which is exactly what a 'lite' install wants. Cached per name.
    """
    cached = _BINARY_CACHE.get(name)
    if cached:
        return cached
    exe = name + ('.exe' if os.name == 'nt' else '')
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.join(base, exe)
    found = bundled if os.path.exists(bundled) else (shutil.which(name) or name)
    _BINARY_CACHE[name] = found
    return found


def _pdeathsig_preexec():
    """Ask the kernel to SIGKILL this child the moment its parent dies (Linux PR_SET_PDEATHSIG).

    Without this, a crashed or force-killed app leaves an orphaned ffmpeg muxing into a temp
    file forever. With it, ffmpeg can't outlive the app no matter how the app goes down.
    """
    try:
        import ctypes, signal as _sig
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, _sig.SIGKILL)  # PR_SET_PDEATHSIG=1
    except Exception:
        pass

# preexec_fn is POSIX-only and pdeathsig is Linux-only; None elsewhere (no-op on Windows/macOS).
_PREEXEC = _pdeathsig_preexec if sys.platform.startswith('linux') else None


# ============= TRANSCODER =============

def check_ffmpeg():
    try:
        subprocess.run([resolve_binary('ffmpeg'), '-version'], capture_output=True, timeout=5)
        return True
    except Exception:
        return False

HAS_FFMPEG = check_ffmpeg()

def probe_file(filepath):
    """Return audio codec, video codec, bitrate, duration."""
    if not HAS_FFMPEG:
        return {}, 0, 0
    try:
        result = subprocess.run([
            resolve_binary('ffprobe'), '-v', 'error',
            '-show_entries', 'stream=codec_name,codec_type',
            '-show_entries', 'format=bit_rate,duration',
            '-of', 'json', filepath
        ], capture_output=True, text=True, timeout=10)
        info = json.loads(result.stdout)
        codecs = {}
        for s in info.get('streams', []):
            codecs[s.get('codec_type', '')] = s.get('codec_name', '')
        bitrate = int(info.get('format', {}).get('bit_rate', 0))
        duration = float(info.get('format', {}).get('duration', 0))
        return codecs, bitrate, duration
    except Exception:
        return {}, 0, 0

def needs_audio_transcode(filepath):
    """Check if audio needs transcoding (AC3/DTS → AAC). Returns reason or None."""
    codecs, _, _ = probe_file(filepath)
    acodec = codecs.get('audio', '')
    if acodec in ('ac3', 'eac3', 'dts', 'dca', 'truehd', 'mlp'):
        return f'audio:{acodec}'
    return None

def format_duration(seconds):
    """Convert seconds to HH:MM:SS."""
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ============= DONGLE STREAMER (ffmpeg → MPEG-TS) =============

import http.server
import socketserver

# Optional cast backends. Imported lazily-guarded so a DLNA-only ("lite") install — or a run
# under a Python without these deps — still works; the extra protocols just go unavailable.
try:
    import pychromecast
    HAS_CHROMECAST = True
except Exception:
    HAS_CHROMECAST = False

try:
    import asyncio
    import pyatv
    HAS_AIRPLAY = True
except Exception:
    HAS_AIRPLAY = False


def discover_chromecasts(timeout=4):
    """Blocking Chromecast scan. Returns (cast_objects, browser); each cast exposes
    .cast_info with friendly_name / host / port / uuid. Caller keeps the browser alive."""
    if not HAS_CHROMECAST:
        return [], None
    return pychromecast.get_chromecasts(timeout=timeout)


class _AsyncLoop:
    """A background asyncio event loop so pyatv's coroutines can be driven from the Tk thread.

    pyatv is asyncio-only; the rest of the app is thread-based. We run one private loop in a
    daemon thread and submit coroutines to it via run_coroutine_threadsafe.
    """
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass


def discover_airplay(aloop, timeout=4):
    """Scan for AirPlay / Apple TV receivers. Returns a list of pyatv configs (each has
    .name / .address / .identifier). Runs on the shared async loop."""
    if not HAS_AIRPLAY:
        return []
    async def _scan():
        return await pyatv.scan(aloop.loop, timeout=timeout)
    return aloop.run(_scan(), timeout=timeout + 6)


class DongleCaster:
    """ffmpeg → MPEG-TS → memory buffer → HTTP, for dongles/old TVs without Range.

    Auto-transcodes AC3/EAC3/DTS/TrueHD/MLP audio → AAC stereo 128k. Waits for
    a 50 MB prefill so the dongle doesn't start with a stutter and bail out.
    """

    def __init__(self, port):
        self.port = port
        self.proc = None
        self.srv = None
        self.thread = None
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.done = False
        self.served = 0
        self.duration = None

    def start(self, filepath, seek=None, callback=None):
        self.stop()
        self.buf = bytearray()
        self.done = False
        self.served = 0

        codecs, bitrate, duration = probe_file(filepath)
        acodec = codecs.get('audio', '')
        bad_audio = acodec in ('ac3', 'eac3', 'dts', 'dca', 'truehd', 'mlp')
        self.duration = format_duration(duration) if duration else None

        cmd = [resolve_binary('ffmpeg')]
        if seek:
            cmd += ['-ss', seek]
        cmd += ['-i', filepath, '-c:v', 'copy']
        if bad_audio:
            cmd += ['-c:a', 'aac', '-ac', '2', '-b:a', '128k']
            if callback:
                callback(f"[FFMPEG] audio {acodec}→AAC")
        else:
            cmd += ['-c:a', 'copy']
        if seek:
            cmd += ['-output_ts_offset', seek]
        cmd += ['-f', 'mpegts', 'pipe:1']

        if callback:
            callback("[FFMPEG] Buffering...")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     preexec_fn=_PREEXEC)

        def reader():
            while self.proc and self.proc.poll() is None:
                chunk = self.proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                with self.lock:
                    self.buf.extend(chunk)
            self.done = True
        threading.Thread(target=reader, daemon=True).start()

        # Prefill: wait until 50 MB buffered or process exits
        for _ in range(120):
            time.sleep(0.5)
            with self.lock:
                sz = len(self.buf)
            if sz > 50 * 1024 * 1024:
                break
            if self.proc.poll() is not None:
                break
        if callback:
            callback(f"[FFMPEG] Ready ({sz // 1024 // 1024}MB prefill)")

        caster = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'video/MP2T')
                self.send_header('transferMode.dlna.org', 'Streaming')
                self.send_header('contentFeatures.dlna.org',
                                 'DLNA.ORG_OP=00;DLNA.ORG_FLAGS=01700000000000000000000000000000')
                self.end_headers()
                pos = 0
                stall = 0
                try:
                    while stall < 100:
                        # Snapshot length AND slice the bytes under a single lock so a
                        # concurrent stop()/reset can't leave `pos` past the buffer end.
                        with caster.lock:
                            buflen = len(caster.buf)
                            chunk = bytes(caster.buf[pos:pos + 64 * 1024]) if buflen > pos else b''
                        if chunk:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            pos += len(chunk)
                            caster.served = max(caster.served, pos)
                            stall = 0
                        elif caster.done:
                            break
                        else:
                            time.sleep(0.1)
                            stall += 1
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header('Content-Type', 'video/MP2T')
                self.send_header('transferMode.dlna.org', 'Streaming')
                self.end_headers()

            def log_message(self, *a):
                _file_log(f"[DONGLE-HTTP] {self.address_string()} " + (a[0] % a[1:] if a else ''))

        self.srv = socketserver.ThreadingTCPServer(('0.0.0.0', self.port), Handler)
        self.srv.allow_reuse_address = True
        self.srv.daemon_threads = True
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        if self.srv:
            try:
                self.srv.shutdown()
            finally:
                try:
                    self.srv.server_close()
                except Exception:
                    pass
            self.srv = None
            self.thread = None
        self.buf = bytearray()
        self.done = False


# ============= 8-BIT KEYGEN MUSIC =============

try:
    import winsound
    HAS_SOUND = True
except ImportError:
    HAS_SOUND = False

class ChiptunePlayer:
    def __init__(self):
        self.playing = False
        self.thread = None
        self.melody = [
            (880, 100), (0, 20), (880, 100), (0, 20), (784, 100), (880, 150),
            (1047, 200), (0, 50), (784, 150), (0, 50),
            (659, 100), (0, 20), (659, 100), (0, 20), (587, 100), (659, 150),
            (784, 200), (0, 50), (523, 150), (0, 100),
            (1047, 100), (988, 100), (880, 100), (784, 150), (0, 30),
            (880, 100), (784, 100), (659, 100), (587, 150), (0, 30),
            (659, 100), (587, 100), (523, 100), (494, 150), (0, 30),
            (523, 200), (0, 100),
            (523, 60), (659, 60), (784, 60), (1047, 60),
            (784, 60), (659, 60), (523, 60), (392, 60),
            (440, 60), (523, 60), (659, 60), (880, 60),
            (659, 60), (523, 60), (440, 60), (392, 100), (0, 150),
        ]

    def play_loop(self):
        while self.playing:
            for freq, dur in self.melody:
                if not self.playing:
                    break
                if freq > 0 and HAS_SOUND:
                    try:
                        winsound.Beep(freq, dur)
                    except Exception:
                        time.sleep(dur / 1000)
                else:
                    time.sleep(dur / 1000)

    def start(self):
        if not self.playing:
            self.playing = True
            self.thread = threading.Thread(target=self.play_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.playing = False

# ============= BUILT-IN HTTP SERVER =============

# (http.server and socketserver already imported above for DongleCaster)

class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Serves ONLY the registered video + optional subtitle for this cast. Anything else → 404."""

    def __init__(self, *args, video_path=None, subtitle_path=None, subtitle_url=None,
                 video_url_name=None, subtitle_url_name=None,
                 total_size=0, **kwargs):
        self.video_path = video_path
        self.subtitle_path = subtitle_path
        self.subtitle_url = subtitle_url
        self.total_size = total_size  # non-zero → growing file mode (yt stream)
        # URL-served names — may be ASCII aliases to dodge DMR quirks with Cyrillic/spaces/etc
        self.video_url_name = video_url_name or (os.path.basename(video_path) if video_path else None)
        self.subtitle_url_name = subtitle_url_name or (os.path.basename(subtitle_path) if subtitle_path else None)
        super().__init__(*args, **kwargs)

    def copyfile(self, source, outputfile):
        """Override with larger buffer (256KB instead of 16KB) for video streaming."""
        import shutil
        shutil.copyfileobj(source, outputfile, length=256 * 1024)

    def _resolve_path(self):
        """Match request basename to a whitelisted URL name. Returns local path or None."""
        req = urllib.parse.unquote(self.path.split('?', 1)[0].split('#', 1)[0])
        name = os.path.basename(req.rstrip('/'))
        if self.video_path and name == self.video_url_name:
            return self.video_path
        if self.subtitle_path and name == self.subtitle_url_name:
            return self.subtitle_path
        return None

    def send_head(self):
        range_header = self.headers.get('Range', '-')
        _file_log(f"[HTTP] req {self.command} {self.path} from {self.client_address[0]} Range={range_header} UA={self.headers.get('User-Agent','-')}")
        path = self._resolve_path()
        if path is None:
            _file_log(f"[HTTP] resp 404 — not in serve scope {self.path}")
            self.send_error(404, "Not Found")
            return None

        ctype = self.guess_type(path)
        is_video = ctype.startswith('video/')
        is_sub = os.path.splitext(path)[1].lower() in ('.srt', '.sub', '.smi', '.vtt')

        if is_sub:
            _file_log(f"[HTTP] resp subtitle {path}")
            return self._serve_subtitle(path, ctype)

        try:
            f = open(path, 'rb')
        except OSError as e:
            _file_log(f"[HTTP] resp 404 — open fail {path}: {e}")
            self.send_error(404, "File not found")
            return None

        fs = os.fstat(f.fileno())
        file_size = self.total_size if self.total_size else fs.st_size

        if range_header and range_header != '-':
            match = re.match(r'bytes=(\d*)-(\d*)', range_header)
            if match:
                start = int(match.group(1)) if match.group(1) else 0
                end = int(match.group(2)) if match.group(2) else file_size - 1
                if start >= file_size:
                    f.close()
                    _file_log(f"[HTTP] resp 416 (start={start} >= size={file_size})")
                    self.send_error(416, 'Range Not Satisfiable')
                    return None
                end = min(end, file_size - 1)
                # Growing file: wait until enough data is downloaded
                if self.total_size:
                    deadline = time.time() + 60
                    while time.time() < deadline:
                        current = os.fstat(f.fileno()).st_size
                        if current > start:
                            end = min(end, current - 1)
                            break
                        time.sleep(0.3)
                length = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(length))
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                self.send_header('Accept-Ranges', 'bytes')
                self._send_dlna_headers(is_video)
                self.end_headers()
                _file_log(f"[HTTP] resp 206 bytes {start}-{end}/{file_size} ctype={ctype}")
                f.seek(start)
                return _RangeFile(f, length)

        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(file_size))
        self.send_header('Accept-Ranges', 'bytes')
        self._send_dlna_headers(is_video)
        self.end_headers()
        _file_log(f"[HTTP] resp 200 size={file_size} ctype={ctype}")
        return f

    def _serve_subtitle(self, path, ctype):
        """Serve subtitle file with UTF-8 BOM prepended for LG WebOS compatibility."""
        try:
            with open(path, 'rb') as f:
                content = f.read()
        except OSError:
            self.send_error(404, "File not found")
            return None
        if not content.startswith(b'\xef\xbb\xbf'):
            content = b'\xef\xbb\xbf' + content
        self.send_response(200)
        self.send_header('Content-Type', f'{ctype}; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('transferMode.dlna.org', 'Interactive')
        self.end_headers()
        return io.BytesIO(content)

    def _send_dlna_headers(self, is_video):
        if is_video:
            self.send_header('transferMode.dlna.org', 'Streaming')
            # Минимум как в рабочем январском cast_to_lg.py — БЕЗ CI=0, БЕЗ Connection: keep-alive
            self.send_header('contentFeatures.dlna.org',
                             'DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000')
            if self.subtitle_url:
                self.send_header('CaptionInfo.sec', self.subtitle_url)
            _file_log(f"[HTTP] dlna headers: Streaming, OP=01 FLAGS=01700000..., CaptionInfo.sec={self.subtitle_url}")

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {
            '.mp4': 'video/mp4', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo',
            '.webm': 'video/webm', '.mov': 'video/quicktime', '.flv': 'video/flv',
            '.ts': 'video/MP2T', '.mpeg': 'video/mpeg', '.mpg': 'video/mpeg',
            '.wmv': 'video/x-ms-wmv', '.3gp': 'video/3gpp',
            '.srt': 'text/srt', '.sub': 'text/sub', '.smi': 'text/smi', '.vtt': 'text/vtt',
            '.ass': 'text/ass', '.ssa': 'text/ssa',
        }.get(ext, 'application/octet-stream')

    def log_message(self, format, *args):
        try:
            _file_log(f"[HTTP-access] {self.address_string()} {format % args}")
        except Exception:
            pass

class _RangeFile:
    def __init__(self, f, length):
        self.f, self.remaining = f, length
    def read(self, size=-1):
        if self.remaining <= 0:
            return b''
        if size < 0 or size > self.remaining:
            size = self.remaining
        data = self.f.read(size)
        self.remaining -= len(data)
        return data
    def close(self):
        self.f.close()

class SilentThreadingTCPServer(socketserver.ThreadingTCPServer):
    """ThreadingTCPServer: each request in its own thread. Suppresses DLNA disconnects."""
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        else:
            super().handle_error(request, client_address)

# Sites we hand to yt-dlp for resolution (vs. a direct media link, which we serve as-is).
_EXTRACTABLE_DOMAINS = (
    'youtube.com', 'youtu.be', 'yt.be', 'youtube-nocookie.com',
    'rutube.ru', 'vk.com', 'vkvideo.ru', 'ok.ru',
    'dailymotion.com', 'vimeo.com',
)

def is_extractable_url(text):
    """True for a page URL yt-dlp should resolve (YouTube, Rutube, …) rather than a direct
    media URL we can pass to the renderer unchanged."""
    if not text:
        return False
    host = text.split('://', 1)[-1].split('/', 1)[0].lower() if '://' in text else ''
    return any(host == d or host.endswith('.' + d) for d in _EXTRACTABLE_DOMAINS) \
        or any(d in text for d in _EXTRACTABLE_DOMAINS)

# Back-compat alias for older call sites.
is_youtube_url = is_extractable_url


class YoutubeStreamer:
    """Resolve a YouTube/Rutube/… URL with a single yt-dlp call, then mux it with ffmpeg into
    a growing MPEG-TS file on disk and serve that file to one or more renderers.

    Disk-backed (not an in-memory buffer) so multi-hour videos can't OOM the app, and the one
    growing file can feed every connected renderer — exactly what multi-room fan-out needs.
    Playback starts a few seconds in; we never wait for the whole download.
    """

    # Force H.264 (avc1) video, not AV1/VP9: cheap dongles & TVs decode H.264 in hardware but
    # play AV1 as audio-only (black screen). Cap at 1080p for the same reason.
    _FORMAT = ('bestvideo[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/'
               'best[vcodec^=avc1][ext=mp4]/best[ext=mp4]/best')

    def __init__(self, url, port, callback=None):
        self.url = url
        self.port = port
        self.callback = callback or (lambda m: None)
        self.total_size = 0      # approximate, informational (the live stream has no fixed length)
        self.duration = None
        self.title = None
        self._dir = None
        self.path = None         # growing .ts file on disk
        self.proc = None         # ffmpeg
        self.srv = None
        self.thread = None
        self._done = False        # ffmpeg finished muxing
        self._completed = False   # a client has streamed the file to EOF (→ don't replay)
        self._is_hls = False      # source is HLS (m3u8) → re-encode video, don't copy
        self._error = None

    def _ytdlp(self, *extra):
        cmd = [resolve_binary('yt-dlp'), '--no-playlist']
        if 'youtube.com' in self.url or 'youtu.be' in self.url:
            cmd += ['--extractor-args', 'youtube:player_client=android_vr,web']
        return cmd + list(extra)

    def probe(self):
        """One `yt-dlp -J` call → title, duration, approx size, and direct stream URL(s).
        Returns the URL list (1 = progressive, 2 = separate video+audio) or [] on failure."""
        try:
            r = subprocess.run(self._ytdlp('-f', self._FORMAT, '-J', self.url),
                               capture_output=True, text=True, timeout=60)
            info = json.loads(r.stdout) if r.stdout.strip() else None
        except Exception as e:
            self._error = f'yt-dlp probe failed: {e}'
            self.callback(f"[YT] {self._error}")
            return []
        if not isinstance(info, dict):
            # yt-dlp returned nothing usable (rate-limited, geo-blocked, extractor error, …)
            self._error = 'yt-dlp returned no data: ' + ((r.stderr or '').strip()[-200:] or 'empty')
            self.callback(f"[YT] {self._error}")
            return []
        self.title = info.get('title')
        if info.get('duration'):
            self.duration = format_duration(info['duration'])
        fmts = info.get('requested_formats') or ([info] if info.get('url') else [])
        urls = [f.get('url') for f in fmts if f.get('url')]
        self.total_size = sum(int(f.get('filesize') or f.get('filesize_approx') or 0) for f in fmts)
        self._is_hls = any('m3u8' in (f.get('protocol') or '') or 'hls' in (f.get('protocol') or '')
                           for f in fmts)
        if not urls:
            self._error = 'no playable stream URL'
            self.callback(f"[YT] {self._error}")
        return urls

    def start(self, stream_urls, seek_seconds=0, max_width=1280):
        """Mux the resolved stream URL(s) into a growing MPEG-TS temp file and start serving.

        seek_seconds: start playback this many seconds in (input seek — fast). max_width: cap
        the re-encoded HLS video width (1280 = 720p default; 1920 = 1080p)."""
        import tempfile
        self._dir = tempfile.mkdtemp(prefix='casttotv_')
        self.path = os.path.join(self._dir, 'stream.ts')
        cmd = [resolve_binary('ffmpeg'), '-loglevel', 'error']
        for su in stream_urls:
            if seek_seconds:
                cmd += ['-ss', str(int(seek_seconds))]   # per-input seek keeps A/V aligned
            cmd += ['-i', su]
        if len(stream_urls) == 2:
            cmd += ['-map', '0:v:0', '-map', '1:a:0']
        if self._is_hls:
            # HLS segments concatenated by `-c:v copy` leave timestamp discontinuities → dongles
            # freeze at segment joins. Re-encode to clean H.264. Use *baseline* (no B-frames, no
            # CABAC), the profile cheap hardware decoders handle reliably — main/high B-frames make
            # some dongles show frame 1 then freeze. Keyframe every ~2s.
            level = '4.0' if max_width > 1280 else '3.1'   # 4.0 needed for 1080p
            maxrate = '10M' if max_width > 1280 else '6M'
            cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
                    '-profile:v', 'baseline', '-level', level, '-pix_fmt', 'yuv420p',
                    '-vf', f"scale='min({max_width},iw)':-2", '-g', '48', '-bf', '0',
                    '-maxrate', maxrate, '-bufsize', '20M']
        else:
            cmd += ['-c:v', 'copy']   # progressive H.264 (YouTube etc.) — copy is fine and fast
        cmd += ['-c:a', 'aac', '-ac', '2', '-b:a', '128k', '-f', 'mpegts', self.path]
        self.callback("[YT] Muxing stream to disk (ffmpeg)...")
        self._errlog = os.path.join(self._dir, 'ffmpeg.log')
        errf = open(self._errlog, 'w')
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf,
                                     preexec_fn=_PREEXEC)
        proc = self.proc   # local ref: stop() may null self.proc while _wait runs

        def _wait():
            proc.wait()
            errf.close()
            self._done = True
            if proc.returncode == 0:
                self.callback("[YT] Source finished")
            else:
                tail = ''
                try:
                    with open(self._errlog) as f:
                        tail = f.read().strip().splitlines()[-2:]
                        tail = ' | '.join(tail)
                except OSError:
                    pass
                self.callback(f"[YT] ffmpeg exit {proc.returncode}: {tail}")
        threading.Thread(target=_wait, daemon=True).start()
        self._serve()

    def wait_prefill(self, mb=4, timeout=40):
        """Block until ~mb MB are on disk so the renderer doesn't catch up to an empty file."""
        target = mb * 1024 * 1024
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if os.path.getsize(self.path) >= target:
                    break
            except OSError:
                pass
            if self._done:
                break
            time.sleep(0.3)
        try:
            self.callback(f"[YT] Buffered {os.path.getsize(self.path)//1024//1024} MB — streaming")
        except OSError:
            pass

    def _serve(self):
        streamer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _hdr(self):
                self.send_response(200)
                self.send_header('Content-Type', 'video/MP2T')
                self.send_header('transferMode.dlna.org', 'Streaming')
                self.send_header('contentFeatures.dlna.org',
                                 'DLNA.ORG_OP=00;DLNA.ORG_FLAGS=01700000000000000000000000000000')
                self.end_headers()

            def do_HEAD(self):
                self._hdr()

            def do_GET(self):
                self._hdr()
                # Play-once: once the whole file has been streamed to a client, a DLNA dongle
                # that re-requests the URL on EOF would otherwise loop. Serve nothing so it stops.
                if streamer._completed:
                    return
                # ffmpeg may not have created the file yet on a very early connect — wait briefly.
                waited = 0
                while not os.path.exists(streamer.path) and waited < 100 and not streamer._done:
                    time.sleep(0.1)
                    waited += 1
                if not os.path.exists(streamer.path):
                    return
                stall = 0
                try:
                    with open(streamer.path, 'rb') as f:   # tail the growing file
                        while stall < 200:
                            chunk = f.read(64 * 1024)
                            if chunk:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                                stall = 0
                            elif streamer._done:
                                chunk = f.read(64 * 1024)   # final drain after ffmpeg exit
                                if chunk:
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                                    continue
                                streamer._completed = True  # reached true EOF — don't replay
                                break
                            else:
                                time.sleep(0.1)
                                stall += 1
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def log_message(self, *a):
                _file_log(f"[YT-HTTP] {self.address_string()} " + (a[0] % a[1:] if a else ''))

        self.srv = socketserver.ThreadingTCPServer(('0.0.0.0', self.port), Handler)
        self.srv.allow_reuse_address = True
        self.srv.daemon_threads = True
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def done(self):
        return self._done

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        if self.srv:
            try:
                self.srv.shutdown()
            finally:
                try:
                    self.srv.server_close()
                except Exception:
                    pass
            self.srv = None
            self.thread = None
        try:
            if self.path and os.path.exists(self.path):
                os.remove(self.path)
            if self._dir and os.path.isdir(self._dir):
                os.rmdir(self._dir)
        except OSError:
            pass


class HTTPServerThread:
    def __init__(self, port, video_path, subtitle_path=None,
                 video_url_name=None, subtitle_url_name=None, total_size=0):
        self.port = port
        self.video_path = os.path.abspath(video_path) if video_path else None
        self.subtitle_path = os.path.abspath(subtitle_path) if subtitle_path else None
        self.video_url_name = video_url_name or (os.path.basename(self.video_path) if self.video_path else None)
        self.subtitle_url_name = subtitle_url_name or (os.path.basename(self.subtitle_path) if self.subtitle_path else None)
        self.subtitle_url = None
        self.total_size = total_size
        self.server = None
        self.thread = None
        self.running = False

    def start(self):
        if self.running:
            return
        handler = lambda *args, **kwargs: RangeRequestHandler(
            *args,
            video_path=self.video_path,
            subtitle_path=self.subtitle_path,
            subtitle_url=self.subtitle_url,
            video_url_name=self.video_url_name,
            subtitle_url_name=self.subtitle_url_name,
            total_size=self.total_size,
            **kwargs)
        self.server = SilentThreadingTCPServer(('0.0.0.0', self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
            finally:
                # MUST close the listening socket — on Windows w/ SO_REUSEADDR a fresh
                # server can otherwise bind alongside the dead one, and the OS will hand
                # incoming SYNs to either, causing timeouts on the dead half.
                self.server.server_close()
            self.server = None
            self.thread = None
            self.running = False

# ============= DLNA FUNCTIONS =============

# MIME map для DIDL-Lite (HTTP Content-Type определяется отдельно в RangeRequestHandler.guess_type).
# В реальной DIDL-метадате мы всегда используем video/mp4 — LG webOS так делает совместимо.
MIME_MAP = {
    '.mp4': 'video/mp4', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo',
    '.mov': 'video/quicktime', '.wmv': 'video/x-ms-wmv',
    '.ts': 'video/mp2t', '.m2ts': 'video/mp2t',
    '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg',
    '.webm': 'video/webm', '.flv': 'video/x-flv', '.3gp': 'video/3gpp',
}

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()

def get_network_prefix():
    ip = get_local_ip()
    return '.'.join(ip.split('.')[:3]) + '.'

# ---------- SSDP Discovery (primary) ----------

def discover_dlna_renderers(timeout=2, retries=3, callback=None, cancel_check=None):
    """SSDP multicast M-SEARCH → collect LOCATIONs → parallel XML fetch."""
    SSDP_ADDR = '239.255.255.250'
    SSDP_PORT = 1900
    msg = ('M-SEARCH * HTTP/1.1\r\n'
           f'HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n'
           'MAN: "ssdp:discover"\r\n'
           f'MX: {max(1, timeout)}\r\n'
           'ST: urn:schemas-upnp-org:service:AVTransport:1\r\n\r\n')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", 4))
    sock.settimeout(0.3)

    if callback:
        callback("[SSDP] M-SEARCH multicast...")

    locations = {}
    raw_count = 0
    try:
        for _ in range(retries):
            sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
            time.sleep(0.05)

        end_time = time.time() + timeout
        while time.time() < end_time:
            if cancel_check and cancel_check():
                break
            try:
                data, addr = sock.recvfrom(65507)
                raw_count += 1
                response = data.decode('utf-8', errors='ignore')
                loc_match = re.search(r'\nlocation:\s*(.+)\r', response, re.IGNORECASE)
                if loc_match:
                    location = loc_match.group(1).strip()
                    if location not in locations:
                        locations[location] = addr[0]
                        if callback:
                            callback(f"[SSDP] {addr[0]} → {location}")
            except socket.timeout:
                continue
            except Exception:
                break
    finally:
        sock.close()

    if callback:
        callback(f"[SSDP] {raw_count} reply(s), {len(locations)} unique location(s)")

    return _parse_locations_parallel(locations.keys(), callback=callback, cancel_check=cancel_check)


def _parse_locations_parallel(location_urls, callback=None, cancel_check=None):
    """Fetch + parse a batch of device-description URLs concurrently. Returns list of devices."""
    urls = list(location_urls)
    devices = []
    if not urls:
        return devices
    fetch = functools.partial(_parse_device_description, callback=callback)
    workers = min(20, len(urls))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch, url): url for url in urls}
        for fut in concurrent.futures.as_completed(futures):
            if cancel_check and cancel_check():
                break
            try:
                device = fut.result()
            except Exception:
                device = None
            if device:
                devices.append(device)
                if callback:
                    callback(f"[OK] {device['friendly_name']} ({device['ip']}:{device['port']})")
    return devices


def _parse_device_description(location_url, callback=None):
    """Fetch device description XML from LOCATION URL, extract friendlyName + AVTransport controlURL."""
    try:
        xml_raw = urllib.request.urlopen(location_url, timeout=5).read().decode('utf-8', errors='ignore')
    except Exception as e:
        if DEBUG_VERBOSE and callback:
            callback(f"[DBG PARSE] HTTP fail {location_url}: {type(e).__name__}: {e}")
        raise
    if DEBUG_VERBOSE and callback:
        callback(f"[DBG PARSE] {location_url} → {len(xml_raw)} bytes")
    xml_clean = re.sub(r'\sxmlns="[^"]*"', '', xml_raw, count=1)
    try:
        root = ET.fromstring(xml_clean)
    except ET.ParseError as e:
        if callback:
            callback(f"[DBG PARSE] XML parse fail {location_url}: {e}")
        raise

    device_el = root.find('./device')
    if device_el is None:
        if DEBUG_VERBOSE and callback:
            callback(f"[DBG PARSE] no <device> in {location_url}")
        return None

    friendly_name = device_el.findtext('./friendlyName', 'Unknown')

    control_path = None
    found_services = []
    for service in device_el.findall('.//service'):
        stype = service.findtext('serviceType', '')
        found_services.append(stype)
        if 'AVTransport' in stype:
            control_path = service.findtext('controlURL')
            break

    if not control_path:
        if DEBUG_VERBOSE and callback:
            callback(f"[DBG PARSE] {friendly_name}: no AVTransport service. Services: {found_services}")
        return None

    parsed = urlparse(location_url)
    return {
        'ip': parsed.hostname,
        'port': parsed.port,
        'friendly_name': friendly_name,
        'control_url': urljoin(location_url, control_path),
        'control_path': control_path,
        'location': location_url,
    }

# ---------- Fast LAN scan + combined discovery ----------

# Targeted DLNA ports: LG webOS (1809, 3000, 7250), dongles, generic UPnP, dynamic range
DLNA_SCAN_PORTS = [1780, 1782, 1790, 1800, 1809, 2020, 2700, 3000, 7000, 7250,
                   8008, 8060, 8080, 9000, 49152, 49153, 49154, 49595]


def fast_lan_scan(prefix, callback=None, cancel_check=None, port_timeout=0.15):
    """Parallel /24 TCP probe across DLNA_SCAN_PORTS. Returns sorted list of live IPs."""
    targets = [(prefix + str(i), p) for i in range(1, 255) for p in DLNA_SCAN_PORTS]

    def probe(ip, port):
        if cancel_check and cancel_check():
            return None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(port_timeout)
            r = s.connect_ex((ip, port))
            s.close()
            if r == 0:
                return ip
        except Exception:
            pass
        return None

    hosts = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=400) as ex:
        for ip in ex.map(lambda t: probe(*t), targets, chunksize=20):
            if ip:
                hosts.add(ip)
    ordered = sorted(hosts, key=lambda x: tuple(int(o) for o in x.split('.')))
    if callback:
        callback(f"[LAN] {prefix}0/24: {len(ordered)} host(s) with DLNA ports open")
    return ordered


def _ssdp_unicast_lookup(ip, timeout=1.0):
    """Send M-SEARCH directly to host, return device dict or None."""
    msg = ('M-SEARCH * HTTP/1.1\r\n'
           'HOST: 239.255.255.250:1900\r\n'
           'MAN: "ssdp:discover"\r\n'
           'MX: 1\r\n'
           'ST: urn:schemas-upnp-org:service:AVTransport:1\r\n\r\n')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg.encode(), (ip, 1900))
        data, _ = s.recvfrom(65507)
        resp = data.decode('utf-8', errors='ignore')
        m = re.search(r'\nlocation:\s*(.+)\r', resp, re.IGNORECASE)
        if m:
            try:
                return _parse_device_description(m.group(1).strip())
            except Exception:
                return None
    except Exception:
        return None
    finally:
        s.close()
    return None


def fast_discover(callback=None, cancel_check=None, on_device=None):
    """SSDP multicast + parallel /24 port scan, run concurrently. Deduped, sorted by IP."""
    prefix = get_network_prefix()
    if callback:
        callback(f"[FAST] discover on {prefix}0/24 — SSDP + port scan")

    ssdp_devices = []
    lan_hosts = []
    devices_by_key = {}
    covered_ips = set()
    _lock = threading.Lock()

    def _emit(d, label=None):
        key = (d['ip'], d['port'])
        with _lock:
            if key in devices_by_key:
                return False
            devices_by_key[key] = d
            covered_ips.add(d['ip'])
        if callback and label:
            callback(label)
        if on_device:
            on_device(d)
        return True

    def do_ssdp():
        devs = discover_dlna_renderers(timeout=2, retries=3,
                                       callback=callback,
                                       cancel_check=cancel_check)
        for d in devs:
            _emit(d, f"[OK] {d['friendly_name']} ({d['ip']}:{d['port']})")
        ssdp_devices.extend(devs)

    def do_lan():
        lan_hosts.extend(fast_lan_scan(prefix, callback=callback,
                                        cancel_check=cancel_check))

    t1 = threading.Thread(target=do_ssdp, daemon=True)
    t2 = threading.Thread(target=do_lan, daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # For LAN hosts that didn't reply to multicast SSDP, try unicast in parallel
    unicast_ips = [ip for ip in lan_hosts if ip not in covered_ips]
    if unicast_ips and not (cancel_check and cancel_check()):
        if callback:
            callback(f"[FAST] {len(unicast_ips)} host(s) without SSDP reply — unicast probe")
        workers = min(20, len(unicast_ips))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for d in ex.map(_ssdp_unicast_lookup, unicast_ips):
                if d:
                    _emit(d, f"[OK] {d['friendly_name']} ({d['ip']}:{d['port']})")

    # Last resort: hosts that responded to TCP but have no SSDP at all (some LG webOS TVs).
    deep_ips = [ip for ip in lan_hosts if ip not in covered_ips]
    if deep_ips and not (cancel_check and cancel_check()):
        if callback:
            callback(f"[FAST] {len(deep_ips)} host(s) need deep scan (no SSDP)")
        def _deep(ip):
            return find_dlna_on_host(ip, callback=callback, cancel_check=cancel_check)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(deep_ips))) as ex:
            for d in ex.map(_deep, deep_ips):
                if d:
                    _emit(d, f"[OK] {d['friendly_name']} ({d['ip']}:{d['port']}) via deep scan")

    return sorted(devices_by_key.values(),
                   key=lambda x: (tuple(int(o) for o in x['ip'].split('.')), x['port']))

# ---------- Deep scan: find DLNA AVTransport on a single host ----------

DESC_PROBE_PATHS = ('/', '/description.xml', '/MediaRenderer/desc.xml',
                    '/dmr/SDM.xml', '/dmr/DeviceDescription.xml',
                    '/xml/device_description.xml', '/DeviceDescription.xml',
                    '/rootDesc.xml', '/upnp/dev/MediaRenderer/desc.xml')


def _scan_host_ports(ip, port_range, callback=None, cancel_check=None,
                     timeout=0.2, workers=800):
    """Parallel TCP probe of `port_range` on a single host. Returns sorted list of open ports."""
    ports = list(port_range)

    def probe(p):
        if cancel_check and cancel_check():
            return None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            r = s.connect_ex((ip, p))
            s.close()
            if r == 0:
                return p
        except Exception:
            pass
        return None

    open_ports = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for p in ex.map(probe, ports):
            if p:
                open_ports.append(p)
    open_ports.sort()
    if callback:
        callback(f"[DEEP] {ip}: {len(open_ports)} open port(s)")
    return open_ports


def find_dlna_on_host(ip, callback=None, cancel_check=None,
                     port_range=range(1000, 50001), http_timeout=1.5):
    """Find DLNA MediaRenderer on a known host by deep parallel port scan + HTTP probe.

    Many LG webOS TVs publish AVTransport on a random ephemeral port that no fixed list catches.
    Returns the moment we get a hit — cancels remaining HTTP probes via done_event.
    """
    if cancel_check and cancel_check():
        return None
    open_ports = _scan_host_ports(ip, port_range, callback=callback, cancel_check=cancel_check)
    if not open_ports:
        return None

    done_event = threading.Event()

    def http_probe(port):
        if done_event.is_set() or (cancel_check and cancel_check()):
            return None
        for path in DESC_PROBE_PATHS:
            if done_event.is_set():
                return None
            url = f'http://{ip}:{port}{path}'
            try:
                body = urllib.request.urlopen(url, timeout=http_timeout).read().decode('utf-8', 'ignore')
            except Exception:
                continue
            if 'AVTransport' in body and 'MediaRenderer' in body:
                try:
                    return _parse_device_description(url)
                except Exception:
                    continue
        return None

    workers = min(32, len(open_ports))
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    found = None
    try:
        futures = [ex.submit(http_probe, p) for p in open_ports]
        for fut in concurrent.futures.as_completed(futures):
            try:
                device = fut.result()
            except Exception:
                device = None
            if device:
                found = device
                done_event.set()
                if callback:
                    callback(f"[DEEP] {ip}:{device['port']} → {device['friendly_name']}")
                break
    finally:
        done_event.set()
        ex.shutdown(wait=False, cancel_futures=True)
    if not found and callback:
        callback(f"[DEEP] {ip}: no MediaRenderer in open ports {open_ports}")
    return found


def find_dlna_service(tv_ip, callback=None, cancel_check=None):
    """Manual CONNECT: SSDP unicast → parallel deep port scan + HTTP probe."""
    if callback:
        callback(f"[FIND] SSDP unicast to {tv_ip}:1900...")
    device = _ssdp_unicast_lookup(tv_ip, timeout=2.0)
    if device:
        if callback:
            callback(f"[OK] {device['friendly_name']} via SSDP ({tv_ip}:{device['port']})")
        return device
    if callback:
        callback(f"[FIND] No SSDP — deep port scan on {tv_ip}")
    return find_dlna_on_host(tv_ip, callback=callback, cancel_check=cancel_check)

# ---------- Cast / Stop ----------

def _soap_call(control_url, soap_action, body, callback=None, timeout=30):
    """Send SOAP request, return (ok, response_text_or_error). Verbose-logs on failure."""
    try:
        req = urllib.request.Request(control_url, data=body.encode(), headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPAction': f'"{soap_action}"'
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        text = resp.read().decode('utf-8', errors='ignore')
        if DEBUG_VERBOSE and callback:
            callback(f"[DBG SOAP] {soap_action.split('#')[-1]} → HTTP {resp.getcode()} ({len(text)}b)")
        return True, text
    except urllib.error.HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8', errors='ignore')
        except Exception:
            pass
        # Parse UPnP errorCode out of the SOAP fault so callers can detect specific codes
        # (e.g. 701 "Transition not available") even when the body is long.
        upnp_code = None
        if err_body:
            m = re.search(r'<errorCode>\s*(\d+)\s*</errorCode>', err_body)
            if m:
                upnp_code = m.group(1)
        if callback:
            callback(f"[DBG SOAP] {soap_action.split('#')[-1]} HTTP {e.code}" + (f" UPnP {upnp_code}" if upnp_code else ""))
            callback(f"[DBG SOAP] >>> control_url: {control_url}")
            callback(f"[DBG SOAP] >>> body sent: {body[:400]}{'...' if len(body)>400 else ''}")
            if err_body:
                callback(f"[DBG SOAP] <<< response: {err_body[:600]}{'...' if len(err_body)>600 else ''}")
        prefix = f"HTTP {e.code}" + (f" UPnP-{upnp_code}" if upnp_code else "")
        return False, f"{prefix}: {err_body[:200] if err_body else e.reason}"
    except Exception as e:
        if callback:
            callback(f"[DBG SOAP] {soap_action.split('#')[-1]} {type(e).__name__}: {e}")
        return False, str(e)


class MediaSource:
    """What gets handed to a cast backend: an HTTP(S) URL plus the metadata a renderer needs.

    Decouples *where the bytes come from* (local file / YouTube / Rutube / direct URL) from
    *which protocol plays it* (DLNA today; Chromecast / AirPlay slot in via play_on()).
    """
    def __init__(self, url, mime='video/mp4', title='Video', duration=None, subtitle_url=None):
        self.url = url
        self.mime = mime
        self.title = title
        self.duration = duration
        self.subtitle_url = subtitle_url


def cast_video(video_url, control_url, subtitle_url=None, title=None,
               video_mime='video/mp4', duration=None, callback=None):
    """Send video to DLNA renderer. Minimal January-style DIDL (LG webOS UP7750PTB verified).

    Auto-recovers from 701 "Transition not available" by sending Stop and retrying SetURI.

    `video_mime` defaults to 'video/mp4' (LG-friendly even for MKV containers). For dongle
    MPEG-TS streaming pass 'video/MP2T'. `duration` is an HH:MM:SS string — when supplied
    the remote control shows the timeline immediately instead of waiting on byte ranges.
    """
    sub_meta = ''
    extra_ns = ''
    if subtitle_url:
        sub_ext = os.path.splitext(subtitle_url.split('?')[0])[1].lstrip('.').lower()
        sub_type = {'srt': 'srt', 'vtt': 'vtt', 'sub': 'sub', 'smi': 'smi'}.get(sub_ext, 'srt')
        sub_meta = (f'<res protocolInfo="http-get:*:text/{sub_type}:*">{subtitle_url}</res>'
                    f'<sec:CaptionInfoEx sec:type="{sub_type}" sec:URIType="public">{subtitle_url}</sec:CaptionInfoEx>')
        extra_ns = ' xmlns:sec="http://www.sec.co.kr/"'

    title_xml = html.escape(title or 'Video')
    # video/mp4 в protocolInfo даже для MKV — LG webOS так умеет.
    # Минимум флагов: OP=01 + FLAGS=01700000... — НЕ добавлять PN/CI/size, они ломают LG.
    # Для MPEG-TS (video/MP2T) — OP=00 (no Range), флаги те же.
    op = '00' if video_mime == 'video/MP2T' else '01'
    proto = f'http-get:*:{video_mime}:DLNA.ORG_OP={op};DLNA.ORG_FLAGS=01700000000000000000000000000000'
    duration_attr = f' duration="{duration}"' if duration else ''
    didl = (f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            f' xmlns:dc="http://purl.org/dc/elements/1.1/"'
            f' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
            f' xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"'
            f'{extra_ns}>'
            f'<item id="0" parentID="-1" restricted="1">'
            f'<dc:title>{title_xml}</dc:title>'
            f'<upnp:class>object.item.videoItem.movie</upnp:class>'
            f'<res protocolInfo="{proto}"{duration_attr}>'
            f'{video_url}</res>'
            f'{sub_meta}'
            f'</item></DIDL-Lite>')

    set_uri = (f'<?xml version="1.0" encoding="utf-8"?>'
               f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
               f' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
               f'<s:Body><u:SetAVTransportURI xmlns:u='
               f'"urn:schemas-upnp-org:service:AVTransport:1">'
               f'<InstanceID>0</InstanceID>'
               f'<CurrentURI>{video_url}</CurrentURI>'
               f'<CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>'
               f'</u:SetAVTransportURI></s:Body></s:Envelope>')

    if callback:
        callback(f"[CAST] URL: {video_url}")
        callback(f"[CAST] title: {title or 'Video'}")

    def _send_set_uri():
        return _soap_call(control_url,
                          'urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI',
                          set_uri, callback=callback)

    ok, msg = _send_set_uri()
    # Auto-recover from transient TV-state UPnP faults: send Stop and retry once.
    #   701 = "Transition not available" — TV stuck in playing/transitioning state.
    #   716 = "Resource not found"       — happens after a GUI restart while TV still
    #         remembers the previous (now-gone) stream URL; Stop wipes the session.
    retry_codes = ('UPnP-701', 'UPnP-716')
    if not ok and any(c in msg for c in retry_codes):
        hit = next((c for c in retry_codes if c in msg), 'UPnP')
        if callback:
            callback(f"[CAST] {hit} detected — sending Stop and retrying SetURI")
        try:
            stop_playback(control_url, callback=callback)
            time.sleep(0.5)
        except Exception:
            pass
        ok, msg = _send_set_uri()
    if not ok:
        return False, msg

    time.sleep(1)

    play = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID><Speed>1</Speed>'
            '</u:Play></s:Body></s:Envelope>')

    ok, msg = _soap_call(control_url,
                          'urn:schemas-upnp-org:service:AVTransport:1#Play',
                          play, callback=callback)
    if ok:
        return True, "OK"
    return False, msg


def get_position(control_url, callback=None):
    """Get current playback position. Returns (position_secs, duration_secs)."""
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            '</u:GetPositionInfo></s:Body></s:Envelope>')
    ok, resp = _soap_call(control_url,
                           'urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo',
                           body, callback=callback, timeout=5)
    if not ok:
        raise RuntimeError(resp)
    def parse_time(tag):
        m = re.search(rf'<{tag}>(\d+):(\d+):(\d+)', resp)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        return 0
    return parse_time('RelTime'), parse_time('TrackDuration')


def seek_to(control_url, time_str, callback=None):
    """Seek to absolute position (HH:MM:SS format)."""
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:Seek xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            '<Unit>REL_TIME</Unit>'
            f'<Target>{time_str}</Target>'
            '</u:Seek></s:Body></s:Envelope>')
    ok, resp = _soap_call(control_url,
                           'urn:schemas-upnp-org:service:AVTransport:1#Seek',
                           body, callback=callback, timeout=10)
    if not ok:
        raise RuntimeError(resp)


def stop_playback(control_url, callback=None):
    stop_xml = ('<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
                ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body><u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                '<InstanceID>0</InstanceID>'
                '</u:Stop></s:Body></s:Envelope>')
    ok, resp = _soap_call(control_url,
                           'urn:schemas-upnp-org:service:AVTransport:1#Stop',
                           stop_xml, callback=callback, timeout=10)
    if not ok:
        raise RuntimeError(resp)


def pause_playback(control_url, callback=None):
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:Pause xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            '</u:Pause></s:Body></s:Envelope>')
    ok, resp = _soap_call(control_url,
                           'urn:schemas-upnp-org:service:AVTransport:1#Pause',
                           body, callback=callback, timeout=10)
    if not ok:
        raise RuntimeError(resp)


def resume_playback(control_url, callback=None):
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID><Speed>1</Speed>'
            '</u:Play></s:Body></s:Envelope>')
    ok, resp = _soap_call(control_url,
                           'urn:schemas-upnp-org:service:AVTransport:1#Play',
                           body, callback=callback, timeout=10)
    if not ok:
        raise RuntimeError(resp)

# ============= MATRIX RAIN =============

class MatrixRain:
    def __init__(self, canvas, w, h):
        self.canvas = canvas
        self.w, self.h = w, h
        self.cols = w // 14
        self.drops = [random.randint(-15, 0) for _ in range(self.cols)]
        self.chars = "CASTTOTV0123<>/\\"

    def update(self):
        self.canvas.delete("m")
        for i, d in enumerate(self.drops):
            x = i * 14 + 5
            c = random.choice(self.chars)
            self.canvas.create_text(x, d * 15, text=c, fill="#00FF00",
                                    font=(MONO, 10, "bold"), tags="m")
            for j in range(1, 5):
                if d - j > 0:
                    g = max(0, 180 - j * 40)
                    self.canvas.create_text(x, (d - j) * 15, text=random.choice(self.chars),
                                            fill=f"#00{g:02x}00", font=(MONO, 10), tags="m")
            self.drops[i] += 1
            if self.drops[i] * 15 > self.h + 60:
                self.drops[i] = random.randint(-8, 0)

# ============= KEYGEN GUI =============

# Default monospace family. "Consolas" only exists on Windows; on Linux/macOS Tk would fall
# back to an ugly proportional default, so we resolve a good installed mono at startup.
MONO = "Consolas"

def _pick_mono_font(root):
    """Pick the best installed monospace family (Consolas on Windows, a sane mono elsewhere)."""
    try:
        import tkinter.font as tkfont
        available = set(tkfont.families(root))
    except Exception:
        return "Consolas"
    for fam in ("Consolas", "DejaVu Sans Mono", "Ubuntu Mono", "Liberation Mono",
                "Noto Sans Mono", "Menlo", "Courier New", "Monospace"):
        if fam in available:
            return fam
    return "TkFixedFont"


class KeygenApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"CastToTV v{VERSION}")
        global MONO
        MONO = _pick_mono_font(self.root)
        # HiDPI scaling. winfo_screenmmwidth() is unreliable on X11 (often 0 or a bogus EDID
        # value → exploding scale), so we don't derive DPI from physical size. Order:
        #   1. explicit CASTTOTV_SCALE env override (e.g. "1.5"),
        #   2. Tk's own reported DPI via winfo_fpixels('1i') — reliable — only if clearly HiDPI,
        #   3. otherwise 1.0 with the native 720x780 layout.
        scale = 1.0
        try:
            env_scale = os.environ.get('CASTTOTV_SCALE')
            if env_scale:
                scale = max(1.0, float(env_scale))
            else:
                dpi = float(self.root.winfo_fpixels('1i'))  # px per inch as Tk sees it
                if dpi > 120:
                    scale = max(1.0, round(dpi / 96, 1))
        except Exception:
            scale = 1.0
        if scale > 1.0:
            self.root.tk.call('tk', 'scaling', scale)
        self.scale = scale
        self.root.geometry(f"{int(720 * scale)}x{int(780 * scale)}")
        self.root.resizable(True, True)
        self.root.configure(bg='#000000')

        self.discovered_device = None  # {ip, port, friendly_name, control_url, ...}
        self.device_list = []  # all discovered devices, mirrored in dev_combo
        self.server_port = 8766
        self.cancel_flag = False
        self.scanning = False

        self.music = ChiptunePlayer()
        self.music_on = False
        self.current_cast = None
        self._current_file = None
        self._seek_pos = 0
        self.http_server = None
        self.dongle_caster = None
        self.youtube_streamer = None
        self._cc_casts = {}       # uuid -> pychromecast.Chromecast (live, from discovery)
        self._cc_browser = None   # kept alive for the session so cast objects stay connected
        self._airplay_confs = {}  # identifier -> pyatv config (from discovery)
        self._aloop = None        # lazily-created background asyncio loop for pyatv
        self.paused = False

        # Truncate log file at startup so it holds only the current run
        try:
            with open(_LOG_PATH, 'w', encoding='utf-8') as f:
                f.write(f"=== CastToTV v{VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception:
            pass

        self.build_ui()
        self.matrix = MatrixRain(self.matrix_canvas, int(720 * self.scale), int(780 * self.scale))
        self.animate_matrix()

    def build_ui(self):
        self.matrix_canvas = tk.Canvas(self.root, width=int(720 * self.scale), height=int(780 * self.scale),
                                       bg='#000000', highlightthickness=0)
        self.matrix_canvas.place(x=0, y=0)

        frame = tk.Frame(self.root, bg='#000000')
        frame.place(relx=0.5, rely=0.5, anchor='center')

        # Framed keygen banner — shadow figlet art (baked, no runtime figlet dependency).
        # Width is derived from the content so the right border always lines up.
        _art = [
            r'  ___|    \     ___|__ __| __ __| _ \  __ __|\ \     /',
            r' |       _ \  \___ \   |      |  |   |    |   \ \   /',
            r' |      ___ \       |  |      |  |   |    |    \ \ /',
            r'\____|_/    _\_____/  _|     _| \___/    _|     \_/',
        ]
        _sub = 'multi-room media caster · DLNA · Chromecast · AirPlay · YouTube · Rutube'
        _footer = f'v{VERSION}   ·   near-synchronous casting   ·   2026'
        _w = max(len(s) for s in _art + [_sub, _footer])
        _top = '╔' + '═' * (_w + 2) + '╗'
        _mid = '╠' + '═' * (_w + 2) + '╣'
        _bot = '╚' + '═' * (_w + 2) + '╝'
        banner = '\n' + '\n'.join(
            [_top] + ['║ ' + s.ljust(_w) + ' ║' for s in _art] +
            [_mid, '║ ' + _sub.ljust(_w) + ' ║', '║ ' + _footer.ljust(_w) + ' ║', _bot]
        )
        tk.Label(frame, text=banner, font=(MONO, 8), fg='#00FF00', bg='#000000', justify='left').pack()

        # Music toggle
        top_row = tk.Frame(frame, bg='#000000')
        top_row.pack(fill='x', padx=10)
        self.music_btn = tk.Button(top_row, text="\u266b MUSIC OFF", font=(MONO, 8, "bold"),
            fg='#000000', bg='#555555', command=self.toggle_music, width=12, bd=2)
        self.music_btn.pack(side='right')

        # Log
        log_frame = tk.Frame(frame, bg='#001100', relief='sunken', bd=2)
        log_frame.pack(fill='x', padx=10, pady=5)
        self.log_text = tk.Text(log_frame, height=14, width=78, font=(MONO, 9),
            fg='#00FF00', bg='#001100', state='disabled')
        self.log_text.pack(padx=3, pady=3)

        # Manual IP entry + device info
        ip_frame = tk.Frame(frame, bg='#000000')
        ip_frame.pack(fill='x', padx=10, pady=3)
        tk.Label(ip_frame, text="[IP]", font=(MONO, 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left')
        self.ip_entry = tk.Entry(ip_frame, width=15, font=(MONO, 9), fg='#00FF00', bg='#001100')
        self.ip_entry.pack(side='left', padx=3)
        tk.Button(ip_frame, text="CONNECT", font=(MONO, 8, "bold"), fg='#000', bg='#FF9900',
            command=self.do_manual_connect, bd=2).pack(side='left', padx=3)
        self.status_lbl = tk.Label(ip_frame, text="READY", font=(MONO, 9, "bold"),
            fg='#FFFF00', bg='#000000', width=14)
        self.status_lbl.pack(side='right')

        # Device dropdown (populated by DISCOVER / CONNECT)
        dev_frame = tk.Frame(frame, bg='#000000')
        dev_frame.pack(fill='x', padx=10, pady=1)
        tk.Label(dev_frame, text="[TV]", font=(MONO, 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left')
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('Keygen.TCombobox',
                        fieldbackground='#001100', background='#003300',
                        foreground='#00FF00', arrowcolor='#00FF00',
                        bordercolor='#00AA00', lightcolor='#00AA00', darkcolor='#003300',
                        selectbackground='#003300', selectforeground='#00FF00')
        self.root.option_add('*TCombobox*Listbox.background', '#001100')
        self.root.option_add('*TCombobox*Listbox.foreground', '#00FF00')
        self.root.option_add('*TCombobox*Listbox.selectBackground', '#006600')
        self.root.option_add('*TCombobox*Listbox.selectForeground', '#000000')
        self.root.option_add('*TCombobox*Listbox.font', (MONO, 9))
        self.dev_combo = ttk.Combobox(dev_frame, state='readonly', font=(MONO, 9),
                                      style='Keygen.TCombobox', height=12)
        self.dev_combo.pack(side='left', padx=5, fill='x', expand=True)
        self.dev_combo.bind('<<ComboboxSelected>>', self._on_device_selected)
        self.dev_combo['values'] = ['<no devices — click DISCOVER>']
        self.dev_combo.current(0)

        # File
        file_frame = tk.Frame(frame, bg='#000000')
        file_frame.pack(fill='x', padx=10, pady=5)
        tk.Label(file_frame, text="[FILE]", font=(MONO, 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left')
        self.file_entry = tk.Entry(file_frame, width=52, font=(MONO, 9), fg='#00FF00', bg='#001100')
        self.file_entry.pack(side='left', padx=5)
        tk.Button(file_frame, text="[...]", font=(MONO, 9, "bold"), fg='#000', bg='#00FF00',
            command=self.browse, bd=2).pack(side='left')

        # Subtitles
        sub_frame = tk.Frame(frame, bg='#000000')
        sub_frame.pack(fill='x', padx=10, pady=2)
        tk.Label(sub_frame, text="[SUBS]", font=(MONO, 9, "bold"), fg='#00CCFF', bg='#000000').pack(side='left')
        self.sub_entry = tk.Entry(sub_frame, width=52, font=(MONO, 9), fg='#00CCFF', bg='#001100')
        self.sub_entry.pack(side='left', padx=5)
        tk.Button(sub_frame, text="[...]", font=(MONO, 9, "bold"), fg='#000', bg='#00CCFF',
            command=self.browse_subs, bd=2).pack(side='left')

        # Streaming mode toggle: force MPEG-TS via ffmpeg (for dongles or AC3/DTS audio)
        mode_frame = tk.Frame(frame, bg='#000000')
        mode_frame.pack(fill='x', padx=10, pady=1)
        self.dongle_mode_var = tk.IntVar(value=0)
        ffmpeg_tip = "" if HAS_FFMPEG else " — needs ffmpeg, NOT INSTALLED"
        tk.Checkbutton(mode_frame,
                       text=f"[MODE] Force MPEG-TS (dongle / AC3 / DTS{ffmpeg_tip})",
                       variable=self.dongle_mode_var,
                       font=(MONO, 8, "bold"),
                       fg='#FFAA00', bg='#000000',
                       activeforeground='#FFCC00', activebackground='#000000',
                       selectcolor='#001100',
                       state='normal' if HAS_FFMPEG else 'disabled').pack(side='left')

        # Multi-room: fan the same stream out to every discovered room at once.
        self.multi_var = tk.IntVar(value=0)
        tk.Checkbutton(mode_frame,
                       text="[MULTI] all rooms (sync)",
                       variable=self.multi_var,
                       font=(MONO, 8, "bold"),
                       fg='#00FFAA', bg='#000000',
                       activeforeground='#00FFCC', activebackground='#000000',
                       selectcolor='#001100').pack(side='right')

        # Buttons row 1
        btn1 = tk.Frame(frame, bg='#000000')
        btn1.pack(pady=8)
        tk.Button(btn1, text="< DISCOVER >", font=(MONO, 10, "bold"), fg='#000', bg='#00FFFF',
            command=self.do_discover, width=14, bd=3).pack(side='left', padx=5)
        tk.Button(btn1, text="DONGLE WiFi", font=(MONO, 9, "bold"), fg='#000', bg='#FF9900',
            command=self.do_dongle_setup, width=11, bd=3).pack(side='left', padx=5)

        # Buttons row 2
        btn2 = tk.Frame(frame, bg='#000000')
        btn2.pack(pady=5)
        self.cancel_btn = tk.Button(btn2, text="< CANCEL >", font=(MONO, 10, "bold"), fg='#000', bg='#FF0000',
            command=self.cancel, width=12, bd=3, state='disabled')
        self.cancel_btn.pack(side='left', padx=5)
        tk.Button(btn2, text="<<< CAST >>>", font=(MONO, 10, "bold"), fg='#000', bg='#FF6600',
            command=self.do_cast, width=14, bd=3).pack(side='left', padx=5)
        self.pause_btn = tk.Button(btn2, text="< PAUSE >", font=(MONO, 10, "bold"), fg='#000', bg='#FFAA00',
            command=self.do_pause, width=11, bd=3)
        self.pause_btn.pack(side='left', padx=5)
        tk.Button(btn2, text="< STOP >", font=(MONO, 10, "bold"), fg='#000', bg='#AA0000',
            command=self.do_stop, width=10, bd=3).pack(side='left', padx=5)

        # Seek buttons
        seek_frame = tk.Frame(frame, bg='#000000')
        seek_frame.pack(pady=3)
        for label, sec in [("<<30s", -30), ("<<10s", -10), (">>10s", 10), (">>30s", 30), (">>5m", 300)]:
            tk.Button(seek_frame, text=label, font=(MONO, 8, "bold"), fg='#00FF00', bg='#003300',
                command=lambda s=sec: self.do_seek(s), width=6, bd=2).pack(side='left', padx=2)

        # Now Playing label
        self.now_playing = tk.Label(frame, text="[NOW] Nothing", font=(MONO, 8),
            fg='#888888', bg='#000000', anchor='w')
        self.now_playing.pack(fill='x', padx=10, pady=2)

        # Footer
        tk.Label(frame, text=("\u2550" * 67 + "\n" +
            "  Greets: Scene 2005 | #warez | The good old days\n" +
            f"  HTTP Server built-in  *  Port {self.server_port}  *  All-in-one"),
            font=(MONO, 8), fg='#006600', bg='#000000').pack(pady=3)

        self.log(f"[SYS] CastToTV v{VERSION} initialized")
        self.log("[SYS] Click DISCOVER or enter IP and click CONNECT")

    def animate_matrix(self):
        self.matrix.update()
        self.root.after(90, self.animate_matrix)

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        def _write():
            self.log_text.config(state='normal')
            self.log_text.insert('end', line + '\n')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
        self.root.after(0, _write)
        try:
            with open(_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def toggle_music(self):
        if self.music_on:
            self.music.stop()
            self.music_on = False
            self.music_btn.config(text="\u266b MUSIC OFF", bg='#555555')
        else:
            self.music.start()
            self.music_on = True
            self.music_btn.config(text="\u266b MUSIC ON", bg='#FF00FF')

    def browse(self):
        f = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.avi *.webm *.mov"), ("All", "*.*")])
        if f:
            self.file_entry.delete(0, 'end')
            self.file_entry.insert(0, f)

    def browse_subs(self):
        f = filedialog.askopenfilename(filetypes=[("Subtitles", "*.srt *.sub *.smi *.vtt"), ("All", "*.*")])
        if f:
            self.sub_entry.delete(0, 'end')
            self.sub_entry.insert(0, f)

    def set_scanning(self, state):
        self.scanning = state
        self.cancel_flag = False
        self.cancel_btn.config(state='normal' if state else 'disabled')
        self.status_lbl.config(text="SCANNING..." if state else "READY",
                               fg='#FF6600' if state else '#FFFF00')

    def cancel(self):
        self.cancel_flag = True
        self.log("[!] Cancelled by user")

    def _device_label(self, device):
        tag = device.get('protocol', 'dlna').upper()
        return f"[{tag}] {device['friendly_name']} — {device['ip']}:{device['port']}"

    def _refresh_dev_combo(self, select_index=None):
        def _apply():
            if self.device_list:
                self.dev_combo['values'] = [self._device_label(d) for d in self.device_list]
                idx = select_index if select_index is not None else self.dev_combo.current()
                if idx < 0 or idx >= len(self.device_list):
                    idx = 0
                self.dev_combo.current(idx)
                self._activate_device(self.device_list[idx])
            else:
                self.dev_combo['values'] = ['<no devices found>']
                self.dev_combo.current(0)
                self.discovered_device = None
        self.root.after(0, _apply)

    def _activate_device(self, device):
        self.discovered_device = device
        self.ip_entry.delete(0, 'end')
        self.ip_entry.insert(0, device['ip'])
        self.status_lbl.config(text=f"PORT {device['port']}", fg='#00FF00')

    def _on_device_selected(self, event=None):
        idx = self.dev_combo.current()
        if 0 <= idx < len(self.device_list):
            self._activate_device(self.device_list[idx])
            self.log(f"[SEL] {self._device_label(self.device_list[idx])}")

    def _populate_devices(self, devices):
        self.device_list = list(devices)
        self._refresh_dev_combo(select_index=0 if devices else None)

    def _add_device(self, device):
        """Add a device (e.g. from manual CONNECT) and select it. Dedupes by ip:port."""
        key = (device['ip'], device['port'])
        for i, d in enumerate(self.device_list):
            if (d['ip'], d['port']) == key:
                self._refresh_dev_combo(select_index=i)
                return
        self.device_list.append(device)
        self._refresh_dev_combo(select_index=len(self.device_list) - 1)

    def do_dongle_setup(self):
        """Open WiFi dongle (Maxscreen/AnyCast/EZCast) web settings to reconfigure WiFi."""
        import webbrowser
        dongle_ips = ['192.168.49.1', '192.168.203.1', '192.168.1.1']
        ip = self.ip_entry.get().strip()
        if ip:
            dongle_ips.insert(0, ip)

        self.log("[DONGLE] Checking dongle web interface...")

        def run():
            for dip in dongle_ips:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    result = sock.connect_ex((dip, 80))
                    sock.close()
                    if result == 0:
                        url = f'http://{dip}'
                        self.log(f"[DONGLE] Found at {dip} — opening browser")
                        self.log("[TIP] Click 'WIFI AP' > 'Scan' > select your home WiFi")
                        self.log("[TIP] After dongle restarts, click DISCOVER")
                        webbrowser.open(url)
                        self.ip_entry.delete(0, 'end')
                        self.ip_entry.insert(0, dip)
                        return
                except Exception:
                    pass
            self.log("[ERR] Dongle not found. Connect to dongle WiFi first!")
            self.log("[TIP] Look for WiFi like 'Xhadapter-xxx' or 'AnyCast-xxx'")

        threading.Thread(target=run, daemon=True).start()

    def do_manual_connect(self):
        """Connect to a manually entered IP — scans for DLNA service, adds it to the dropdown."""
        ip = self.ip_entry.get().strip()
        if not ip:
            self.log("[ERR] Enter IP address first")
            return
        def run():
            self.set_scanning(True)
            self.log(f"[SCAN] Connecting to {ip}...")
            device = find_dlna_service(ip, self.log, lambda: self.cancel_flag)
            if device and not self.cancel_flag:
                self._add_device(device)
            elif not self.cancel_flag:
                self.log(f"[ERR] No DLNA service found on {ip}")
            self.set_scanning(False)
        threading.Thread(target=run, daemon=True).start()

    def do_discover(self):
        """Fast combined discovery: SSDP multicast + parallel TCP scan of DLNA ports across /24."""
        def run():
            self.set_scanning(True)
            self.device_list = []
            t0 = time.time()
            devices = fast_discover(callback=self.log,
                                    cancel_check=lambda: self.cancel_flag,
                                    on_device=lambda d: self.root.after(0, lambda dev=d: self._add_device(dev)))
            if HAS_CHROMECAST and not self.cancel_flag:
                self._discover_chromecasts()
            if HAS_AIRPLAY and not self.cancel_flag:
                self._discover_airplay()
            elapsed = time.time() - t0
            if self.cancel_flag:
                self.set_scanning(False)
                return
            if not self.device_list:
                self._populate_devices(devices)
            if devices:
                self.log(f"[OK] {len(devices)} renderer(s) in {elapsed:.1f}s")
            else:
                self.log(f"[WARN] No DLNA renderers found ({elapsed:.1f}s)")
                self.log("[TIP] Enter IP manually and click CONNECT")
            self.set_scanning(False)
        threading.Thread(target=run, daemon=True).start()

    def _discover_chromecasts(self):
        """Add Chromecast devices to the dropdown, tagged [CHROMECAST]. Keeps the cast objects
        alive in self._cc_casts so play_on() can reach them without re-scanning."""
        self.log("[CC] Scanning for Chromecast / Google Cast...")
        try:
            casts, browser = discover_chromecasts(timeout=4)
        except Exception as e:
            self.log(f"[CC] discovery error: {e}")
            return
        self._cc_browser = browser
        for cc in casts:
            ci = cc.cast_info
            uuid = str(ci.uuid)
            self._cc_casts[uuid] = cc
            dev = {'ip': ci.host, 'port': ci.port, 'friendly_name': ci.friendly_name,
                   'protocol': 'chromecast', 'uuid': uuid, 'control_url': None}
            self.root.after(0, lambda d=dev: self._add_device(d))
            self.log(f"[CC] {ci.friendly_name} ({ci.host})")

    def _ensure_aloop(self):
        if self._aloop is None:
            self._aloop = _AsyncLoop()
        return self._aloop

    def _discover_airplay(self):
        """Add AirPlay / Apple TV receivers to the dropdown, tagged [AIRPLAY]. Configs are
        kept in self._airplay_confs so play_on() can connect without re-scanning."""
        self.log("[AP] Scanning for AirPlay / Apple TV...")
        try:
            confs = discover_airplay(self._ensure_aloop(), timeout=4)
        except Exception as e:
            self.log(f"[AP] discovery error: {e}")
            return
        for c in confs:
            ident = c.identifier or str(c.address)
            self._airplay_confs[ident] = c
            dev = {'ip': str(c.address), 'port': 0, 'friendly_name': c.name,
                   'protocol': 'airplay', 'uuid': ident, 'control_url': None}
            self.root.after(0, lambda d=dev: self._add_device(d))
            self.log(f"[AP] {c.name} ({c.address})")

    def do_seek(self, delta_secs):
        """Seek via DLNA Seek SOAP — TV handles position natively for direct file streams."""
        if not self.discovered_device:
            return
        def run():
            try:
                control = self.discovered_device['control_url']
                try:
                    pos, dur = get_position(control, callback=self.log)
                except Exception:
                    pos, dur = self._seek_pos, 0
                new_pos = max(0, pos + delta_secs)
                self._seek_pos = new_pos
                seek_str = format_duration(new_pos)
                self.log(f"[SEEK] -> {seek_str}")
                seek_to(control, seek_str, callback=self.log)
                self.log(f"[OK] Seeked to {seek_str}")
            except Exception as e:
                self.log(f"[ERR] Seek failed: {e}")
        threading.Thread(target=run, daemon=True).start()

    def do_pause(self):
        """Toggle Pause/Resume via DLNA AVTransport."""
        if not self.discovered_device:
            self.log("[ERR] No TV — click DISCOVER first")
            return
        def run():
            control = self.discovered_device['control_url']
            try:
                if self.paused:
                    resume_playback(control, callback=self.log)
                    self.paused = False
                    self.root.after(0, lambda: self.pause_btn.config(text="< PAUSE >"))
                    self.root.after(0, lambda: self.status_lbl.config(text="PLAYING", fg='#00FF00'))
                    self.log("[OK] Resumed")
                else:
                    pause_playback(control, callback=self.log)
                    self.paused = True
                    self.root.after(0, lambda: self.pause_btn.config(text="< RESUME >"))
                    self.root.after(0, lambda: self.status_lbl.config(text="PAUSED", fg='#FFFF00'))
                    self.log("[OK] Paused")
            except Exception as e:
                self.log(f"[ERR] Pause/Resume failed: {e}")
        threading.Thread(target=run, daemon=True).start()

    def do_stop(self):
        if not self.discovered_device:
            self.log("[ERR] No TV discovered — click DISCOVER first")
            return
        def run():
            try:
                stop_playback(self.discovered_device['control_url'], callback=self.log)
            except Exception as e:
                self.log(f"[DBG STOP] {type(e).__name__}: {e}")
            if self.http_server:
                self.http_server.stop()
                self.http_server = None
            if self.dongle_caster:
                self.dongle_caster.stop()
                self.dongle_caster = None
            self.paused = False
            self.root.after(0, lambda: self.pause_btn.config(text="< PAUSE >"))
            self.log("[OK] Playback stopped")
            self.current_cast = None
            self._current_file = None
            self.now_playing.config(text="[NOW] Nothing", fg='#888888')
            self.status_lbl.config(text="STOPPED", fg='#FFFF00')
        threading.Thread(target=run, daemon=True).start()

    def play_on(self, target, source):
        """Dispatch a MediaSource to one cast target by its protocol. Returns (ok, message).

        Today only DLNA is wired; Chromecast / AirPlay backends slot in here without
        touching the rest of do_cast. Multi-room fan-out (Phase 4) will call this per target.
        """
        protocol = (target.get('protocol') if isinstance(target, dict) else None) or 'dlna'
        if protocol == 'dlna':
            return cast_video(source.url, target['control_url'],
                              subtitle_url=source.subtitle_url, video_mime=source.mime,
                              duration=source.duration, title=source.title, callback=self.log)
        if protocol == 'chromecast':
            return self._play_chromecast(target, source)
        if protocol == 'airplay':
            return self._play_airplay(target, source)
        if protocol == 'miracast':
            # Miracast mirrors the whole screen over Wi-Fi Direct — it's not an in-app HTTP
            # stream, so we can't push a single source to it from here. See README/help.
            return False, "Miracast is screen-mirroring only — use the system display helper"
        return False, f"protocol '{protocol}' not supported yet"

    def _dedupe_targets(self, devices):
        """Collapse multiple entries for the same physical box (a dongle that answers both
        DLNA and AirPlay shouldn't get the stream twice) — keep the first per IP."""
        seen, out = set(), []
        for d in devices:
            ip = d.get('ip')
            if ip in seen:
                continue
            seen.add(ip)
            out.append(d)
        return out

    def cast_to_all(self, targets, source):
        """Fan one MediaSource out to N rooms. A threading.Barrier releases every Play at the
        same instant (best-effort sync — independent clocks still drift a few seconds).
        Returns a list of (target, ok, message)."""
        results, lock = [], threading.Lock()
        barrier = threading.Barrier(len(targets))

        def fire(t):
            try:
                barrier.wait(timeout=12)   # line everyone up, then fire together
            except Exception:
                pass
            ok, msg = self.play_on(t, source)
            with lock:
                results.append((t, ok, msg))

        threads = [threading.Thread(target=fire, args=(t,), daemon=True) for t in targets]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        return results

    def _play_airplay(self, target, source):
        """Stream a MediaSource to an AirPlay receiver via pyatv (fire-and-forget on the loop)."""
        if not HAS_AIRPLAY:
            return False, "pyatv not installed"
        conf = self._airplay_confs.get(target.get('uuid'))
        if conf is None:
            return False, "AirPlay device not found — run DISCOVER again"
        aloop = self._ensure_aloop()

        async def _go():
            atv = await pyatv.connect(conf, aloop.loop)
            try:
                await atv.stream.play_url(source.url)
            finally:
                atv.close()

        try:
            fut = aloop.submit(_go())   # play_url runs until the clip ends; don't block the UI
        except Exception as e:
            return False, f"AirPlay error: {type(e).__name__}: {e}"

        def _done(f):
            exc = f.exception()
            if exc:
                self.log(f"[AP] play error: {type(exc).__name__}: {exc}")
        fut.add_done_callback(_done)
        return True, "AirPlay started"

    def _play_chromecast(self, target, source):
        """Hand a MediaSource to a Chromecast via pychromecast's MediaController."""
        if not HAS_CHROMECAST:
            return False, "pychromecast not installed"
        cc = self._cc_casts.get(target.get('uuid'))
        if cc is None:
            return False, "Chromecast not found — run DISCOVER again"
        # Chromecast can't decode MPEG-TS; our YouTube/dongle streams are TS. Direct mp4 is fine.
        if source.mime == 'video/MP2T':
            return False, "Chromecast can't play MPEG-TS — use a local MP4 or a direct video URL"
        try:
            cc.wait(timeout=10)
            mc = cc.media_controller
            mc.play_media(source.url, source.mime, title=source.title,
                          subtitles=source.subtitle_url or None)
            mc.block_until_active(timeout=10)
            return True, "Chromecast playing"
        except Exception as e:
            return False, f"Chromecast error: {type(e).__name__}: {e}"

    def do_cast(self):
        video = self.file_entry.get().strip()
        subs = self.sub_entry.get().strip()

        if not self.discovered_device:
            messagebox.showerror("Error", "Click DISCOVER first!")
            return
        if not video:
            messagebox.showerror("Error", "Select video file!")
            return

        control = self.discovered_device['control_url']

        def run():
            sub_url = None
            video_mime = 'video/mp4'
            duration = None
            if is_extractable_url(video):
                # ---- Extractable URL (YouTube / Rutube / …): yt-dlp → ffmpeg → growing TS → DLNA ----
                self.log("[YT] Resolving stream via yt-dlp...")
                # Free the shared dongle port and any previous extracted stream.
                if self.dongle_caster:
                    self.dongle_caster.stop()
                    self.dongle_caster = None
                if self.youtube_streamer:
                    self.youtube_streamer.stop()
                streamer = YoutubeStreamer(video, self.server_port + 2, callback=self.log)
                self.youtube_streamer = streamer
                stream_urls = streamer.probe()
                if not stream_urls:
                    self.log("[ERR] Could not resolve a playable stream")
                    self.status_lbl.config(text="FAILED", fg='#FF0000')
                    return
                self.log(f"[YT] {streamer.title or 'stream'} — {streamer.duration or 'unknown'}")
                local = get_local_ip()
                streamer.start(stream_urls)
                streamer.wait_prefill()
                url = f"http://{local}:{self.server_port + 2}/stream.ts"
                name = streamer.title or 'Stream'
                duration = streamer.duration
                video_mime = 'video/MP2T'
                self.log(f"[YT] Streaming: {url}")
            elif video.startswith("http"):
                url = video
                name = video.split('/')[-1][:40]
            else:
                if not os.path.exists(video):
                    self.log("[ERR] File not found!")
                    return

                local = get_local_ip()
                self.log(f"[DBG CAST] local IP: {local} (TV must reach this)")
                if local.startswith('127.') or local.startswith('169.254.'):
                    self.log(f"[WARN] local IP {local} is loopback/link-local — TV will NOT reach it")
                name = os.path.basename(video)
                video_abs = os.path.abspath(video)
                self._current_file = video
                self._seek_pos = 0

                file_size = os.path.getsize(video)
                self.log(f"[DBG CAST] file: {video} ({file_size} bytes)")

                # Probe once: pick streaming mode and grab duration for the DIDL timeline.
                bad_audio = None
                if HAS_FFMPEG:
                    bad_audio = needs_audio_transcode(video)
                    codecs, _, dur_secs = probe_file(video)
                    if dur_secs:
                        duration = format_duration(dur_secs)

                force_dongle = bool(self.dongle_mode_var.get())
                use_dongle = force_dongle or bool(bad_audio)
                if use_dongle and not HAS_FFMPEG:
                    self.log("[ERR] FFmpeg required for MPEG-TS mode but not installed")
                    return
                if bad_audio and not force_dongle:
                    self.log(f"[AUDIO] {bad_audio} detected — auto-switching to MPEG-TS + AAC transcode")
                elif force_dongle:
                    self.log("[MODE] Force MPEG-TS (dongle mode) — ffmpeg pipe")

                subtitle_abs = None
                sub_url_name = None
                if subs and os.path.exists(subs):
                    subtitle_abs = os.path.abspath(subs)
                    sub_ext = (os.path.splitext(subtitle_abs)[1] or '.srt').lower()
                    sub_url_name = f"subs{sub_ext}"
                    sub_url = f"http://{local}:{self.server_port}/{sub_url_name}"
                    self.log(f"[SUBS] {os.path.basename(subtitle_abs)} → {sub_url_name}")
                elif subs:
                    self.log(f"[WARN] Subtitle file not found: {subs}")

                if use_dongle:
                    # ---- MPEG-TS path (ffmpeg → in-memory buffer → HTTP) ----
                    # Stop any leftover direct-file server; we'll spin a subs-only one if needed.
                    if self.http_server and self.http_server.video_path:
                        self.http_server.stop()
                        self.http_server = None

                    if self.dongle_caster is None:
                        self.dongle_caster = DongleCaster(self.server_port + 2)
                    self.dongle_caster.start(video, callback=self.log)
                    url = f"http://{local}:{self.server_port + 2}/stream.ts"
                    video_mime = 'video/MP2T'
                    duration = self.dongle_caster.duration or duration
                    self.log(f"[DBG CAST] URL: {url}")

                    if subtitle_abs:
                        need_restart = (self.http_server is None
                                        or self.http_server.video_path is not None
                                        or self.http_server.subtitle_path != subtitle_abs
                                        or self.http_server.subtitle_url != sub_url)
                        if need_restart:
                            if self.http_server:
                                self.http_server.stop()
                            self.http_server = HTTPServerThread(self.server_port, None,
                                                                subtitle_path=subtitle_abs,
                                                                subtitle_url_name=sub_url_name)
                            self.http_server.subtitle_url = sub_url
                            try:
                                self.http_server.start()
                                self.log(f"[HTTP] Subs-only server on port {self.server_port}: {sub_url_name}")
                            except Exception as e:
                                self.log(f"[WARN] Subs HTTP: {e}")
                else:
                    # ---- Direct DLNA file path (Range + native seek) ----
                    if self.dongle_caster:
                        self.dongle_caster.stop()
                        self.dongle_caster = None

                    # ASCII-safe URL alias — LG webOS DMR rejects long URLs with Cyrillic/spaces/parens
                    # (UPnP 716). Title in DIDL keeps the original name for on-screen display.
                    video_ext = (os.path.splitext(name)[1] or '.mkv').lower()
                    video_url_name = f"video{video_ext}"

                    # Start/restart HTTP server scoped to ONLY this video (+ subtitle).
                    # Any other path on the port returns 404 — no directory listing, no other files.
                    need_restart = (self.http_server is None or
                                    self.http_server.video_path != video_abs or
                                    self.http_server.subtitle_path != subtitle_abs or
                                    self.http_server.subtitle_url != sub_url)
                    if need_restart:
                        if self.http_server:
                            self.http_server.stop()
                        self.http_server = HTTPServerThread(self.server_port, video_abs,
                                                            subtitle_path=subtitle_abs,
                                                            video_url_name=video_url_name,
                                                            subtitle_url_name=sub_url_name)
                        self.http_server.subtitle_url = sub_url
                        try:
                            self.http_server.start()
                            scope = f"{video_url_name} (= {name})" + (f" + {sub_url_name}" if sub_url_name else "")
                            self.log(f"[HTTP] Server on port {self.server_port} — serving only: {scope}")
                        except Exception as e:
                            self.log(f"[WARN] HTTP: {e}")

                    url = f"http://{local}:{self.server_port}/{video_url_name}"
                    self.log(f"[DBG CAST] URL: {url}")

                    # Self-test: can WE reach our own HTTP server with the URL we'll give to TV?
                    # If this fails, TV definitely can't either. If this succeeds but TV gets 716,
                    # the problem is firewall / routing between Windows and TV.
                    self.log(f"[SELFTEST] HEAD {url}")
                    try:
                        req = urllib.request.Request(url, method='HEAD')
                        resp = urllib.request.urlopen(req, timeout=3)
                        self.log(f"[SELFTEST] HEAD → HTTP {resp.getcode()} CL={resp.headers.get('Content-Length')} CT={resp.headers.get('Content-Type')}")
                    except Exception as e:
                        self.log(f"[SELFTEST] HEAD FAIL: {type(e).__name__}: {e}")
                    self.log(f"[SELFTEST] GET Range bytes=0-1023 {url}")
                    try:
                        req = urllib.request.Request(url, headers={'Range': 'bytes=0-1023'})
                        resp = urllib.request.urlopen(req, timeout=3)
                        body = resp.read()
                        self.log(f"[SELFTEST] Range → HTTP {resp.getcode()} got {len(body)}b CR={resp.headers.get('Content-Range')}")
                    except Exception as e:
                        self.log(f"[SELFTEST] Range FAIL: {type(e).__name__}: {e}")

            self.log(f"[CAST] {self.discovered_device['friendly_name']}")
            self.status_lbl.config(text="CASTING...", fg='#FF6600')
            title = os.path.splitext(name)[0] if not video.startswith('http') and not is_extractable_url(video) else name
            source = MediaSource(url, mime=video_mime, title=title,
                                 duration=duration, subtitle_url=sub_url)
            if self.multi_var.get() and len(self.device_list) > 1:
                targets = self._dedupe_targets(self.device_list)
                self.log(f"[MULTI] Casting to {len(targets)} room(s), synchronised start...")
                results = self.cast_to_all(targets, source)
                ok_n = sum(1 for _, ok_i, _ in results if ok_i)
                for t, ok_i, m in results:
                    self.log(f"[MULTI] {self._device_label(t)} -> {'OK' if ok_i else 'FAIL: ' + m}")
                ok = ok_n > 0
                msg = f"{ok_n}/{len(results)} room(s) playing"
            else:
                ok, msg = self.play_on(self.discovered_device, source)
            if ok:
                self.log("[OK] Streaming started!")
                if sub_url:
                    self.log("[OK] Subtitles attached")
                self.status_lbl.config(text="PLAYING", fg='#00FF00')
                self.paused = False
                self.root.after(0, lambda: self.pause_btn.config(text="< PAUSE >"))
                self.current_cast = name
                display_name = name[:50] + "..." if len(name) > 50 else name
                self.now_playing.config(text=f"[NOW] {display_name}", fg='#00FF00')
            else:
                self.log(f"[ERR] {msg}")
                self.status_lbl.config(text="FAILED", fg='#FF0000')

        threading.Thread(target=run, daemon=True).start()

    def on_close(self):
        self.music.stop()
        if self.http_server:
            self.http_server.stop()
        if self.dongle_caster:
            self.dongle_caster.stop()
        if self.youtube_streamer:
            self.youtube_streamer.stop()
        if self._cc_browser is not None:
            try:
                pychromecast.discovery.stop_discovery(self._cc_browser)
            except Exception:
                pass
        if self._aloop is not None:
            self._aloop.stop()
        self.root.destroy()

def main():
    root = tk.Tk()
    app = KeygenApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
