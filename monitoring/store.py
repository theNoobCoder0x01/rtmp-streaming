"""SQLite persistence and interval-based reporting; no network dependencies."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import threading
import time


ROLES = ("publisher", "rtmp_viewer", "hls_viewer")
SCHEMA = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    connection_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('publisher','rtmp_viewer','hls_viewer')),
    application TEXT NOT NULL,
    stream_name TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    missing_since REAL,
    ended_at REAL,
    start_source TEXT NOT NULL,
    end_reason TEXT,
    CHECK(ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX one_open_connection ON sessions(role,connection_key) WHERE ended_at IS NULL;
CREATE INDEX session_time ON sessions(started_at,ended_at);
CREATE INDEX session_stream ON sessions(stream_name,started_at);
CREATE INDEX session_ip ON sessions(ip_address,started_at);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    kind TEXT NOT NULL CHECK(kind IN ('connect','disconnect')),
    occurred_at REAL NOT NULL,
    observed_at REAL NOT NULL,
    reason TEXT NOT NULL,
    estimated INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id,kind)
);
CREATE INDEX event_time ON events(occurred_at DESC,id DESC);
CREATE TABLE samples (
    id INTEGER PRIMARY KEY,
    sampled_at REAL NOT NULL,
    publishers INTEGER NOT NULL,
    rtmp_viewers INTEGER NOT NULL,
    hls_viewers INTEGER NOT NULL,
    bandwidth_in REAL NOT NULL,
    bandwidth_out REAL NOT NULL
);
CREATE INDEX sample_time ON samples(sampled_at);
PRAGMA user_version=1;
"""


class MonitoringStore:
    def __init__(self, path):
        self.path = str(path)
        self.write_lock = threading.RLock()

    @contextmanager
    def connection(self, write=False):
        lock = self.write_lock if write else _NoLock()
        with lock:
            conn = sqlite3.connect(self.path, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                else:
                    conn.execute("BEGIN")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self, now=None):
        now = time.time() if now is None else now
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.write_lock, closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript(SCHEMA)
            elif version != 1:
                raise RuntimeError("Unsupported monitoring schema version")
            conn.commit()
        with self.connection(write=True) as conn:
            for row in conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL").fetchall():
                self._end(conn, row, row["last_seen_at"], "monitor_restart", True, now)

    def _start(self, conn, client, source, now):
        row = conn.execute("SELECT * FROM sessions WHERE role=? AND connection_key=? AND ended_at IS NULL",
                           (client["role"], client["connection_key"])).fetchone()
        if row and row["ip_address"] != client["ip_address"]:
            self._end(conn, row, now, "client_id_reused", True, now)
            row = None
        if row:
            conn.execute("UPDATE sessions SET last_seen_at=MAX(last_seen_at,?),missing_since=NULL WHERE id=?", (now, row["id"]))
            return row["id"]
        if source == "stats_recovery":
            previous = conn.execute("SELECT ended_at,end_reason FROM sessions WHERE role=? AND connection_key=? ORDER BY id DESC LIMIT 1",
                                    (client["role"], client["connection_key"])).fetchone()
            if previous and previous["ended_at"] is not None:
                # A poll can finish after a newer, exact disconnect callback.
                # A brief tombstone also covers Nginx's notification teardown.
                if previous["ended_at"] >= now or (previous["end_reason"] == "callback" and previous["ended_at"] > now - 6):
                    return None
        cursor = conn.execute("""INSERT INTO sessions
            (connection_key,role,application,stream_name,ip_address,username,started_at,last_seen_at,start_source)
            VALUES(?,?,?,?,?,?,?,?,?)""", (client["connection_key"], client["role"], client["application"],
            client["stream_name"], client["ip_address"], client.get("username", ""), now, now, source))
        conn.execute("INSERT INTO events(session_id,kind,occurred_at,observed_at,reason,estimated) VALUES(?,?,?,?,?,?)",
                     (cursor.lastrowid, "connect", now, now, source, int(source == "stats_recovery")))
        return cursor.lastrowid

    def start(self, client, source="callback", now=None):
        now = time.time() if now is None else now
        with self.connection(write=True) as conn:
            return self._start(conn, client, source, now)

    @staticmethod
    def _end(conn, row, at, reason, estimated, observed):
        at = max(at, row["started_at"])
        changed = conn.execute("UPDATE sessions SET ended_at=?,end_reason=? WHERE id=? AND ended_at IS NULL",
                               (at, reason, row["id"])).rowcount
        if changed:
            conn.execute("""INSERT OR IGNORE INTO events
                (session_id,kind,occurred_at,observed_at,reason,estimated) VALUES(?,?,?,?,?,?)""",
                         (row["id"], "disconnect", at, observed, reason, int(estimated)))

    def end(self, role, connection_key, reason="callback", now=None):
        now = time.time() if now is None else now
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE role=? AND connection_key=? AND ended_at IS NULL",
                               (role, connection_key)).fetchone()
            if row:
                self._end(conn, row, now, reason, False, now)

    def end_browser(self, browser_key, now=None):
        now = time.time() if now is None else now
        with self.connection(write=True) as conn:
            rows = conn.execute("SELECT * FROM sessions WHERE role='hls_viewer' AND ended_at IS NULL AND connection_key LIKE ?",
                                (browser_key + ":%",)).fetchall()
            for row in rows:
                self._end(conn, row, now, "login_ended", False, now)

    def reconcile(self, clients, now=None, grace=6):
        now = time.time() if now is None else now
        seen = {(client["role"], client["connection_key"]) for client in clients}
        with self.connection(write=True) as conn:
            for client in clients:
                self._start(conn, client, "stats_recovery", now)
            rows = conn.execute("SELECT * FROM sessions WHERE role!='hls_viewer' AND ended_at IS NULL").fetchall()
            for row in rows:
                if (row["role"], row["connection_key"]) not in seen:
                    if row["last_seen_at"] > now:
                        continue  # This connection is newer than the fetched snapshot.
                    if row["missing_since"] is None:
                        conn.execute("UPDATE sessions SET missing_since=? WHERE id=?", (now, row["id"]))
                    elif row["missing_since"] <= now - grace:
                        self._end(conn, row, row["last_seen_at"], "stats_missing", True, now)

    def reset_absence(self):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE sessions SET missing_since=NULL WHERE ended_at IS NULL AND missing_since IS NOT NULL")

    def expire_hls(self, now=None, timeout=30):
        now = time.time() if now is None else now
        with self.connection(write=True) as conn:
            rows = conn.execute("SELECT * FROM sessions WHERE role='hls_viewer' AND ended_at IS NULL AND last_seen_at<=?",
                                (now - timeout,)).fetchall()
            for row in rows:
                self._end(conn, row, row["last_seen_at"], "media_inactivity", True, now)

    def sample(self, bandwidth_in, bandwidth_out, now=None):
        now = time.time() if now is None else now
        with self.connection(write=True) as conn:
            last = conn.execute("SELECT MAX(sampled_at) FROM samples").fetchone()[0]
            if last is not None and now - last < 30:
                return
            counts = dict(conn.execute("SELECT role,COUNT(*) FROM sessions WHERE ended_at IS NULL GROUP BY role").fetchall())
            conn.execute("""INSERT INTO samples
                (sampled_at,publishers,rtmp_viewers,hls_viewers,bandwidth_in,bandwidth_out) VALUES(?,?,?,?,?,?)""",
                         (now, counts.get("publisher", 0), counts.get("rtmp_viewer", 0),
                          counts.get("hls_viewer", 0), bandwidth_in, bandwidth_out))
            conn.execute("DELETE FROM samples WHERE sampled_at<?", (now - 30 * 86400,))

    @staticmethod
    def _where(filters, now, events=False):
        start, end = filters.get("start", now - 86400), filters.get("end", now)
        if events:
            parts, args = ["e.occurred_at>=?", "e.occurred_at<?"], [start, end]
        else:
            parts, args = ["s.started_at<?", "COALESCE(s.ended_at,?)>?"], [end, now, start]
        for key, column in (("role", "role"), ("stream", "stream_name"), ("ip", "ip_address")):
            if filters.get(key):
                parts.append("s." + column + "=?")
                args.append(filters[key])
        if filters.get("status") == "active":
            parts.append("s.ended_at IS NULL")
        elif filters.get("status") == "ended":
            parts.append("s.ended_at IS NOT NULL")
        return " AND ".join(parts), args

    @staticmethod
    def _public_session(row, now):
        item = dict(row)
        item.pop("connection_key", None)
        item.pop("missing_since", None)
        item["duration_seconds"] = max(0, (item["ended_at"] if item["ended_at"] is not None else now) - item["started_at"])
        item["status"] = "active" if item["ended_at"] is None else "ended"
        item["estimated"] = (item["role"] == "hls_viewer" or item["start_source"] == "stats_recovery"
                             or item["end_reason"] in ("stats_missing", "monitor_restart", "client_id_reused"))
        return item

    def sessions(self, filters=None, limit=50, offset=0, now=None):
        now = time.time() if now is None else now
        where, args = self._where(filters or {}, now)
        with self.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM sessions s WHERE " + where, args).fetchone()[0]
            rows = conn.execute("SELECT s.* FROM sessions s WHERE " + where +
                                " ORDER BY s.started_at DESC,s.id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return {"items": [self._public_session(row, now) for row in rows], "total": total, "limit": limit, "offset": offset}

    def events(self, filters=None, limit=50, offset=0, now=None):
        now = time.time() if now is None else now
        where, args = self._where(filters or {}, now, events=True)
        joined = " FROM events e JOIN sessions s ON s.id=e.session_id WHERE " + where
        with self.connection() as conn:
            total = conn.execute("SELECT COUNT(*)" + joined, args).fetchone()[0]
            rows = conn.execute("""SELECT e.*,s.role,s.application,s.stream_name,s.ip_address,s.username""" +
                                joined + " ORDER BY e.occurred_at DESC,e.id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def live(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as conn:
            counts = dict(conn.execute("SELECT role,COUNT(*) FROM sessions WHERE ended_at IS NULL GROUP BY role").fetchall())
            ips = conn.execute("SELECT COUNT(DISTINCT ip_address) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
            rows = conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC,id DESC LIMIT 200").fetchall()
        return {"counts": {role: counts.get(role, 0) for role in ROLES}, "unique_ips": ips,
                "connections": [self._public_session(row, now) for row in rows],
                "total_connections": sum(counts.values())}

    def history(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as conn:
            rows = conn.execute("""SELECT CAST(sampled_at/300 AS INTEGER)*300 AS at,
                MAX(publishers) AS publishers,MAX(rtmp_viewers) AS rtmp_viewers,MAX(hls_viewers) AS hls_viewers,
                AVG(bandwidth_in) AS bandwidth_in,AVG(bandwidth_out) AS bandwidth_out
                FROM samples WHERE sampled_at>=? GROUP BY CAST(sampled_at/300 AS INTEGER) ORDER BY at""",
                                (now - 86400,)).fetchall()
        return [dict(row) for row in rows]

    def report(self, filters=None, now=None):
        now = time.time() if now is None else now
        filters = filters or {}
        start, end = filters.get("start", now - 86400), min(filters.get("end", now), now)
        where, args = self._where(filters, now)
        duration = "MAX(0,MIN(COALESCE(s.ended_at,?),?)-MAX(s.started_at,?))"
        duration_args = [now, end, start]
        with self.connection() as conn:
            summary = dict(conn.execute("SELECT COUNT(*) AS sessions,COUNT(DISTINCT s.ip_address) AS unique_ips,"
                                       "COALESCE(SUM(" + duration + "),0) AS duration_seconds FROM sessions s WHERE " + where,
                                       duration_args + args).fetchone())
            groups = {}
            for label, column in (("roles", "role"), ("streams", "stream_name"), ("ips", "ip_address")):
                rows = conn.execute("SELECT s." + column + " AS label,COUNT(*) AS sessions,"
                                    "COUNT(DISTINCT s.ip_address) AS unique_ips,SUM(" + duration +
                                    ") AS duration_seconds FROM sessions s WHERE " + where +
                                    " GROUP BY s." + column + " ORDER BY sessions DESC,label LIMIT 100",
                                    duration_args + args).fetchall()
                groups[label] = [dict(row) for row in rows]
            peak = conn.execute("""WITH spans AS (
                SELECT MAX(s.started_at,?) AS start,MIN(COALESCE(s.ended_at,?),?) AS end
                FROM sessions s WHERE """ + where + """), points AS (
                SELECT start AS at,1 AS delta FROM spans WHERE end>start UNION ALL
                SELECT end AS at,-1 AS delta FROM spans WHERE end>start), grouped AS (
                SELECT at,SUM(delta) AS delta FROM points GROUP BY at), running AS (
                SELECT SUM(delta) OVER (ORDER BY at ROWS UNBOUNDED PRECEDING) AS count FROM grouped)
                SELECT COALESCE(MAX(count),0) FROM running""", [start, now, end] + args).fetchone()[0]
            summary["peak_connections"] = peak
            event_where, event_args = self._where(filters, now, events=True)
            counts = dict(conn.execute("SELECT e.kind,COUNT(*) FROM events e JOIN sessions s ON s.id=e.session_id WHERE " +
                                       event_where + " GROUP BY e.kind", event_args).fetchall())
            summary["connects"] = counts.get("connect", 0)
            summary["disconnects"] = counts.get("disconnect", 0)
        return {"start": start, "end": end, "summary": summary, **groups}

    def export_page(self, filters, after_id, max_id, now):
        where, args = self._where(filters, now)
        with self.connection() as conn:
            rows = conn.execute("SELECT s.* FROM sessions s WHERE " + where +
                                " AND s.id>? AND s.id<=? ORDER BY s.id LIMIT 200", args + [after_id, max_id]).fetchall()
        return [self._public_session(row, now) for row in rows]

    def max_session_id(self):
        with self.connection() as conn:
            return conn.execute("SELECT COALESCE(MAX(id),0) FROM sessions").fetchone()[0]


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
