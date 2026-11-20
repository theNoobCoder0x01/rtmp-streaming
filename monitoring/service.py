"""Normalize Nginx observations and distinguish exact from inferred events."""

import hashlib
import ipaddress
import json
import logging
import math
from pathlib import Path
import queue
import sqlite3
import threading
import time

from .store import MonitoringStore


logger = logging.getLogger(__name__)


def connection_key(role, application, stream, client_id):
    return hashlib.sha256(json.dumps([role, application, stream, str(client_id)]).encode()).hexdigest()


def browser_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def number(node, path, default=0):
    text = node.findtext(path)
    value = float(text) if text else default
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid Nginx statistics counter")
    return value


def parse_stats(root):
    if root.tag != "rtmp" or root.find("server/application") is None:
        raise ValueError("Incomplete Nginx statistics document")
    streams, clients = [], []
    for server in root.findall("server"):
        port = int(number(server, "port", 1935))
        for application in server.findall("application"):
            app = application.findtext("name")
            if not app:
                raise ValueError("Missing application in Nginx statistics")
            for node in application.findall("live/stream"):
                name = node.findtext("name")
                if not name:
                    raise ValueError("Missing stream name in Nginx statistics")
                publishers = 0
                viewers = 0
                for client in node.findall("client"):
                    client_id, address = client.findtext("id"), client.findtext("address")
                    if not client_id or not address:
                        raise ValueError("Incomplete Nginx client statistics")
                    address = str(ipaddress.ip_address(address))
                    role = "publisher" if client.find("publishing") is not None else "rtmp_viewer"
                    publishers += role == "publisher"
                    viewers += role == "rtmp_viewer"
                    clients.append({"role": role, "application": app, "stream_name": name,
                                    "ip_address": address,
                                    "connection_key": connection_key(role, app, name, client_id)})
                height = int(number(node, "meta/video/height"))
                quality = "{}p".format(height) if height else "unknown"
                streams.append({"id": "{}_{}".format(port, name), "name": name, "application": app,
                    "port": port, "status": "active" if publishers else "inactive", "quality": quality,
                    "publishers": publishers, "rtmp_viewers": viewers,
                    "bandwidth_in": number(node, "bw_in") / 1000000,
                    "bandwidth_out": number(node, "bw_out") / 1000000,
                    "bytes_in": number(node, "bytes_in") / 1000000,
                    "bytes_out": number(node, "bytes_out") / 1000000})
    return {"streams": streams, "clients": clients}


class MonitoringService:
    def __init__(self, path, hls_root="/tmp"):
        self.store = MonitoringStore(path)
        self.hls_root = Path(hls_root).resolve()
        self.enabled = False
        self.last_success = None
        self.last_error = None
        self.storage_error = None
        self.streams = []
        self.dropped_events = 0
        self.pending_activity = queue.Queue(maxsize=10000)
        self.hls_touches = {}
        self.revoked_browsers = {}
        self.touch_lock = threading.Lock()
        self.maintenance_lock = threading.Lock()

    def initialize(self, now=None):
        self.store.initialize(now)
        self.enabled = True

    def _write(self, method, *args, **kwargs):
        if not self.enabled:
            return None
        try:
            result = method(*args, **kwargs)
            self.storage_error = None
            return True if result is None else result
        except sqlite3.Error:
            self.storage_error = "SQLite is temporarily unavailable; some observations may be missing."
            logger.exception("Monitoring database operation failed")
            return None

    def callback(self, event, fields, now=None):
        if event not in ("publish", "publish_done", "play", "play_done"):
            raise ValueError("Unknown notification")
        app, name, client_id = fields.get("app", ""), fields.get("name", ""), fields.get("clientid", "")
        if not app or not name or not client_id or len(app) > 128 or len(name) > 512 or len(client_id) > 128:
            raise ValueError("Incomplete notification")
        address = str(ipaddress.ip_address(fields.get("addr", "")))
        role = "publisher" if event.startswith("publish") else "rtmp_viewer"
        key = connection_key(role, app, name, client_id)
        now = time.time() if now is None else now
        if event.endswith("_done"):
            recorded = self._write(self.store.end, role, key, now=now)
        else:
            recorded = self._write(self.store.start, {"role": role, "connection_key": key, "application": app,
                                          "stream_name": name, "ip_address": address}, now=now)
        if self.enabled and recorded is None:
            self.dropped_events += 1

    def observe(self, snapshot, now=None):
        now = time.time() if now is None else now
        self.streams = snapshot["streams"]
        if self.last_error:
            self._write(self.store.reset_absence)
        self.last_success, self.last_error = now, None
        self._write(self.store.reconcile, snapshot["clients"], now=now)
        self._write(self.store.sample, sum(s["bandwidth_in"] for s in self.streams),
                    sum(s["bandwidth_out"] for s in self.streams), now=now)

    def failed_poll(self):
        self.last_error = "Nginx statistics are unavailable. Last-known connections may be stale."

    def end_browser(self, token):
        if self.enabled and token:
            key, at = browser_key(token), time.time()
            with self.touch_lock:
                self.revoked_browsers[key] = at
                self._enqueue("logout", key, at)

    def _enqueue(self, kind, payload, at):
        try:
            self.pending_activity.put_nowait((kind, payload, at))
        except queue.Full:
            self.dropped_events += 1
            logger.warning("Monitoring activity queue is full; observation dropped")

    def maintain(self, now=None):
        # A dashboard request and the polling loop may drain concurrently.
        # Keep media/start and logout/end observations in FIFO order.
        with self.maintenance_lock:
            self._maintain(now)

    def _maintain(self, now=None):
        if not self.enabled:
            return
        while True:
            try:
                kind, payload, at = self.pending_activity.get_nowait()
            except queue.Empty:
                break
            if kind == "logout":
                result = self._write(self.store.end_browser, payload, now=at)
            else:
                result = self._write(self.store.start, payload, source="hls_request", now=at)
            if result is None:
                self.dropped_events += 1
        self._write(self.store.expire_hls, now=now)
        now = time.time() if now is None else now
        with self.touch_lock:
            self.hls_touches = {key: at for key, at in self.hls_touches.items() if at > now - 60}
            self.revoked_browsers = {key: at for key, at in self.revoked_browsers.items() if at > now - 12 * 3600}

    def hls_request(self, token, username, address, file_path, now=None):
        if not self.enabled:
            return
        path = (self.hls_root / file_path).resolve()
        try:
            path.relative_to(self.hls_root)
        except ValueError:
            return
        # Do not count nonexistent playlists, missing segments or dashboard visits.
        if not path.is_file():
            return
        folder, remainder = file_path.split("/", 1)
        if folder not in ("hls", "hls1", "hls2", "hls3", "hls4", "hls5"):
            return
        if remainder.endswith(".m3u8"):
            name = remainder[:-5]
        else:
            # nginx-rtmp's default fragment names are <stream>-<sequence>.ts.
            name, separator, sequence = remainder[:-3].rpartition("-")
            if not separator or not sequence.isdigit():
                return
        app = "live" if folder == "hls" else folder
        browser = browser_key(token)
        key = browser + ":" + connection_key("hls_viewer", app, name, "")
        now = time.time() if now is None else now
        address = str(ipaddress.ip_address(address))
        with self.touch_lock:
            if browser in self.revoked_browsers:
                return
            last = self.hls_touches.get(key)
            if last is not None and now - last < 5:
                return
            self.hls_touches[key] = now
            self._enqueue("media", {"role": "hls_viewer", "connection_key": key,
                          "application": app, "stream_name": name, "ip_address": address,
                          "username": username}, now)

    def health(self, now=None):
        now = time.time() if now is None else now
        age = None if self.last_success is None else max(0, now - self.last_success)
        status = "starting" if age is None else "healthy"
        if self.last_error or (age is not None and age > 6):
            status = "stale"
        if self.storage_error:
            status = "degraded"
        return {"status": status, "last_success": self.last_success, "age_seconds": age,
                "message": self.storage_error or self.last_error,
                "dropped_events": self.dropped_events, "hls_timeout_seconds": 30}

    def dashboard(self):
        now = time.time()
        self.maintain(now)
        live = self.store.live(now)
        return {"generated_at": now, "health": self.health(now), **live,
                "streams": self.streams,
                "recent_events": self.store.events(limit=12, now=now)["items"],
                "history": self.store.history(now)}
