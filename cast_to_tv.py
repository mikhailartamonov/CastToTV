#!/usr/bin/env python3
"""
D3x LG WebOS TV CASTER v1.0 - DLNA Video Streaming Tool
KeyGen 2005 Style Interface with MUSIC!
"""

import tkinter as tk
from tkinter import filedialog, messagebox
import urllib.request
import html
import socket
import re
import time
import threading
import random
import os
from urllib.parse import quote

# ============= 8-BIT KEYGEN MUSIC =============

try:
    import winsound
    HAS_SOUND = True
except:
    HAS_SOUND = False

class ChiptunePlayer:
    def __init__(self):
        self.playing = False
        self.thread = None
        # Classic keygen style melody (Unreal Superhero / CORE vibe)
        self.melody = [
            # Main theme
            (880, 100), (0, 20), (880, 100), (0, 20), (784, 100), (880, 150),
            (1047, 200), (0, 50), (784, 150), (0, 50),
            (659, 100), (0, 20), (659, 100), (0, 20), (587, 100), (659, 150),
            (784, 200), (0, 50), (523, 150), (0, 100),
            # Variation
            (1047, 100), (988, 100), (880, 100), (784, 150), (0, 30),
            (880, 100), (784, 100), (659, 100), (587, 150), (0, 30),
            (659, 100), (587, 100), (523, 100), (494, 150), (0, 30),
            (523, 200), (0, 100),
            # Arpeggios
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
                    except:
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
        is_sub = os.path.splitext(path)[1].lower() in ('.srt','.sub','.smi','.vtt')

        # For subtitle files, serve with UTF-8 BOM prepended (LG WebOS requirement)
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
        import io
        return io.BytesIO(content)

    def _send_dlna_headers(self, is_video):
        if is_video:
            self.send_header('transferMode.dlna.org', 'Streaming')
            self.send_header('contentFeatures.dlna.org', 'DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000')
            if self.subtitle_url:
                self.send_header('CaptionInfo.sec', self.subtitle_url)

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {'.mp4': 'video/mp4', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo',
                '.webm': 'video/webm', '.mov': 'video/quicktime',
                '.srt': 'text/srt', '.sub': 'text/sub', '.smi': 'text/smi',
                '.ass': 'text/ass', '.ssa': 'text/ssa', '.vtt': 'text/vtt'}.get(ext, 'application/octet-stream')

    def log_message(self, format, *args):
        pass  # Suppress logging

class _RangeFile:
    def __init__(self, f, length):
        self.f, self.remaining = f, length
    def read(self, size=-1):
        if self.remaining <= 0: return b''
        if size < 0 or size > self.remaining: size = self.remaining
        data = self.f.read(size)
        self.remaining -= len(data)
        return data
    def close(self):
        self.f.close()

class SilentTCPServer(socketserver.TCPServer):
    """TCPServer that suppresses expected DLNA client disconnects."""
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys
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
        handler = lambda *args, **kwargs: RangeRequestHandler(*args, directory=self.directory, subtitle_url=self.subtitle_url, **kwargs)
        self.server = SilentTCPServer(('0.0.0.0', self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True

    def restart_with_subs(self, subtitle_url):
        self.subtitle_url = subtitle_url
        if self.running:
            self.stop()
        self.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.running = False

# ============= DLNA FUNCTIONS =============

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def get_network_prefix():
    ip = get_local_ip()
    return '.'.join(ip.split('.')[:3]) + '.'

def discover_tv_ssdp(timeout=3):
    SSDP_ADDR = '239.255.255.250'
    SSDP_PORT = 1900
    msg = ('M-SEARCH * HTTP/1.1\r\n'
           f'HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n'
           'MAN: "ssdp:discover"\r\n'
           'MX: 3\r\n'
           'ST: urn:schemas-upnp-org:service:AVTransport:1\r\n\r\n')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    devices = []
    try:
        sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                data, addr = sock.recvfrom(1024)
                if addr[0] not in devices:
                    devices.append(addr[0])
            except socket.timeout:
                break
    finally:
        sock.close()
    return devices

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
            for port in [1780, 1800, 8008, 8060, 9000]:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.08)
                    if sock.connect_ex((ip, port)) == 0:
                        found = True
                    sock.close()
                except:
                    pass
                if found:
                    devices.append(ip)
                    if callback:
                        callback(f"[HIT] Device at {ip}")
                    break
        except:
            pass
    return devices

def find_dlna_service(tv_ip, callback=None, cancel_check=None):
    for port in range(1000, 3000):
        if cancel_check and cancel_check():
            return None, None
        if callback and port % 200 == 0:
            callback(f"[SCAN] Port {port}/3000...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.03)
            if sock.connect_ex((tv_ip, port)) == 0:
                sock.close()
                try:
                    url = f'http://{tv_ip}:{port}/'
                    data = urllib.request.urlopen(url, timeout=1).read(1000).decode('utf-8', errors='ignore')
                    if 'MediaRenderer' in data or 'AVTransport' in data:
                        full = urllib.request.urlopen(url, timeout=3).read().decode()
                        m = re.search(r'<controlURL>(/AVTransport/[^<]+)</controlURL>', full)
                        if m:
                            return port, m.group(1)
                except:
                    pass
            else:
                sock.close()
        except:
            pass
    return None, None

def cast_video(video_url, tv_ip, port, control_url, subtitle_url=None):
    control = f'http://{tv_ip}:{port}{control_url}'

    sub_meta = ''
    if subtitle_url:
        sub_ext = os.path.splitext(subtitle_url.split('?')[0])[1].lstrip('.').lower()
        sub_type = {'srt': 'srt', 'vtt': 'vtt', 'sub': 'sub', 'smi': 'smi'}.get(sub_ext, 'srt')
        sub_meta = (f'<res protocolInfo="http-get:*:text/{sub_type}:*">{subtitle_url}</res>'
                    f'<sec:CaptionInfoEx sec:type="{sub_type}">{subtitle_url}</sec:CaptionInfoEx>')

    didl = f'''<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/" xmlns:sec="http://www.sec.co.kr/"><item id="0" parentID="-1" restricted="1"><dc:title>Video</dc:title><upnp:class>object.item.videoItem.movie</upnp:class><res protocolInfo="http-get:*:video/mp4:DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000">{video_url}</res>{sub_meta}</item></DIDL-Lite>'''

    set_uri = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body><u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID><CurrentURI>{video_url}</CurrentURI>
<CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>
</u:SetAVTransportURI></s:Body></s:Envelope>'''

    try:
        req = urllib.request.Request(control, data=set_uri.encode(), headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'
        })
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        return False, str(e)

    time.sleep(1)

    play = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body><u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID><Speed>1</Speed></u:Play></s:Body></s:Envelope>'''

    try:
        req = urllib.request.Request(control, data=play.encode(), headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#Play"'
        })
        urllib.request.urlopen(req, timeout=30)
        return True, "OK"
    except Exception as e:
        return False, str(e)

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
            self.canvas.create_text(x, d*15, text=c, fill="#00FF00", font=("Consolas", 10, "bold"), tags="m")
            for j in range(1, 5):
                if d - j > 0:
                    self.canvas.create_text(x, (d-j)*15, text=random.choice(self.chars),
                        fill=f"#{0:02x}{max(0,180-j*40):02x}{0:02x}", font=("Consolas", 10), tags="m")
            self.drops[i] += 1
            if self.drops[i] * 15 > self.h + 60:
                self.drops[i] = random.randint(-8, 0)

# ============= KEYGEN GUI =============

class KeygenApp:
    def __init__(self, root):
        self.root = root
        self.root.title("D3x LG Caster")
        self.root.geometry("720x600")
        self.root.resizable(False, False)
        self.root.configure(bg='#000000')

        self.tv_ip = None
        self.dlna_port = None
        self.control_url = None
        self.server_port = 8766
        self.cancel_flag = False
        self.scanning = False

        self.music = ChiptunePlayer()
        self.music_on = False
        self.current_cast = None  # Currently casting file
        self.http_server = None  # Built-in HTTP server

        self.build_ui()
        self.matrix = MatrixRain(self.matrix_canvas, 720, 600)
        self.animate_matrix()
        # Music OFF by default

    def build_ui(self):
        self.matrix_canvas = tk.Canvas(self.root, width=720, height=600, bg='#000000', highlightthickness=0)
        self.matrix_canvas.place(x=0, y=0)

        frame = tk.Frame(self.root, bg='#000000')
        frame.place(relx=0.5, rely=0.5, anchor='center')

        banner = """
╔═══════════════════════════════════════════════════════════╗
║    ____  _____         __    ______   ______          __  ║
║   / __ \\|__  /_ __    / /   / ____/  /_  __/__  __   / /  ║
║  / / / / /_ <\\ \\ /   / /   / / __     / /  \\ \\ / /  / /   ║
║ / /_/ /___/ / /_/   / /___/ /_/ /    / /    \\ V /  /_/    ║
║/_____//____/       /_____/\\____/    /_/      \\_/  (_)     ║
║                                                           ║
╠═══════════════════════════════════════════════════════════╣
║  [ LG WebOS DLNA Caster ]     Coded by D3x  *  2026       ║
╚═══════════════════════════════════════════════════════════╝"""

        tk.Label(frame, text=banner, font=("Consolas", 7), fg='#00FF00', bg='#000000', justify='left').pack()

        # Music toggle
        top_row = tk.Frame(frame, bg='#000000')
        top_row.pack(fill='x', padx=10)
        self.music_btn = tk.Button(top_row, text="♫ MUSIC OFF", font=("Consolas", 8, "bold"),
            fg='#000000', bg='#555555', command=self.toggle_music, width=12, bd=2)
        self.music_btn.pack(side='right')

        # Log
        log_frame = tk.Frame(frame, bg='#001100', relief='sunken', bd=2)
        log_frame.pack(fill='x', padx=10, pady=5)
        self.log_text = tk.Text(log_frame, height=5, width=78, font=("Consolas", 9),
            fg='#00FF00', bg='#001100', state='disabled')
        self.log_text.pack(padx=3, pady=3)

        # IP / Port manual entry
        ip_frame = tk.Frame(frame, bg='#000000')
        ip_frame.pack(fill='x', padx=10, pady=5)

        tk.Label(ip_frame, text="[TV IP]", font=("Consolas", 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left')
        self.ip_entry = tk.Entry(ip_frame, width=15, font=("Consolas", 9), fg='#00FF00', bg='#001100')
        self.ip_entry.pack(side='left', padx=5)
        self.ip_entry.insert(0, "192.168.100.28")

        tk.Label(ip_frame, text="[PORT]", font=("Consolas", 9, "bold"), fg='#00FF00', bg='#000000').pack(side='left', padx=(15,0))
        self.port_entry = tk.Entry(ip_frame, width=6, font=("Consolas", 9), fg='#00FF00', bg='#001100')
        self.port_entry.pack(side='left', padx=5)
        self.port_entry.insert(0, "auto")

        self.status_lbl = tk.Label(ip_frame, text="READY", font=("Consolas", 9, "bold"), fg='#FFFF00', bg='#000000', width=14)
        self.status_lbl.pack(side='right')

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
        tk.Button(btn1, text="< SSDP >", font=("Consolas", 10, "bold"), fg='#000', bg='#00FFFF',
            command=self.do_ssdp, width=12, bd=3).pack(side='left', padx=5)
        tk.Button(btn1, text="< NET SCAN >", font=("Consolas", 10, "bold"), fg='#000', bg='#FFFF00',
            command=self.do_netscan, width=12, bd=3).pack(side='left', padx=5)
        tk.Button(btn1, text="< FIND DLNA >", font=("Consolas", 10, "bold"), fg='#000', bg='#00FF00',
            command=self.do_dlna_scan, width=12, bd=3).pack(side='left', padx=5)

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
        tk.Label(frame, text="═══════════════════════════════════════════════════════════════════\n" +
            "  Greets: Scene 2005 | #warez | The good old days\n" +
            "  HTTP Server built-in  *  Port 8766  *  All-in-one",
            font=("Consolas", 8), fg='#006600', bg='#000000').pack(pady=3)

        self.log("[SYS] D3x LG WebOS TV Caster v1.0 initialized")
        self.log("[SYS] Enter TV IP or use SSDP/NET SCAN to find it")

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
            self.music_btn.config(text="♫ MUSIC OFF", bg='#555555')
        else:
            self.music.start()
            self.music_on = True
            self.music_btn.config(text="♫ MUSIC ON", bg='#FF00FF')

    def browse(self):
        f = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.avi *.webm *.mov"), ("All", "*.*")])
        if f:
            self.file_entry.delete(0, 'end')
            self.file_entry.insert(0, f)

    def browse_subs(self):
        f = filedialog.askopenfilename(filetypes=[("Subtitles", "*.srt *.sub *.smi *.ass *.ssa *.vtt"), ("All", "*.*")])
        if f:
            self.sub_entry.delete(0, 'end')
            self.sub_entry.insert(0, f)

    def set_scanning(self, state):
        self.scanning = state
        self.cancel_flag = False
        self.cancel_btn.config(state='normal' if state else 'disabled')
        self.status_lbl.config(text="SCANNING..." if state else "READY", fg='#FF6600' if state else '#FFFF00')

    def cancel(self):
        self.cancel_flag = True
        self.log("[!] Cancelled by user")

    def do_ssdp(self):
        def run():
            self.set_scanning(True)
            self.log("[SSDP] Broadcasting...")
            devs = discover_tv_ssdp(3)
            if devs:
                self.ip_entry.delete(0, 'end')
                self.ip_entry.insert(0, devs[0])
                self.log(f"[OK] Found: {', '.join(devs)}")
            else:
                self.log("[WARN] No devices via SSDP")
            self.set_scanning(False)
        threading.Thread(target=run, daemon=True).start()

    def do_netscan(self):
        def run():
            self.set_scanning(True)
            prefix = get_network_prefix()
            self.log(f"[NET] Scanning {prefix}0/24...")
            devs = scan_network_for_tv(prefix, self.log, lambda: self.cancel_flag)
            if devs and not self.cancel_flag:
                self.ip_entry.delete(0, 'end')
                self.ip_entry.insert(0, devs[0])
                self.log(f"[OK] Found {len(devs)} device(s)")
            elif not self.cancel_flag:
                self.log("[WARN] No devices found")
            self.set_scanning(False)
        threading.Thread(target=run, daemon=True).start()

    def do_dlna_scan(self):
        def run():
            self.set_scanning(True)
            ip = self.ip_entry.get().strip()
            if not ip:
                self.log("[ERR] Enter TV IP first!")
                self.set_scanning(False)
                return
            self.log(f"[SCAN] Scanning {ip} ports 1000-3000...")
            port, ctrl = find_dlna_service(ip, self.log, lambda: self.cancel_flag)
            if port and not self.cancel_flag:
                self.dlna_port = port
                self.control_url = ctrl
                self.tv_ip = ip
                self.port_entry.delete(0, 'end')
                self.port_entry.insert(0, str(port))
                self.log(f"[OK] DLNA on port {port}")
                self.status_lbl.config(text=f"PORT {port}", fg='#00FF00')
            elif not self.cancel_flag:
                self.log("[ERR] DLNA not found!")
                self.status_lbl.config(text="NOT FOUND", fg='#FF0000')
            self.set_scanning(False)
        threading.Thread(target=run, daemon=True).start()

    def do_stop(self):
        """Stop current playback on TV"""
        ip = self.ip_entry.get().strip()
        port_s = self.port_entry.get().strip()

        if not ip:
            self.log("[ERR] No TV IP")
            return

        if port_s == "auto" or not port_s:
            if not self.dlna_port:
                self.log("[ERR] No DLNA port")
                return
            port = self.dlna_port
            ctrl = self.control_url
        else:
            port = int(port_s)
            ctrl = "/AVTransport/88c02eda-1570-6616-a5df-d1730b811149/control.xml"

        def run():
            control = f'http://{ip}:{port}{ctrl}'
            stop_xml = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body><u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID></u:Stop></s:Body></s:Envelope>'''
            try:
                req = urllib.request.Request(control, data=stop_xml.encode(), headers={
                    'Content-Type': 'text/xml; charset="utf-8"',
                    'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#Stop"'
                })
                urllib.request.urlopen(req, timeout=10)
                self.log("[OK] Playback stopped")
                self.current_cast = None
                self.now_playing.config(text="[NOW] Nothing", fg='#888888')
                self.status_lbl.config(text="STOPPED", fg='#FFFF00')
            except Exception as e:
                self.log(f"[ERR] Stop failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def do_cast(self):
        ip = self.ip_entry.get().strip()
        port_s = self.port_entry.get().strip()
        video = self.file_entry.get().strip()
        subs = self.sub_entry.get().strip()

        if not ip:
            messagebox.showerror("Error", "Enter TV IP!")
            return
        if not video:
            messagebox.showerror("Error", "Select video file!")
            return

        if port_s == "auto" or not port_s:
            if not self.dlna_port:
                messagebox.showerror("Error", "Click FIND DLNA first!")
                return
            port = self.dlna_port
            ctrl = self.control_url
        else:
            port = int(port_s)
            ctrl = "/AVTransport/88c02eda-1570-6616-a5df-d1730b811149/control.xml"

        def run():
            sub_url = None
            if video.startswith("http"):
                url = video
                name = video.split('/')[-1][:40]
            else:
                if not os.path.exists(video):
                    self.log(f"[ERR] File not found!")
                    return
                video_dir = os.path.dirname(os.path.abspath(video))
                local = get_local_ip()

                # Resolve subtitle URL first (needed before starting video server)
                if subs and os.path.exists(subs):
                    subs_dir = os.path.dirname(os.path.abspath(subs))
                    sub_name = os.path.basename(subs)
                    if subs_dir == video_dir:
                        sub_url = f"http://{local}:{self.server_port}/{quote(sub_name)}"
                    else:
                        # Subs in different dir — start separate server
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

                # Start/restart video HTTP server with subtitle URL for CaptionInfo.sec header
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

            self.log(f"[CAST] {ip}:{port}")
            self.status_lbl.config(text="CASTING...", fg='#FF6600')
            ok, msg = cast_video(url, ip, port, ctrl, subtitle_url=sub_url)
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
