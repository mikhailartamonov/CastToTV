# Legacy — January prototypes

These three scripts predate the `Initial commit` of this repo (2026-04-14).
They're the actual origin of CastToTV: hand-written experiments from the
2025-12-31 / 2026-01-04 weekend that figured out how to push video to an
LG webOS TV over DLNA. They are kept here for historical reference and
because some of the design choices in the modern `cast_to_tv.py` only
make sense in light of these earlier attempts.

| File | Date | Role |
| --- | --- | --- |
| `dlna_cast.py` | 2025-12-31 | First working prototype. Spawns `SimpleHTTPServer`, sends `SetAVTransportURI` + `Play` over SOAP. TV port hardcoded to `9197`. No discovery. |
| `cast_to_lg.py` | 2026-01-04 | Adds DLNA port discovery — LG TVs randomise the AVTransport port after every reboot, so the script shells out to `nmap` (1000-2000 range) and fingerprints each open port until it finds one whose XML descriptor advertises `MediaRenderer` + `AVTransport`. |
| `dlnap.py` | 2026-01-04 | Vendored copy of [cherezov/dlnap](https://github.com/cherezov/dlnap) v0.15 by Pavel Cherezov — the SSDP / UPnP base library that the modern discovery code in `cast_to_tv.py` descends from. Kept verbatim, MIT-licensed upstream. |

## What survived into the modern code

- **DIDL-Lite metadata structure** in `cast_to_lg.py` was kept almost
  verbatim — same `protocolInfo`, same `<upnp:class>object.item.videoItem.movie`.
- **SetAVTransportURI → Play with a 2-second sleep** sequencing came from
  here; LG TVs reject `Play` if it arrives before the URI has been parsed.
- **HTTP server in a daemon thread** alongside the main app, so the TV can
  pull the file while the GUI stays responsive — first appears in `dlna_cast.py`.

## What was dropped

- **nmap port scan** — replaced by proper SSDP discovery in v0.4.4-beta
  (commit `b0b54a3`). Faster, no external binary.
- **Hardcoded TV IP / port** — replaced by user input + auto-detect.
- **Single-file CLI** — replaced by the keygen-style Tk GUI.
