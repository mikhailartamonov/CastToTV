#!/usr/bin/env python
"""Simple DLNA casting script for LG webOS TV"""
import http.server
import socket
import threading
import urllib.request
import sys
import os
import time

TV_IP = "192.168.100.28"
TV_PORT = 9197  # AVTransport port

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((TV_IP, 80))
        return s.getsockname()[0]
    finally:
        s.close()

def start_http_server(file_path, port=8000):
    directory = os.path.dirname(os.path.abspath(file_path))
    filename = os.path.basename(file_path)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)
        def log_message(self, format, *args):
            print(f"[HTTP] {args[0]}")

    server = http.server.HTTPServer(('0.0.0.0', port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return port, filename

def send_to_tv(video_url, filename):
    # First, get the AVTransport control URL
    location_url = f"http://{TV_IP}:1070/"

    # DIDL-Lite metadata with proper MIME type
    didl_meta = f'''&lt;DIDL-Lite xmlns=&quot;urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/&quot; xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot; xmlns:upnp=&quot;urn:schemas-upnp-org:metadata-1-0/upnp/&quot;&gt;&lt;item id=&quot;0&quot; parentID=&quot;-1&quot; restricted=&quot;1&quot;&gt;&lt;dc:title&gt;{filename}&lt;/dc:title&gt;&lt;upnp:class&gt;object.item.videoItem&lt;/upnp:class&gt;&lt;res protocolInfo=&quot;http-get:*:video/mp4:*&quot;&gt;{video_url}&lt;/res&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;'''

    soap_body = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
      <CurrentURI>{video_url}</CurrentURI>
      <CurrentURIMetaData>{didl_meta}</CurrentURIMetaData>
    </u:SetAVTransportURI>
  </s:Body>
</s:Envelope>'''

    # Get the device description to find AVTransport URL
    try:
        with urllib.request.urlopen(location_url, timeout=5) as resp:
            desc = resp.read().decode()
            # Parse to find AVTransport control URL
            import re
            # Find service with AVTransport
            match = re.search(r'<controlURL>([^<]*AVTransport[^<]*)</controlURL>', desc, re.IGNORECASE)
            if not match:
                # Try to find any controlURL under AVTransport service block
                match = re.search(r'AVTransport.*?<controlURL>([^<]+)</controlURL>', desc, re.DOTALL | re.IGNORECASE)
            if not match:
                # Fallback - common LG paths
                control_path = "/upnp/control/AVTransport1"
            else:
                control_path = match.group(1)
    except Exception as e:
        print(f"Error getting device description: {e}")
        control_path = "/upnp/control/AVTransport1"

    control_url = f"http://{TV_IP}:9197{control_path}" if not control_path.startswith('http') else control_path
    if not control_path.startswith('http') and ':' not in control_path:
        # Try port 1070 first
        control_url = f"http://{TV_IP}:1070{control_path}"

    print(f"Sending to: {control_url}")

    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
    }

    req = urllib.request.Request(control_url, data=soap_body.encode('utf-8'), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"SetAVTransportURI: {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"SetAVTransportURI error: {e.code} - {e.read().decode()}")
        return False
    except Exception as e:
        print(f"SetAVTransportURI error: {e}")
        return False

    # Now send Play command
    time.sleep(0.5)
    play_body = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
      <Speed>1</Speed>
    </u:Play>
  </s:Body>
</s:Envelope>'''

    headers['SOAPAction'] = '"urn:schemas-upnp-org:service:AVTransport:1#Play"'
    req = urllib.request.Request(control_url, data=play_body.encode('utf-8'), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Play: {resp.status}")
            return True
    except Exception as e:
        print(f"Play error: {e}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python dlna_cast.py <video_file>")
        sys.exit(1)

    file_path = sys.argv[1]
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        sys.exit(1)

    local_ip = get_local_ip()
    print(f"Local IP: {local_ip}")
    print(f"TV IP: {TV_IP}")
    print(f"File: {file_path}")

    port, filename = start_http_server(file_path, 8765)
    video_url = f"http://{local_ip}:{port}/{urllib.parse.quote(filename)}"
    print(f"Video URL: {video_url}")

    print("\nSending to TV...")
    if send_to_tv(video_url, filename):
        print("\n[OK] Video sent to TV!")
        print("Press Ctrl+C to stop the server...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        print("\n[FAIL] Failed to send video")

import urllib.parse
if __name__ == "__main__":
    main()
