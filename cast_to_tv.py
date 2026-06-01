#!/usr/bin/env python3
"""
D3x LG WebOS TV CASTER v0.4.4-beta - DLNA Video Streaming Tool
KeyGen 2005 Style Interface with MUSIC!
"""

VERSION = "0.5.0-beta"
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

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cast_log.txt')

# ============= TRANSCODER =============

def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
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
            'ffprobe', '-v', 'error',
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

        cmd = ['ffmpeg']
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
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

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
                        with caster.lock:
                            avail = len(caster.buf) - pos
                        if avail > 0:
                            with caster.lock:
                                chunk = bytes(caster.buf[pos:pos + 64 * 1024])
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

    def start_youtube(self, url, duration_str=None, callback=None):
        """yt-dlp --get-url → ffmpeg direct stream → MPEG-TS → HTTP."""
        self.stop()
        self.buf = bytearray()
        self.done = False
        self.served = 0
        self.duration = duration_str
        self._yt_proc = None

        yt_env = {**os.environ,
                  'PATH': os.path.expanduser('~/.deno/bin') + ':' + os.environ.get('PATH', '')}

        # Get direct stream URL(s) from yt-dlp
        if callback:
            callback("[YT] Resolving stream URL via yt-dlp...")
        r = subprocess.run(
            ['yt-dlp',
             '--extractor-args', 'youtube:player_client=android_vr,web',
             '--no-playlist', '-f',
             'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
             '-g', url],
            capture_output=True, text=True, timeout=30, env=yt_env
        )
        stream_urls = [u.strip() for u in r.stdout.strip().splitlines() if u.strip()]
        if not stream_urls:
            if callback:
                callback(f"[YT] yt-dlp -g failed: {r.stderr.strip()[-200:]}")
            return

        if callback:
            callback(f"[YT] Got {len(stream_urls)} stream URL(s) — starting ffmpeg...")

        # Build ffmpeg command: one or two input URLs (video + audio) → MPEG-TS
        ff_cmd = ['ffmpeg']
        for su in stream_urls:
            ff_cmd += ['-i', su]
        if len(stream_urls) == 2:
            ff_cmd += ['-map', '0:v:0', '-map', '1:a:0']
        ff_cmd += ['-c:v', 'copy', '-c:a', 'aac', '-ac', '2', '-b:a', '128k',
                   '-f', 'mpegts', 'pipe:1']

        self.proc = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=yt_env)

        def reader():
            while self.proc and self.proc.poll() is None:
                chunk = self.proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                with self.lock:
                    self.buf.extend(chunk)
            self.done = True
            try:
                os.remove(fifo)
            except OSError:
                pass
        threading.Thread(target=reader, daemon=True).start()

        # Prefill: wait for 10 MB before starting to stream
        for _ in range(120):
            time.sleep(0.5)
            with self.lock:
                sz = len(self.buf)
            if sz > 10 * 1024 * 1024:
                break
            if self.proc.poll() is not None:
                break
        if callback:
            callback(f"[YT] Buffered {sz // 1024 // 1024} MB — streaming to TV")

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
                        with caster.lock:
                            avail = len(caster.buf) - pos
                        if avail > 0:
                            with caster.lock:
                                chunk = bytes(caster.buf[pos:pos + 64 * 1024])
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
                _file_log(f"[YT-HTTP] {self.address_string()} " + (a[0] % a[1:] if a else ''))

        self.srv = socketserver.ThreadingTCPServer(('0.0.0.0', self.port), Handler)
        self.srv.allow_reuse_address = True
        self.srv.daemon_threads = True
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if hasattr(self, '_yt_proc') and self._yt_proc and self._yt_proc.poll() is None:
            try:
                self._yt_proc.kill()
            except Exception:
                pass
        self._yt_proc = None
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

_YT_DOMAINS = ('youtube.com', 'youtu.be', 'youtu.be/', 'yt.be')

def is_youtube_url(text):
    return any(d in text for d in _YT_DOMAINS) or (
        text.startswith(('http://', 'https://')) and
        any(text.split('://', 1)[-1].startswith(d) for d in _YT_DOMAINS)
    )


class YoutubeStreamer:
    """Downloads a YouTube URL via yt-dlp into a temp file while exposing progress."""

    TEMP_PATH = '/tmp/cast_yt_stream.mp4'

    def __init__(self, url, callback=None):
        self.url = url
        self.callback = callback or (lambda m: None)
        self.total_size = 0
        self.duration = None
        self.proc = None
        self._done = False
        self._error = None

    _FORMAT = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    _YTDLP_EXTRA = [
        '--extractor-args', 'youtube:player_client=android_vr,web',
        '--no-playlist', '--newline',
    ]
    _ENV = {**__import__('os').environ,
            'PATH': __import__('os').path.expanduser('~/.deno/bin') + ':' +
                    __import__('os').environ.get('PATH', '')}

    def probe(self):
        """Fetch filesize + duration before download starts. Returns True on success."""
        try:
            r = subprocess.run(
                ['yt-dlp', '-f', self._FORMAT] + self._YTDLP_EXTRA +
                ['--print', '%(filesize,filesize_approx)s', '--print', '%(duration)s', self.url],
                capture_output=True, text=True, timeout=15, env=self._ENV
            )
            lines = r.stdout.strip().splitlines()
            if lines:
                try:
                    self.total_size = int(lines[0])
                except (ValueError, IndexError):
                    self.total_size = 0
            if len(lines) > 1:
                try:
                    secs = float(lines[1])
                    m, s = divmod(int(secs), 60)
                    h, m = divmod(m, 60)
                    self.duration = f"{h:02d}:{m:02d}:{s:02d}" if h else f"00:{m:02d}:{s:02d}"
                except (ValueError, IndexError):
                    pass
            return True
        except Exception as e:
            self._error = str(e)
            return False

    def start(self):
        """Start background download to TEMP_PATH."""
        import glob
        # Clean up any leftover partial files from previous runs
        for f in glob.glob(self.TEMP_PATH.replace('.mp4', '.*')):
            try:
                os.remove(f)
            except OSError:
                pass
        self.proc = subprocess.Popen(
            ['yt-dlp', '-f', self._FORMAT] + self._YTDLP_EXTRA +
            ['-o', self.TEMP_PATH, self.url],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self._ENV
        )
        def _monitor():
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    self.callback(f"[YT] {line}")
            self.proc.wait()
            if self.proc.returncode == 0:
                self._done = True
                self.callback("[YT] Download complete")
            else:
                self._error = f"yt-dlp exit {self.proc.returncode}"
                self.callback(f"[YT] Error: {self._error}")
        threading.Thread(target=_monitor, daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    @property
    def active_path(self):
        """Return the largest existing file matching our temp prefix (final or partial)."""
        import glob
        candidates = glob.glob(self.TEMP_PATH.replace('.mp4', '*.mp4'))
        candidates = [p for p in candidates if os.path.exists(p)]
        if not candidates:
            return None
        return max(candidates, key=lambda p: os.path.getsize(p))

    @property
    def downloaded(self):
        p = self.active_path
        try:
            return os.path.getsize(p) if p else 0
        except OSError:
            return 0

    @property
    def done(self):
        return self._done


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
        self.chars = "D3xLGWebOS01TV"

    def update(self):
        self.canvas.delete("m")
        for i, d in enumerate(self.drops):
            x = i * 14 + 5
            c = random.choice(self.chars)
            self.canvas.create_text(x, d * 15, text=c, fill="#00FF00",
                                    font=("Consolas", 10, "bold"), tags="m")
            for j in range(1, 5):
                if d - j > 0:
                    g = max(0, 180 - j * 40)
                    self.canvas.create_text(x, (d - j) * 15, text=random.choice(self.chars),
                                            fill=f"#00{g:02x}00", font=("Consolas", 10), tags="m")
            self.drops[i] += 1
            if self.drops[i] * 15 > self.h + 60:
                self.drops[i] = random.randint(-8, 0)

# ============= KEYGEN GUI =============

class KeygenApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"D3x LG Caster v{VERSION}")
        self.root.geometry("720x780")
        self.root.resizable(False, False)
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
        self.paused = False

        # Truncate log file at startup so it holds only the current run
        try:
            with open(_LOG_PATH, 'w', encoding='utf-8') as f:
                f.write(f"=== D3x LG Caster v{VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception:
            pass

        self.build_ui()
        self.matrix = MatrixRain(self.matrix_canvas, 720, 780)
        self.animate_matrix()

    def build_ui(self):
        self.matrix_canvas = tk.Canvas(self.root, width=720, height=780, bg='#000000', highlightthickness=0)
        self.matrix_canvas.place(x=0, y=0)

        frame = tk.Frame(self.root, bg='#000000')
        frame.place(relx=0.5, rely=0.5, anchor='center')

        banner = f"""
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551    ____  _____         __    ______   ______          __  \u2551
\u2551   / __ \\|__  /_ __    / /   / ____/  /_  __/__  __   / /  \u2551
\u2551  / / / / /_ <\\ \\ /   / /   / / __     / /  \\ \\ / /  / /   \u2551
\u2551 / /_/ /___/ / /_/   / /___/ /_/ /    / /    \\ V /  /_/    \u2551
\u2551/_____//____/       /_____/\\____/    /_/      \\_/  (_)     \u2551
\u2551                                                           \u2551
\u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563
\u2551  [ DLNA Caster ]           v{VERSION}  *  D3x  *  2026  \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d"""

        tk.Label(frame, text=banner, font=("Consolas", 7), fg='#00FF00', bg='#000000', justify='left').pack()

        # Music toggle
        top_row = tk.Frame(frame, bg='#000000')
        top_row.pack(fill='x', padx=10)
        self.music_btn = tk.Button(top_row, text="\u266b MUSIC OFF", font=("Consolas", 8, "bold"),
            fg='#000000', bg='#555555', command=self.toggle_music, width=12, bd=2)
        self.music_btn.pack(side='right')

        # Log
        log_frame = tk.Frame(frame, bg='#001100', relief='sunken', bd=2)
        log_frame.pack(fill='x', padx=10, pady=5)
        self.log_text = tk.Text(log_frame, height=14, width=78, font=("Consolas", 9),
            fg='#00FF00', bg='#001100', state='disabled')
        self.log_text.pack(padx=3, pady=3)

        # Manual IP entry + device info
        ip_frame = tk.Frame(frame, bg='#000000')
        ip_frame.pack(fill='x', padx=10, pady=3)
        tk.Label(ip_frame, text="[IP]", font=("Consolas", 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left')
        self.ip_entry = tk.Entry(ip_frame, width=15, font=("Consolas", 9), fg='#00FF00', bg='#001100')
        self.ip_entry.pack(side='left', padx=3)
        tk.Button(ip_frame, text="CONNECT", font=("Consolas", 8, "bold"), fg='#000', bg='#FF9900',
            command=self.do_manual_connect, bd=2).pack(side='left', padx=3)
        self.status_lbl = tk.Label(ip_frame, text="READY", font=("Consolas", 9, "bold"),
            fg='#FFFF00', bg='#000000', width=14)
        self.status_lbl.pack(side='right')

        # Device dropdown (populated by DISCOVER / CONNECT)
        dev_frame = tk.Frame(frame, bg='#000000')
        dev_frame.pack(fill='x', padx=10, pady=1)
        tk.Label(dev_frame, text="[TV]", font=("Consolas", 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left')
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
        self.root.option_add('*TCombobox*Listbox.font', ('Consolas', 9))
        self.dev_combo = ttk.Combobox(dev_frame, state='readonly', font=("Consolas", 9),
                                      style='Keygen.TCombobox', height=12)
        self.dev_combo.pack(side='left', padx=5, fill='x', expand=True)
        self.dev_combo.bind('<<ComboboxSelected>>', self._on_device_selected)
        self.dev_combo['values'] = ['<no devices — click DISCOVER>']
        self.dev_combo.current(0)

        # File
        file_frame = tk.Frame(frame, bg='#000000')
        file_frame.pack(fill='x', padx=10, pady=5)
        tk.Label(file_frame, text="[FILE]", font=("Consolas", 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left')
        self.file_entry = tk.Entry(file_frame, width=52, font=("Consolas", 9), fg='#00FF00', bg='#001100')
        self.file_entry.pack(side='left', padx=5)
        tk.Button(file_frame, text="[...]", font=("Consolas", 9, "bold"), fg='#000', bg='#00FF00',
            command=self.browse, bd=2).pack(side='left')

        # Subtitles
        sub_frame = tk.Frame(frame, bg='#000000')
        sub_frame.pack(fill='x', padx=10, pady=2)
        tk.Label(sub_frame, text="[SUBS]", font=("Consolas", 9, "bold"), fg='#00CCFF', bg='#000000').pack(side='left')
        self.sub_entry = tk.Entry(sub_frame, width=52, font=("Consolas", 9), fg='#00CCFF', bg='#001100')
        self.sub_entry.pack(side='left', padx=5)
        tk.Button(sub_frame, text="[...]", font=("Consolas", 9, "bold"), fg='#000', bg='#00CCFF',
            command=self.browse_subs, bd=2).pack(side='left')

        # Streaming mode toggle: force MPEG-TS via ffmpeg (for dongles or AC3/DTS audio)
        mode_frame = tk.Frame(frame, bg='#000000')
        mode_frame.pack(fill='x', padx=10, pady=1)
        self.dongle_mode_var = tk.IntVar(value=0)
        ffmpeg_tip = "" if HAS_FFMPEG else " — needs ffmpeg, NOT INSTALLED"
        tk.Checkbutton(mode_frame,
                       text=f"[MODE] Force MPEG-TS (dongle / AC3 / DTS{ffmpeg_tip})",
                       variable=self.dongle_mode_var,
                       font=("Consolas", 8, "bold"),
                       fg='#FFAA00', bg='#000000',
                       activeforeground='#FFCC00', activebackground='#000000',
                       selectcolor='#001100',
                       state='normal' if HAS_FFMPEG else 'disabled').pack(side='left')

        # Buttons row 1
        btn1 = tk.Frame(frame, bg='#000000')
        btn1.pack(pady=8)
        tk.Button(btn1, text="< DISCOVER >", font=("Consolas", 10, "bold"), fg='#000', bg='#00FFFF',
            command=self.do_discover, width=14, bd=3).pack(side='left', padx=5)
        tk.Button(btn1, text="DONGLE WiFi", font=("Consolas", 9, "bold"), fg='#000', bg='#FF9900',
            command=self.do_dongle_setup, width=11, bd=3).pack(side='left', padx=5)

        # Buttons row 2
        btn2 = tk.Frame(frame, bg='#000000')
        btn2.pack(pady=5)
        self.cancel_btn = tk.Button(btn2, text="< CANCEL >", font=("Consolas", 10, "bold"), fg='#000', bg='#FF0000',
            command=self.cancel, width=12, bd=3, state='disabled')
        self.cancel_btn.pack(side='left', padx=5)
        tk.Button(btn2, text="<<< CAST >>>", font=("Consolas", 10, "bold"), fg='#000', bg='#FF6600',
            command=self.do_cast, width=14, bd=3).pack(side='left', padx=5)
        self.pause_btn = tk.Button(btn2, text="< PAUSE >", font=("Consolas", 10, "bold"), fg='#000', bg='#FFAA00',
            command=self.do_pause, width=11, bd=3)
        self.pause_btn.pack(side='left', padx=5)
        tk.Button(btn2, text="< STOP >", font=("Consolas", 10, "bold"), fg='#000', bg='#AA0000',
            command=self.do_stop, width=10, bd=3).pack(side='left', padx=5)

        # Seek buttons
        seek_frame = tk.Frame(frame, bg='#000000')
        seek_frame.pack(pady=3)
        for label, sec in [("<<30s", -30), ("<<10s", -10), (">>10s", 10), (">>30s", 30), (">>5m", 300)]:
            tk.Button(seek_frame, text=label, font=("Consolas", 8, "bold"), fg='#00FF00', bg='#003300',
                command=lambda s=sec: self.do_seek(s), width=6, bd=2).pack(side='left', padx=2)

        # Now Playing label
        self.now_playing = tk.Label(frame, text="[NOW] Nothing", font=("Consolas", 8),
            fg='#888888', bg='#000000', anchor='w')
        self.now_playing.pack(fill='x', padx=10, pady=2)

        # Footer
        tk.Label(frame, text=("\u2550" * 67 + "\n" +
            "  Greets: Scene 2005 | #warez | The good old days\n" +
            f"  HTTP Server built-in  *  Port {self.server_port}  *  All-in-one"),
            font=("Consolas", 8), fg='#006600', bg='#000000').pack(pady=3)

        self.log(f"[SYS] D3x DLNA Caster v{VERSION} initialized")
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
        return f"{device['friendly_name']} — {device['ip']}:{device['port']}"

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
            if is_youtube_url(video):
                # ---- YouTube: yt-dlp → FIFO → ffmpeg → MPEG-TS → HTTP → DLNA ----
                self.log(f"[YT] Detected YouTube URL — probing...")
                streamer = YoutubeStreamer(video, callback=self.log)
                streamer.probe()
                self.log(f"[YT] Duration: {streamer.duration or 'unknown'}")
                local = get_local_ip()
                if self.dongle_caster is None:
                    self.dongle_caster = DongleCaster(self.server_port + 2)
                self.dongle_caster.start_youtube(video,
                                                  duration_str=streamer.duration,
                                                  callback=self.log)
                url = f"http://{local}:{self.server_port + 2}/stream.ts"
                name = 'YouTube Stream'
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
            title = os.path.splitext(name)[0] if not video.startswith('http') and not is_youtube_url(video) else name
            ok, msg = cast_video(url, control, subtitle_url=sub_url,
                                 video_mime=video_mime, duration=duration,
                                 title=title, callback=self.log)
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
        self.root.destroy()

def main():
    root = tk.Tk()
    app = KeygenApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
