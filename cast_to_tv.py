#!/usr/bin/env python3
"""
D3x LG WebOS TV CASTER v0.4.4-beta - DLNA Video Streaming Tool
KeyGen 2005 Style Interface with MUSIC!
"""

VERSION = "0.4.4-beta"

import tkinter as tk
from tkinter import filedialog, messagebox
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

import http.server
import socketserver

class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler with Range support for DLNA seeking"""

    def __init__(self, *args, directory=None, subtitle_url=None, **kwargs):
        self.serve_directory = directory
        self.subtitle_url = subtitle_url
        super().__init__(*args, **kwargs)

    def copyfile(self, source, outputfile):
        """Override with larger buffer (256KB instead of 16KB) for video streaming."""
        import shutil
        shutil.copyfileobj(source, outputfile, length=256 * 1024)

    def translate_path(self, path):
        import posixpath
        path = posixpath.normpath(urllib.parse.unquote(path))
        words = path.split('/')
        words = [w for w in words if w]
        path = self.serve_directory if self.serve_directory else os.getcwd()
        for word in words:
            if os.path.dirname(word) or word in (os.curdir, os.pardir):
                continue
            path = os.path.join(path, word)
        return path

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        ctype = self.guess_type(path)
        is_video = ctype.startswith('video/')
        is_sub = os.path.splitext(path)[1].lower() in ('.srt', '.sub', '.smi', '.vtt')

        if is_sub:
            return self._serve_subtitle(path, ctype)

        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(404, "File not found")
            return None

        fs = os.fstat(f.fileno())
        file_size = fs.st_size

        range_header = self.headers.get('Range')
        if range_header:
            match = re.match(r'bytes=(\d*)-(\d*)', range_header)
            if match:
                start = int(match.group(1)) if match.group(1) else 0
                end = int(match.group(2)) if match.group(2) else file_size - 1
                if start >= file_size:
                    f.close()
                    self.send_error(416, 'Range Not Satisfiable')
                    return None
                end = min(end, file_size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(length))
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                self.send_header('Accept-Ranges', 'bytes')
                self._send_dlna_headers(is_video)
                self.end_headers()
                f.seek(start)
                return _RangeFile(f, length)

        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(file_size))
        self.send_header('Accept-Ranges', 'bytes')
        self._send_dlna_headers(is_video)
        self.end_headers()
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
            self.send_header('contentFeatures.dlna.org',
                             'DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000')
            if self.subtitle_url:
                self.send_header('CaptionInfo.sec', self.subtitle_url)

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {
            '.mp4': 'video/mp4', '.mkv': 'video/x-mkv', '.avi': 'video/avi',
            '.webm': 'video/webm', '.mov': 'video/quicktime', '.flv': 'video/flv',
            '.ts': 'video/MP2T', '.mpeg': 'video/mpeg', '.mpg': 'video/mpeg',
            '.wmv': 'video/x-ms-wmv', '.3gp': 'video/3gpp',
            '.srt': 'text/srt', '.sub': 'text/sub', '.smi': 'text/smi', '.vtt': 'text/vtt',
        }.get(ext, 'application/octet-stream')

    def log_message(self, format, *args):
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

class HTTPServerThread:
    def __init__(self, port, directory):
        self.port = port
        self.directory = directory
        self.subtitle_url = None
        self.server = None
        self.thread = None
        self.running = False

    def start(self):
        if self.running:
            return
        handler = lambda *args, **kwargs: RangeRequestHandler(
            *args, directory=self.directory, subtitle_url=self.subtitle_url, **kwargs)
        self.server = SilentThreadingTCPServer(('0.0.0.0', self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.running = False

# ============= DLNA FUNCTIONS =============

MIME_MAP = {
    '.mp4': 'video/mp4', '.mkv': 'video/x-mkv', '.avi': 'video/avi',
    '.webm': 'video/webm', '.mov': 'video/quicktime', '.flv': 'video/flv',
    '.ts': 'video/MP2T', '.mpeg': 'video/mpeg', '.mpg': 'video/mpeg',
    '.wmv': 'video/x-ms-wmv', '.3gp': 'video/3gpp', '.asf': 'video/x-ms-asf',
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

def discover_dlna_renderers(timeout=5, retries=3, callback=None, cancel_check=None):
    """
    Full SSDP discovery: M-SEARCH → parse LOCATION → fetch device XML → extract controlURL.
    Returns list of dicts: {ip, port, friendly_name, control_url, control_path, location}
    """
    SSDP_ADDR = '239.255.255.250'
    SSDP_PORT = 1900
    msg = ('M-SEARCH * HTTP/1.1\r\n'
           f'HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n'
           'MAN: "ssdp:discover"\r\n'
           f'MX: {timeout}\r\n'
           'ST: urn:schemas-upnp-org:service:AVTransport:1\r\n\r\n')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", 4))
    sock.settimeout(timeout)

    if callback:
        callback("[SSDP] Broadcasting M-SEARCH...")

    locations = {}
    try:
        for _ in range(retries):
            sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
            time.sleep(0.1)

        end_time = time.time() + timeout
        while time.time() < end_time:
            if cancel_check and cancel_check():
                break
            try:
                data, addr = sock.recvfrom(65507)
                response = data.decode('utf-8', errors='ignore')
                loc_match = re.search(r'\nlocation:\s*(.+)\r', response, re.IGNORECASE)
                if loc_match:
                    location = loc_match.group(1).strip()
                    if location not in locations:
                        locations[location] = addr[0]
                        if callback:
                            callback(f"[SSDP] Found device at {addr[0]}")
            except socket.timeout:
                break
    finally:
        sock.close()

    if callback:
        callback(f"[SSDP] Got {len(locations)} response(s), parsing...")

    devices = []
    for location_url, ip in locations.items():
        if cancel_check and cancel_check():
            break
        try:
            device = _parse_device_description(location_url)
            if device:
                devices.append(device)
                if callback:
                    callback(f"[OK] {device['friendly_name']} ({device['ip']}:{device['port']})")
        except Exception:
            pass

    return devices


def _parse_device_description(location_url):
    """Fetch device description XML from LOCATION URL, extract friendlyName + AVTransport controlURL."""
    xml_raw = urllib.request.urlopen(location_url, timeout=5).read().decode('utf-8', errors='ignore')
    xml_clean = re.sub(r'\sxmlns="[^"]*"', '', xml_raw, count=1)
    root = ET.fromstring(xml_clean)

    device_el = root.find('./device')
    if device_el is None:
        return None

    friendly_name = device_el.findtext('./friendlyName', 'Unknown')

    control_path = None
    for service in device_el.findall('.//service'):
        stype = service.findtext('serviceType', '')
        if 'AVTransport' in stype:
            control_path = service.findtext('controlURL')
            break

    if not control_path:
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

# ---------- Network scan (fallback) ----------

def scan_network_for_tv(prefix, callback=None, cancel_check=None):
    devices = []
    for i in range(1, 255):
        if cancel_check and cancel_check():
            break
        ip = prefix + str(i)
        if callback:
            callback(f"[NET] Ping {ip}")
        try:
            found = False
            for port in [1780, 1782, 1790, 2700, 8008, 8060, 9000]:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.08)
                    if sock.connect_ex((ip, port)) == 0:
                        found = True
                    sock.close()
                except Exception:
                    pass
                if found:
                    devices.append(ip)
                    if callback:
                        callback(f"[HIT] Device at {ip}")
                    break
        except Exception:
            pass
    return devices

# ---------- DLNA service scan for manual IP (fallback) ----------

def find_dlna_service(tv_ip, callback=None, cancel_check=None):
    """Try common DLNA ports first (LG TV + WiFi dongles), then broader scan."""
    # LG TV ports + EZCast/AnyCast/MiraScreen dongle ports
    priority_ports = [49152, 49153, 49154, 49595, 2020, 7000, 8008, 8060,
                      1780, 1782, 1790, 2700, 1800, 8080, 9000, 1900]
    all_ports = priority_ports + [p for p in range(1000, 50000) if p not in priority_ports]

    desc_paths = ['/', '/dmr/DeviceDescription.xml', '/xml/device_description.xml',
                   '/upnp/dev/MediaRenderer/desc.xml', '/DeviceDescription.xml',
                   '/rootDesc.xml', '/description.xml']

    for port in all_ports:
        if cancel_check and cancel_check():
            return None
        if callback and port % 500 == 0 and port > priority_ports[-1]:
            callback(f"[SCAN] Port {port}...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.05)
            if sock.connect_ex((tv_ip, port)) == 0:
                sock.close()
                for path in desc_paths:
                    try:
                        url = f'http://{tv_ip}:{port}{path}'
                        xml_raw = urllib.request.urlopen(url, timeout=2).read().decode('utf-8', errors='ignore')
                        if 'MediaRenderer' in xml_raw or 'AVTransport' in xml_raw:
                            device = _parse_device_description(url)
                            if device:
                                if callback:
                                    callback(f"[OK] Found on port {port}")
                                return device
                    except Exception:
                        continue
            else:
                sock.close()
        except Exception:
            pass
    return None

# ---------- Cast / Stop ----------

def cast_video(video_url, control_url, subtitle_url=None, video_mime='video/mp4'):
    sub_meta = ''
    if subtitle_url:
        sub_ext = os.path.splitext(subtitle_url.split('?')[0])[1].lstrip('.').lower()
        sub_type = {'srt': 'srt', 'vtt': 'vtt', 'sub': 'sub', 'smi': 'smi'}.get(sub_ext, 'srt')
        sub_meta = (f'<res protocolInfo="http-get:*:text/{sub_type}:*">{subtitle_url}</res>'
                    f'<sec:CaptionInfoEx sec:type="{sub_type}">{subtitle_url}</sec:CaptionInfoEx>')

    didl = (f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            f' xmlns:dc="http://purl.org/dc/elements/1.1/"'
            f' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
            f' xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"'
            f' xmlns:sec="http://www.sec.co.kr/">'
            f'<item id="0" parentID="-1" restricted="1">'
            f'<dc:title>Video</dc:title>'
            f'<upnp:class>object.item.videoItem.movie</upnp:class>'
            f'<res protocolInfo="http-get:*:{video_mime}:'
            f'DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000">'
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

    try:
        req = urllib.request.Request(control_url, data=set_uri.encode(), headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'
        })
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        return False, str(e)

    time.sleep(1)

    play = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID><Speed>1</Speed>'
            '</u:Play></s:Body></s:Envelope>')

    try:
        req = urllib.request.Request(control_url, data=play.encode(), headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#Play"'
        })
        urllib.request.urlopen(req, timeout=30)
        return True, "OK"
    except Exception as e:
        return False, str(e)


def stop_playback(control_url):
    stop_xml = ('<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
                ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body><u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                '<InstanceID>0</InstanceID>'
                '</u:Stop></s:Body></s:Envelope>')
    req = urllib.request.Request(control_url, data=stop_xml.encode(), headers={
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#Stop"'
    })
    urllib.request.urlopen(req, timeout=10)

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
        self.root.geometry("720x620")
        self.root.resizable(False, False)
        self.root.configure(bg='#000000')

        self.discovered_device = None  # {ip, port, friendly_name, control_url, ...}
        self.server_port = 8766
        self.cancel_flag = False
        self.scanning = False

        self.music = ChiptunePlayer()
        self.music_on = False
        self.current_cast = None
        self.http_server = None

        self.build_ui()
        self.matrix = MatrixRain(self.matrix_canvas, 720, 620)
        self.animate_matrix()

    def build_ui(self):
        self.matrix_canvas = tk.Canvas(self.root, width=720, height=620, bg='#000000', highlightthickness=0)
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
        self.log_text = tk.Text(log_frame, height=6, width=78, font=("Consolas", 9),
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

        # Device label
        dev_frame = tk.Frame(frame, bg='#000000')
        dev_frame.pack(fill='x', padx=10, pady=1)
        tk.Label(dev_frame, text="[TV]", font=("Consolas", 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left')
        self.dev_label = tk.Label(dev_frame, text="Not discovered — use DISCOVER or enter IP", font=("Consolas", 9),
            fg='#888888', bg='#000000', anchor='w')
        self.dev_label.pack(side='left', padx=5, fill='x', expand=True)

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

        # Buttons row 1
        btn1 = tk.Frame(frame, bg='#000000')
        btn1.pack(pady=8)
        tk.Button(btn1, text="< DISCOVER >", font=("Consolas", 10, "bold"), fg='#000', bg='#00FFFF',
            command=self.do_discover, width=14, bd=3).pack(side='left', padx=5)
        tk.Button(btn1, text="< NET SCAN >", font=("Consolas", 10, "bold"), fg='#000', bg='#FFFF00',
            command=self.do_netscan, width=12, bd=3).pack(side='left', padx=5)
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
        tk.Button(btn2, text="< STOP >", font=("Consolas", 10, "bold"), fg='#000', bg='#AA0000',
            command=self.do_stop, width=10, bd=3).pack(side='left', padx=5)

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
        def _write():
            self.log_text.config(state='normal')
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
        self.root.after(0, _write)

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

    def _set_device(self, device):
        """Set discovered device and update UI."""
        self.discovered_device = device
        name = device['friendly_name']
        ip = device['ip']
        port = device['port']
        self.dev_label.config(text=f"{name} ({ip}:{port})", fg='#00FF00')
        self.status_lbl.config(text=f"PORT {port}", fg='#00FF00')

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
        """Connect to a manually entered IP — scans for DLNA service."""
        ip = self.ip_entry.get().strip()
        if not ip:
            self.log("[ERR] Enter IP address first")
            return
        def run():
            self.set_scanning(True)
            self.log(f"[SCAN] Connecting to {ip}...")
            device = find_dlna_service(ip, self.log, lambda: self.cancel_flag)
            if device and not self.cancel_flag:
                self._set_device(device)
            elif not self.cancel_flag:
                self.log(f"[ERR] No DLNA service found on {ip}")
            self.set_scanning(False)
        threading.Thread(target=run, daemon=True).start()

    def do_discover(self):
        """SSDP discovery — finds everything in one step."""
        def run():
            self.set_scanning(True)
            devices = discover_dlna_renderers(timeout=5, retries=3,
                                              callback=self.log,
                                              cancel_check=lambda: self.cancel_flag)
            if devices and not self.cancel_flag:
                self._set_device(devices[0])
                self.ip_entry.delete(0, 'end')
                self.ip_entry.insert(0, devices[0]['ip'])
                self.log(f"[OK] Found {len(devices)} renderer(s)")
                if len(devices) > 1:
                    for d in devices[1:]:
                        self.log(f"     + {d['friendly_name']} ({d['ip']}:{d['port']})")
            elif not self.cancel_flag:
                self.log("[WARN] No DLNA renderers found via SSDP")
                self.log("[TIP] Enter IP manually and click CONNECT")
            self.set_scanning(False)
        threading.Thread(target=run, daemon=True).start()

    def do_netscan(self):
        """Network scan fallback — finds IPs, then fetches device descriptions."""
        def run():
            self.set_scanning(True)
            prefix = get_network_prefix()
            self.log(f"[NET] Scanning {prefix}0/24...")
            devs = scan_network_for_tv(prefix, self.log, lambda: self.cancel_flag)
            if devs and not self.cancel_flag:
                self.log(f"[NET] Found {len(devs)} device(s), checking DLNA...")
                for ip in devs:
                    if self.cancel_flag:
                        break
                    self.log(f"[SCAN] Checking {ip}...")
                    device = find_dlna_service(ip, self.log, lambda: self.cancel_flag)
                    if device:
                        self._set_device(device)
                        self.log(f"[OK] {device['friendly_name']} ({ip}:{device['port']})")
                        break
                else:
                    if not self.cancel_flag:
                        self.log("[ERR] No DLNA service found on discovered devices")
            elif not self.cancel_flag:
                self.log("[WARN] No devices found on network")
            self.set_scanning(False)
        threading.Thread(target=run, daemon=True).start()

    def do_stop(self):
        if not self.discovered_device:
            self.log("[ERR] No TV discovered — click DISCOVER first")
            return
        def run():
            try:
                stop_playback(self.discovered_device['control_url'])
                self.log("[OK] Playback stopped")
                self.current_cast = None
                self.now_playing.config(text="[NOW] Nothing", fg='#888888')
                self.status_lbl.config(text="STOPPED", fg='#FFFF00')
            except Exception as e:
                self.log(f"[ERR] Stop failed: {e}")
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
            if video.startswith("http"):
                url = video
                name = video.split('/')[-1][:40]
                video_mime = 'video/mp4'
            else:
                if not os.path.exists(video):
                    self.log("[ERR] File not found!")
                    return
                video_dir = os.path.dirname(os.path.abspath(video))
                local = get_local_ip()
                ext = os.path.splitext(video)[1].lower()
                video_mime = MIME_MAP.get(ext, 'video/mp4')

                # Resolve subtitle URL
                if subs and os.path.exists(subs):
                    subs_dir = os.path.dirname(os.path.abspath(subs))
                    sub_name = os.path.basename(subs)
                    if subs_dir == video_dir:
                        sub_url = f"http://{local}:{self.server_port}/{quote(sub_name)}"
                    else:
                        if not hasattr(self, 'sub_server') or self.sub_server is None or self.sub_server.directory != subs_dir:
                            if hasattr(self, 'sub_server') and self.sub_server:
                                self.sub_server.stop()
                            self.sub_server = HTTPServerThread(self.server_port + 1, subs_dir)
                            try:
                                self.sub_server.start()
                                self.log(f"[HTTP] Subs server on port {self.server_port + 1}")
                            except Exception as e:
                                self.log(f"[WARN] Subs HTTP: {e}")
                        sub_url = f"http://{local}:{self.server_port + 1}/{quote(sub_name)}"
                    self.log(f"[SUBS] {sub_name}")
                elif subs:
                    self.log(f"[WARN] Subtitle file not found: {subs}")

                # Start/restart video HTTP server
                need_restart = (self.http_server is None or
                                self.http_server.directory != video_dir or
                                self.http_server.subtitle_url != sub_url)
                if need_restart:
                    if self.http_server:
                        self.http_server.stop()
                    self.http_server = HTTPServerThread(self.server_port, video_dir)
                    self.http_server.subtitle_url = sub_url
                    try:
                        self.http_server.start()
                        self.log(f"[HTTP] Server on port {self.server_port}")
                    except Exception as e:
                        self.log(f"[WARN] HTTP: {e}")
                else:
                    self.http_server.subtitle_url = sub_url

                name = os.path.basename(video)
                url = f"http://{local}:{self.server_port}/{quote(name)}"

            self.log(f"[CAST] {self.discovered_device['friendly_name']}")
            self.status_lbl.config(text="CASTING...", fg='#FF6600')
            ok, msg = cast_video(url, control, subtitle_url=sub_url, video_mime=video_mime)
            if ok:
                self.log("[OK] Streaming started!")
                if sub_url:
                    self.log("[OK] Subtitles attached")
                self.status_lbl.config(text="PLAYING", fg='#00FF00')
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
        if hasattr(self, 'sub_server') and self.sub_server:
            self.sub_server.stop()
        self.root.destroy()

def main():
    root = tk.Tk()
    app = KeygenApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
