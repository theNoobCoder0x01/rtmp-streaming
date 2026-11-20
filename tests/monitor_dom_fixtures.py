"""SQLite-generated test fixtures for the optional DOM smoke test, never production data."""

import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitoring.service import MonitoringService, connection_key


with tempfile.TemporaryDirectory(prefix="monitor-dom-") as temporary:
    monitor = MonitoringService(Path(temporary) / "test.sqlite3")
    now = time.time()
    monitor.initialize(now=now - 1000)
    name = 'main-hall <img src=x onerror="window.injected=true">'
    monitor.callback("publish", {"app": "live", "name": name, "clientid": "1", "addr": "192.168.1.20"}, now=now - 600)
    monitor.callback("play", {"app": "live", "name": name, "clientid": "2", "addr": "192.168.1.21"}, now=now - 300)
    browser = {"role": "hls_viewer", "application": "live", "stream_name": name, "username": "viewer",
               "ip_address": "192.168.1.22", "connection_key": "fixture-browser"}
    monitor.store.start(browser, source="hls_request", now=now - 200)
    monitor.store.start(browser, source="hls_request", now=now)
    ended = {"app": "live", "name": "camera-2", "clientid": "3", "addr": "192.168.1.23"}
    monitor.callback("publish", ended, now=now - 1200)
    monitor.callback("publish_done", ended, now=now - 1000)
    monitor.streams = [{"id": "1935_main-hall", "name": name, "application": "live", "port": 1935,
                        "status": "active", "quality": "1080p", "publishers": 1, "rtmp_viewers": 1,
                        "bandwidth_in": 4.5, "bandwidth_out": 4.5}]
    monitor.last_success = now
    for timestamp in (now - 900, now - 600, now - 300, now):
        monitor.store.sample(4.5, 4.5, now=timestamp)
    selected = {"start": now - 86400, "end": now + 1}
    result = {"dashboard": monitor.dashboard()}
    for key, method in (("sessions", monitor.store.sessions), ("events", monitor.store.events), ("reports", monitor.store.report)):
        result[key] = method(selected)
        result["empty_" + key] = method({**selected, "ip": "192.168.1.250"})
        result["publisher_" + key] = method({**selected, "role": "publisher"})
    print(json.dumps(result))
