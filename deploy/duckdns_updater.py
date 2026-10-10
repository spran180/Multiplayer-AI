"""Keep a DuckDNS hostname pointed at this host without logging its token."""
from __future__ import annotations

import os
import time
from urllib.parse import urlencode
from urllib.request import urlopen


subdomain = os.environ.get("DUCKDNS_SUBDOMAIN", "").strip()
token = os.environ.get("DUCKDNS_TOKEN", "").strip()
if not subdomain or not token:
    raise SystemExit("Set DUCKDNS_SUBDOMAIN and DUCKDNS_TOKEN before enabling the public profile.")

while True:
    url = "https://www.duckdns.org/update?" + urlencode({
        "domains": subdomain,
        "token": token,
        "ip": "",
    })
    try:
        with urlopen(url, timeout=15) as response:
            result = response.read().decode("utf-8", errors="replace").strip()
        if result != "OK":
            raise RuntimeError("DuckDNS returned a failed update status")
        print("DuckDNS address refreshed", flush=True)
    except Exception as exc:
        print(f"DuckDNS update failed: {type(exc).__name__}", flush=True)
    time.sleep(300)
