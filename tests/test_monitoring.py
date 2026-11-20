import csv
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from monitoring.api import session_csv
from monitoring.service import MonitoringService, browser_key, connection_key, parse_stats
from monitoring.store import MonitoringStore


def client(identity="1", role="publisher", ip="192.168.1.20", name="test"):
    return {"connection_key": connection_key(role, "live", name, identity), "role": role,
            "application": "live", "stream_name": name, "ip_address": ip, "username": ""}


XML = """<rtmp><server><application><name>live</name><live><stream><name>test</name>
<bw_in>4000000</bw_in><bw_out>8000000</bw_out><bytes_in>10000000</bytes_in><bytes_out>20000000</bytes_out>
<meta><video><height>1080</height></video></meta>
<client><id>1</id><address>192.168.1.20</address><publishing/></client>
<client><id>2</id><address>192.168.1.21</address></client>
</stream></live></application></server></rtmp>"""


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="monitor-tests-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "monitor.sqlite3"
        self.service = MonitoringService(self.path, hls_root=self.temp.name)
        self.service.initialize(now=0)
        self.store = self.service.store

    def test_atomic_idempotent_connect_disconnect_and_reconnect(self):
        item = client()
        first = self.store.start(item, now=100)
        self.assertEqual(self.store.start(item, now=101), first)
        self.store.end(item["role"], item["connection_key"], now=110)
        self.store.end(item["role"], item["connection_key"], now=111)
        second = self.store.start(item, now=120)
        self.assertNotEqual(first, second)
        events = self.store.events({"start": 0, "end": 200}, now=200)["items"]
        self.assertEqual([event["kind"] for event in events], ["connect", "disconnect", "connect"])
        self.assertEqual(self.store.live(now=200)["counts"]["publisher"], 1)

    def test_multiple_connections_with_same_ip_are_separate_sessions(self):
        self.store.start(client("1"), now=100)
        self.store.start(client("2"), now=100)
        live = self.store.live(now=110)
        self.assertEqual(live["counts"]["publisher"], 2)
        self.assertEqual(live["unique_ips"], 1)

    def test_callback_retains_nginx_ip_and_does_not_store_auth_tokens(self):
        fields = {"app": "live", "name": "test", "clientid": "9", "addr": "192.168.1.55"}
        self.service.callback("publish", fields, now=100)
        self.service.callback("publish_done", fields, now=120)
        row = self.store.sessions({"start": 0, "end": 200}, now=200)["items"][0]
        self.assertEqual(row["ip_address"], "192.168.1.55")
        self.assertEqual(row["duration_seconds"], 20)
        self.assertNotIn("connection_key", row)

    def test_reconciliation_recovers_but_requires_a_grace_period_to_end(self):
        item = client()
        self.store.reconcile([item], now=100)
        self.store.reconcile([], now=105)
        self.assertEqual(self.store.live(now=105)["counts"]["publisher"], 1)
        self.store.reconcile([], now=107)
        self.assertEqual(self.store.live(now=107)["counts"]["publisher"], 1)
        self.store.reconcile([], now=111)
        self.assertEqual(self.store.live(now=107)["counts"]["publisher"], 0)
        events = self.store.events({"start": 0, "end": 200}, now=200)["items"]
        self.assertTrue(all(event["estimated"] for event in events))
        self.assertEqual(events[0]["reason"], "stats_missing")

    def test_failed_snapshot_keeps_last_known_live_connections(self):
        self.service.observe(parse_stats(ET.fromstring(XML)), now=100)
        self.service.failed_poll()
        self.assertEqual(self.store.live(now=120)["counts"]["publisher"], 1)
        self.assertEqual(self.service.health(now=120)["status"], "stale")

    def test_restart_persists_history_and_closes_stale_sessions(self):
        self.store.start(client(), now=100)
        recovered = MonitoringStore(self.path)
        recovered.initialize(now=200)
        row = recovered.sessions({"start": 0, "end": 300}, now=300)["items"][0]
        self.assertEqual(row["ended_at"], 100)
        self.assertEqual(row["end_reason"], "monitor_restart")
        self.assertEqual(recovered.events({"start": 0, "end": 300}, now=300)["total"], 2)

    def test_schema_version_and_wal(self):
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_hls_counts_real_media_not_dashboard_or_missing_files(self):
        root = Path(self.temp.name) / "hls"
        root.mkdir()
        (root / "test.m3u8").write_text("#EXTM3U")
        (root / "test-1.ts").write_bytes(b"segment")
        token = "private-auth-token"
        self.service.hls_request(token, "viewer", "192.168.1.70", "hls/missing.m3u8", now=100)
        self.assertEqual(self.store.live(now=101)["counts"]["hls_viewer"], 0)
        self.service.hls_request(token, "viewer", "192.168.1.70", "hls/test.m3u8", now=100)
        self.service.hls_request(token, "viewer", "192.168.1.70", "hls/test-1.ts", now=106)
        self.service.maintain(now=106)
        self.assertEqual(self.store.live(now=110)["counts"]["hls_viewer"], 1)
        self.store.expire_hls(now=135)
        self.assertEqual(self.store.live(now=135)["counts"]["hls_viewer"], 1)
        self.store.expire_hls(now=136)
        row = self.store.sessions({"start": 0, "end": 200}, now=200)["items"][0]
        self.assertEqual(row["ended_at"], 106)
        self.assertEqual(row["end_reason"], "media_inactivity")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertNotIn(token, str(conn.execute("SELECT * FROM sessions").fetchall()))

    def test_logout_ends_browser_sessions(self):
        item = client(role="hls_viewer")
        item["connection_key"] = browser_key("secret") + ":stream"
        self.store.start(item, source="hls_request", now=100)
        with patch("monitoring.service.time.time", return_value=120):
            self.service.end_browser("secret")
        self.service.maintain(now=120)
        row = self.store.sessions({"start": 0, "end": 200}, now=200)["items"][0]
        self.assertEqual(row["end_reason"], "login_ended")
        self.assertEqual(row["ended_at"], 120)

    def test_reports_clip_durations_and_compute_peak_from_intervals(self):
        first, second, third = client("1"), client("2", "rtmp_viewer"), client("3", "hls_viewer")
        self.store.start(first, now=80)
        self.store.start(second, now=110)
        self.store.start(third, now=120)
        self.store.end(first["role"], first["connection_key"], now=120)
        self.store.end(second["role"], second["connection_key"], now=140)
        self.store.end(third["role"], third["connection_key"], now=160)
        report = self.store.report({"start": 100, "end": 150}, now=200)
        self.assertEqual(report["summary"]["sessions"], 3)
        self.assertEqual(report["summary"]["duration_seconds"], 80)
        self.assertEqual(report["summary"]["peak_connections"], 2)
        self.assertEqual(report["summary"]["connects"], 2)
        self.assertEqual(report["summary"]["disconnects"], 2)

    def test_session_at_range_end_is_excluded_and_boundary_end_is_excluded(self):
        item = client()
        self.store.start(item, now=100)
        self.store.end(item["role"], item["connection_key"], now=110)
        self.assertEqual(self.store.report({"start": 110, "end": 120}, now=200)["summary"]["sessions"], 0)
        self.assertEqual(self.store.report({"start": 90, "end": 100}, now=200)["summary"]["sessions"], 0)

    def test_filters_pagination_and_csv_formula_escaping(self):
        self.store.start(client("1", name="=DANGER()"), now=100)
        self.store.start(client("2", ip="192.168.1.21", name="other"), now=101)
        selected = {"start": 0, "end": 200, "ip": "192.168.1.20"}
        rows = self.store.sessions(selected, limit=1, now=200)
        self.assertEqual(rows["total"], 1)
        self.assertEqual(rows["items"][0]["stream_name"], "=DANGER()")
        self.assertEqual(self.store.sessions({"start": 0, "end": 200}, limit=1, offset=1, now=200)["items"][0]["stream_name"], "=DANGER()")
        exported = list(csv.DictReader(io.StringIO("".join(session_csv(self.store, selected, 200)))))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["stream_name"], "'=DANGER()")

    def test_metrics_are_throttled_and_history_is_persistent(self):
        self.store.sample(2, 4, now=100)
        self.store.sample(3, 5, now=110)
        self.store.sample(4, 6, now=131)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)
        self.assertEqual(self.store.history(now=200)[0]["bandwidth_out"], 5)

    def test_notify_database_error_does_not_stop_broadcasting(self):
        with patch.object(self.store, "start", side_effect=sqlite3.OperationalError("busy")):
            self.service.callback("publish", {"app": "live", "name": "test", "clientid": "1", "addr": "192.168.1.20"}, now=100)
        self.assertEqual(self.service.health(now=101)["status"], "degraded")

    def test_parser_distinguishes_a_publisher_from_waiting_viewers(self):
        parsed = parse_stats(ET.fromstring(XML))
        self.assertEqual(parsed["streams"][0]["publishers"], 1)
        self.assertEqual(parsed["streams"][0]["rtmp_viewers"], 1)
        self.assertEqual(parsed["streams"][0]["bandwidth_in"], 4)
        root = ET.fromstring(XML)
        first = root.find("server/application/live/stream/client")
        first.remove(first.find("publishing"))
        self.assertEqual(parse_stats(root)["streams"][0]["status"], "inactive")

    def test_malformed_statistics_do_not_become_empty_successful_snapshots(self):
        for xml in ("<html/>", "<rtmp/>", XML.replace("192.168.1.20", "not-an-ip")):
            with self.subTest(xml=xml):
                with self.assertRaises(ValueError):
                    parse_stats(ET.fromstring(xml))

    def test_concurrent_duplicate_callbacks_remain_idempotent(self):
        item = client()
        with ThreadPoolExecutor(max_workers=4) as workers:
            ids = list(workers.map(lambda _: self.store.start(item, now=100), range(16)))
        self.assertEqual(len(set(ids)), 1)
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda _: self.store.end(item["role"], item["connection_key"], now=120), range(16)))
        self.assertEqual(self.store.events({"start": 0, "end": 200}, now=200)["total"], 2)

    def test_media_writes_are_queued_and_logout_cannot_reopen_a_viewer(self):
        directory = Path(self.temp.name) / "hls"
        directory.mkdir()
        (directory / "test.m3u8").write_text("#EXTM3U")
        self.service.hls_request("secret", "viewer", "192.168.1.20", "hls/test.m3u8", now=100)
        self.assertEqual(self.store.live(now=100)["total_connections"], 0)
        with patch("monitoring.service.time.time", return_value=120):
            self.service.end_browser("secret")
        self.service.hls_request("secret", "viewer", "192.168.1.20", "hls/test.m3u8", now=125)
        self.service.maintain(now=125)
        self.assertEqual(self.store.live(now=125)["total_connections"], 0)
        rows = self.store.sessions({"start": 0, "end": 200}, now=200)["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["started_at"], 100)
        self.assertEqual(rows[0]["ended_at"], 120)

    def test_stats_outage_does_not_count_toward_missing_client_grace(self):
        self.service.observe(parse_stats(ET.fromstring(XML)), now=100)
        empty = {"clients": [], "streams": []}
        self.service.observe(empty, now=102)
        self.service.failed_poll()
        self.service.observe(empty, now=200)
        self.assertEqual(self.store.live(now=200)["counts"]["publisher"], 1)
        self.service.observe(empty, now=206)
        self.assertEqual(self.store.live(now=206)["counts"]["publisher"], 0)

    def test_legacy_player_state_follows_publishers_and_survives_bad_xml(self):
        from server import StreamManager
        manager = StreamManager()
        root = ET.fromstring(XML)
        with patch("server.time.time", return_value=100):
            manager.process_rtmp_stats(root)
        with patch("server.time.time", return_value=120):
            manager.process_rtmp_stats(root)
        self.assertEqual(manager.streams["1935_test"].start_time, 100)
        first = root.find("server/application/live/stream/client")
        first.remove(first.find("publishing"))
        manager.process_rtmp_stats(root)
        self.assertEqual(manager.streams["1935_test"].status, "inactive")
        with self.assertRaises(ValueError):
            manager.process_rtmp_stats(ET.fromstring("<html/>"))
        self.assertIn("1935_test", manager.streams)

    def test_late_statistics_cannot_resurrect_a_newer_disconnect(self):
        item = client()
        self.store.start(item, now=100)
        self.store.end(item["role"], item["connection_key"], now=110)
        self.store.reconcile([item], now=109)
        self.store.reconcile([item], now=111)
        self.assertEqual(self.store.live(now=111)["counts"]["publisher"], 0)
        self.assertEqual(self.store.sessions({"start": 0, "end": 200}, now=200)["total"], 1)

    def test_old_snapshot_does_not_start_absence_clock_for_a_new_connection(self):
        item = client()
        self.store.start(item, now=200)
        self.store.reconcile([], now=199)
        self.store.reconcile([], now=201)
        self.store.reconcile([], now=205)
        self.assertEqual(self.store.live(now=205)["counts"]["publisher"], 1)
        self.store.reconcile([], now=207)
        self.assertEqual(self.store.live(now=207)["counts"]["publisher"], 0)


if __name__ == "__main__":
    unittest.main()
