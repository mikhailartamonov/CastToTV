#!/usr/bin/env python3
"""
Cast video to LG webOS TV via DLNA
Usage: python cast_to_lg.py <video_file>
"""

import urllib.request
import html
import sys
import os
import socket
import re

# Configuration
TV_IP = "192.168.100.28"
SERVER_IP = "192.168.100.9"
SERVER_PORT = 8766

def find_dlna_port(tv_ip):
    """Find the DMR port on LG TV (changes after reboot)"""
    import subprocess

    # Quick scan ports 1000-2000
    result = subprocess.run(
        [r"C:\Program Files (x86)\Nmap\nmap.exe", "-p", "1000-2000", tv_ip, "-T4"],
        capture_output=True, text=True, timeout=60
    )

    # Parse open ports
    ports = re.findall(r'(\d+)/tcp\s+open', result.stdout)

    for port in ports:
        try:
            url = f'http://{tv_ip}:{port}/'
            req = urllib.request.urlopen(url, timeout=2)
            data = req.read(500).decode('utf-8', errors='ignore')
            if 'MediaRenderer' in data and 'AVTransport' in data:
                # Get control URL
                full_data = urllib.request.urlopen(url, timeout=5).read().decode()
                match = re.search(r'<controlURL>(/AVTransport/[^<]+)</controlURL>', full_data)
                if match:
                    return int(port), match.group(1)
        except:
            pass

    return None, None

def cast_video(video_url, tv_ip=TV_IP, port=None, control_url=None):
    """Cast video to LG TV via DLNA"""

    if port is None or control_url is None:
        print("Scanning for DLNA port...")
        port, control_url = find_dlna_port(tv_ip)
        if port is None:
            print("ERROR: Could not find DLNA service on TV")
            return False
        print(f"Found DLNA on port {port}")

    control = f'http://{tv_ip}:{port}{control_url}'

    # DIDL-Lite metadata (required for LG TV)
    didl = f'''<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"><item id="0" parentID="-1" restricted="1"><dc:title>Video</dc:title><upnp:class>object.item.videoItem.movie</upnp:class><res protocolInfo="http-get:*:video/mp4:DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000">{video_url}</res></item></DIDL-Lite>'''

    metadata = html.escape(didl)

    # SetAVTransportURI
    set_uri = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID>
<CurrentURI>{video_url}</CurrentURI>
<CurrentURIMetaData>{metadata}</CurrentURIMetaData>
</u:SetAVTransportURI>
</s:Body>
</s:Envelope>'''

    try:
        req = urllib.request.Request(control, data=set_uri.encode(), headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'
        })
        urllib.request.urlopen(req, timeout=30)
        print("SetURI: OK")
    except Exception as e:
        print(f"SetURI ERROR: {e}")
        return False

    import time
    time.sleep(2)

    # Play
    play = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID>
<Speed>1</Speed>
</u:Play>
</s:Body>
</s:Envelope>'''

    try:
        req = urllib.request.Request(control, data=play.encode(), headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPAction': '"urn:schemas-upnp-org:service:AVTransport:1#Play"'
        })
        urllib.request.urlopen(req, timeout=30)
        print("Play: OK")
        return True
    except Exception as e:
        print(f"Play ERROR: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python cast_to_lg.py <video_file>")
        print("Example: python cast_to_lg.py movie.mp4")
        sys.exit(1)

    video_file = sys.argv[1]

    # Build URL
    if video_file.startswith("http"):
        video_url = video_file
    else:
        filename = os.path.basename(video_file)
        video_url = f"http://{SERVER_IP}:{SERVER_PORT}/{filename}"

    print(f"Casting: {video_url}")

    if cast_video(video_url):
        print("Success! Video should be playing on TV.")
    else:
        print("Failed to cast video.")
        sys.exit(1)
