from pathlib import Path
import queue
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from passlib.hash import sha512_crypt

import server
from monitoring.store import MonitoringStore


ROOT = Path(__file__).resolve().parents[1]


class AddressedApp:
    def __init__(self, address):
        self.address = address

    async def __call__(self, scope, receive, send):
        scope = {**scope, "client": (self.address, 12345)}
        await server.app(scope, receive, send)


class MonitoringAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = sha512_crypt.hash("monitor-test-password")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="monitor-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for page in ROOT.glob("*.html"):
            (self.root / page.name).write_text(page.read_text())
        (self.root / ".htpasswd").write_text("viewer:" + self.password_hash + "\n")
        self.store = MonitoringStore(self.root / "data.sqlite3")
        self.store.initialize()
        monitor_patch = patch.multiple(server.monitoring, store=self.store, enabled=True, hls_root=self.root,
                                       last_success=None, last_error=None, storage_error=None,
                                       streams=[], hls_touches={}, revoked_browsers={}, pending_activity=queue.Queue())
        auth_patch = patch.multiple(server.viewer_auth, html_root=self.root,
                                    password_file=str(self.root / ".htpasswd"))
        monitor_patch.start()
        auth_patch.start()
        self.addCleanup(monitor_patch.stop)
        self.addCleanup(auth_patch.stop)
        server.viewer_auth.sessions.clear()
        self.addCleanup(server.viewer_auth.sessions.clear)
        self.lan = TestClient(AddressedApp("192.168.1.88"))
        self.private = TestClient(AddressedApp("127.0.0.1"))
        self.addCleanup(self.lan.close)
        self.addCleanup(self.private.close)

    def sign_in(self, target="/m.html"):
        response = self.lan.post("/auth/login", data={"username": "viewer", "password": "monitor-test-password", "return_to": target}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.lan.get(response.headers["location"]).status_code, 200)

    def notify(self, event="publish", **changes):
        body = {"app": "live", "name": "test", "clientid": "10", "addr": "192.168.1.22"}
        body.update(changes)
        return self.private.post("/internal/rtmp/" + event, data=body, headers={"x-internal-rtmp": "1"})

    def test_monitoring_data_requires_viewer_login(self):
        for path in ("dashboard", "sessions", "events", "reports", "sessions.csv"):
            self.assertEqual(self.lan.get("/api/monitoring/" + path).status_code, 401)

    def test_private_callback_requires_loopback_and_marker_even_for_logged_in_user(self):
        self.sign_in()
        path = "/internal/rtmp/publish"
        self.assertEqual(self.lan.post(path, headers={"x-internal-rtmp": "1", "x-forwarded-for": "127.0.0.1"}).status_code, 404)
        self.assertEqual(self.private.post(path).status_code, 404)
        self.assertEqual(self.notify().status_code, 200)
        rows = self.lan.get("/api/monitoring/dashboard").json()
        self.assertEqual(rows["counts"]["publisher"], 1)
        self.assertEqual(rows["connections"][0]["ip_address"], "192.168.1.22")

    def test_callbacks_populate_events_reports_and_csv(self):
        self.sign_in()
        self.notify()
        self.notify()
        self.notify("publish_done")
        self.notify("publish_done")
        self.assertEqual(self.lan.get("/api/monitoring/events").json()["total"], 2)
        report = self.lan.get("/api/monitoring/reports").json()
        self.assertEqual(report["summary"]["sessions"], 1)
        self.assertEqual(report["ips"][0]["label"], "192.168.1.22")
        export = self.lan.get("/api/monitoring/sessions.csv")
        self.assertEqual(export.status_code, 200)
        self.assertIn("started_at_utc", export.text)
        self.assertIn("192.168.1.22", export.text)
        self.assertIn("attachment", export.headers["content-disposition"])

    def test_hls_tracks_actual_lan_ip_and_logout_not_forwarded_input(self):
        self.sign_in("/player.html")
        directory = self.root / "hls"
        directory.mkdir()
        (directory / "test.m3u8").write_text("#EXTM3U")
        self.lan.get("/hls/test.m3u8", headers={"x-forwarded-for": "10.0.0.99"})
        live = self.lan.get("/api/monitoring/dashboard").json()
        self.assertEqual(live["counts"]["hls_viewer"], 1)
        self.assertEqual(live["connections"][0]["ip_address"], "192.168.1.88")
        self.assertEqual(live["connections"][0]["username"], "viewer")
        self.lan.get("/player.html")
        server.monitoring.maintain()
        self.assertEqual(self.store.live()["counts"]["hls_viewer"], 0)
        self.assertEqual(self.store.events()["items"][0]["reason"], "login_ended")

    def test_dashboard_visit_does_not_count_as_video_viewer(self):
        self.sign_in()
        self.assertEqual(self.lan.get("/api/monitoring/dashboard").json()["total_connections"], 0)

    def test_bad_filters_and_unbounded_pages_are_rejected(self):
        self.sign_in()
        for query in ("role=admin", "ip=bad", "limit=5000", "offset=-1", "start=2026-01-01T00:00:00", "start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z"):
            self.assertEqual(self.lan.get("/api/monitoring/sessions?" + query).status_code, 422)

    def test_malformed_notifications_are_not_publishing_authorization_errors(self):
        self.assertEqual(self.notify(addr="invalid").status_code, 200)
        self.assertEqual(self.store.live()["total_connections"], 0)

    def test_database_errors_return_a_useful_error_and_callback_still_succeeds(self):
        self.sign_in()
        with patch.object(self.store, "live", side_effect=sqlite3.OperationalError("unavailable")):
            response = self.lan.get("/api/monitoring/dashboard")
            self.assertEqual(response.status_code, 503)
        with patch.object(self.store, "start", side_effect=sqlite3.OperationalError("unavailable")):
            self.assertEqual(self.notify().status_code, 200)


if __name__ == "__main__":
    unittest.main()
