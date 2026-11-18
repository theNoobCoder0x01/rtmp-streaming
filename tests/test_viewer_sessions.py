"""Exercise the real HTTP/WebSocket app in-process; no services are started."""

import base64
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from passlib.hash import apr_md5_crypt, sha512_crypt
from starlette.websockets import WebSocketDisconnect

import server
from viewer_auth import COOKIE_NAME


ROOT = Path(__file__).resolve().parents[1]


class ViewerSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = sha512_crypt.hash("viewer-test-password")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="viewer-session-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for page in ROOT.glob("*.html"):
            (self.root / page.name).write_text(page.read_text())
        (self.root / "public").mkdir()
        (self.root / "public" / "video-js.css").write_text("body {}")
        self.password_file = self.root / ".htpasswd"
        self.password_file.write_text("viewer:" + self.password_hash + "\n")
        self.auth = server.viewer_auth
        self.auth.sessions.clear()
        self.addCleanup(self.auth.sessions.clear)
        self.root_patch = patch.object(self.auth, "html_root", self.root)
        self.file_patch = patch.object(self.auth, "password_file", str(self.password_file))
        self.root_patch.start()
        self.file_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.file_patch.stop)
        # Avoid a lifespan context: the actual stream monitor must not run.
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def login(self, target="/", **fields):
        data = {"username": "viewer", "password": "viewer-test-password", "return_to": target}
        data.update(fields)
        return self.client.post("/auth/login", data=data, follow_redirects=False)

    def signed_in(self, target="/"):
        response = self.login(target)
        self.assertEqual(response.status_code, 303)
        token = self.client.cookies.get(COOKIE_NAME)
        response = self.client.get(response.headers["location"])
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Sign in to watch", response.text)
        return token

    def test_pages_show_form_without_basic_auth_challenge(self):
        for path in ("/", "/player.html", "/m.html", "/1.html"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Sign in to watch", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("www-authenticate", response.headers)

    def test_resources_require_a_session(self):
        for path in ("/api/streams", "/hls/test.m3u8", "/hls1/test.ts", "/stat",
                     "/debug/rtmp-stats", "/public/video-js.css"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("x-accel-redirect", response.headers)
                self.assertNotIn("www-authenticate", response.headers)

    def test_login_admits_only_one_page_load(self):
        token = self.signed_in("/player.html?mode=live")
        for _ in range(3):
            self.assertEqual(self.client.get("/api/streams").status_code, 200)
            self.assertEqual(self.client.get("/hls/test.m3u8").status_code, 200)
        response = self.client.get("/player.html?mode=live")
        self.assertIn("Sign in to watch", response.text)
        self.assertNotIn(token, self.auth.sessions)
        self.assertEqual(self.client.get("/api/streams").status_code, 401)

    def test_refresh_revokes_the_old_cookie_on_the_server(self):
        token = self.signed_in()
        self.client.get("/")
        replay = TestClient(server.app)
        self.addCleanup(replay.close)
        for path in ("/api/streams", "/hls/test.m3u8", "/hls/test-1.ts"):
            response = replay.get(path, headers={"cookie": COOKIE_NAME + "=" + token})
            self.assertEqual(response.status_code, 401)

    def test_navigation_to_another_page_also_requires_login(self):
        self.signed_in()
        self.assertIn("Sign in to watch", self.client.get("/m.html").text)

    def test_cookie_is_httponly_and_not_persistent(self):
        response = self.login()
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("Max-Age", cookie)
        self.assertNotIn("viewer-test-password", cookie)
        self.assertFalse(self.auth.is_authenticated(self.client.cookies.get(COOKIE_NAME)))

    def test_https_cookie_is_secure(self):
        client = TestClient(server.app, base_url="https://testserver")
        self.addCleanup(client.close)
        response = client.post("/auth/login", data={
            "username": "viewer", "password": "viewer-test-password", "return_to": "/",
        }, follow_redirects=False)
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_wrong_or_empty_credentials_do_not_create_a_session(self):
        for fields in ({"password": "wrong"}, {"username": "unknown"}, {"password": ""}):
            with self.subTest(fields=fields):
                response = self.login(**fields)
                self.assertEqual(response.status_code, 401)
                self.assertIn("Incorrect username or password", response.text)
                self.assertEqual(self.auth.sessions, {})

    def test_cached_basic_credentials_cannot_sign_in(self):
        encoded = base64.b64encode(b"viewer:viewer-test-password").decode()
        self.client.headers["authorization"] = "Basic " + encoded
        self.assertIn("Sign in to watch", self.client.get("/").text)
        self.assertEqual(self.client.get("/api/streams").status_code, 401)

    def test_apr1_mounted_password_files_still_work(self):
        self.password_file.write_text("viewer:" + apr_md5_crypt.hash("viewer-test-password") + "\n")
        self.signed_in()

    def test_missing_password_file_fails_closed(self):
        self.password_file.unlink()
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.auth.sessions, {})

    def test_expired_or_forged_sessions_are_rejected(self):
        token = self.signed_in()
        self.auth.sessions[token].expires_at = 0
        self.assertEqual(self.client.get("/api/streams").status_code, 401)
        self.assertNotIn(token, self.auth.sessions)
        self.assertEqual(self.client.get("/api/streams", headers={
            "cookie": COOKIE_NAME + "=forged",
        }).status_code, 401)

    def test_redirects_stay_on_this_server(self):
        for target in ("https://evil.example/", "//evil.example/", "/auth/login", "/login.html",
                       "//[broken", "/\r\nLocation: https://evil.example/"):
            with self.subTest(target=target):
                response = self.login(target)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/")

    def test_cross_origin_login_is_rejected(self):
        response = self.client.post("/auth/login", data={
            "username": "viewer", "password": "viewer-test-password",
        }, headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.auth.sessions, {})

    def test_authorized_media_and_files_use_internal_nginx_locations(self):
        self.signed_in()
        for folder in ("hls", "hls1", "hls2", "hls3", "hls4", "hls5"):
            for name in ("test.m3u8", "test-1.ts"):
                path = folder + "/" + name
                response = self.client.get("/" + path)
                self.assertEqual(response.headers["x-accel-redirect"], "/_stream_hls/" + path)
        self.assertEqual(self.client.get("/public/video-js.css").headers["x-accel-redirect"],
                         "/_stream_files/public/video-js.css")
        self.assertEqual(self.client.get("/stat").headers["x-accel-redirect"], "/_stream_stats")

    def test_file_traversal_and_unknown_hls_files_are_blocked(self):
        self.signed_in()
        for path in ("/hls/%2e%2e/secret.ts", "/public/%2e%2e/%2e%2e/secret",
                     "/hls/private.txt", "/hls/foo%5c..%5csecret.ts"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("x-accel-redirect", response.headers)

    def test_back_forward_cache_guard_is_injected(self):
        self.login()
        response = self.client.get("/")
        self.assertIn('addEventListener("pageshow"', response.text)
        self.assertIn("e.persisted", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_websocket_requires_login(self):
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect("/ws"):
                pass
        self.assertEqual(caught.exception.code, 1008)

    def test_websocket_closes_when_page_is_refreshed(self):
        self.signed_in()
        with self.client.websocket_connect("/ws") as websocket:
            websocket.send_text("ping")
            self.client.get("/")
            with self.assertRaises(WebSocketDisconnect) as caught:
                websocket.receive_text()
            self.assertEqual(caught.exception.code, 1008)

    def test_websocket_rejects_cross_origin_handshakes(self):
        self.signed_in()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
                pass

    def test_revoked_sessions_receive_no_further_broadcasts(self):
        token = self.signed_in()

        class Client:
            cookies = {COOKIE_NAME: token}
            sent = False
            closed = False

            async def send_json(self, data):
                self.sent = True

            async def close(self, code):
                self.closed = code

        client = Client()
        server.stream_manager.connected_clients.append(client)
        self.addCleanup(server.stream_manager.connected_clients.clear)
        self.client.get("/")
        asyncio.run(server.stream_manager.broadcast_updates())
        self.assertFalse(client.sent)
        self.assertEqual(client.closed, 1008)
        self.assertNotIn(client, server.stream_manager.connected_clients)


if __name__ == "__main__":
    unittest.main()
