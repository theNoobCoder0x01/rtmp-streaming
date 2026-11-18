"""Viewer sessions that admit one page load and expire on the next reload."""

from dataclasses import dataclass
from html import escape
from pathlib import Path
import secrets
import time
from typing import Dict, Optional
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from passlib.apache import HtpasswdFile
from passlib.context import CryptContext
from starlette.concurrency import run_in_threadpool


COOKIE_NAME = "viewer_session"
PASSWORD_CONTEXT = CryptContext(schemes=["sha512_crypt", "sha256_crypt", "apr_md5_crypt"])


@dataclass
class ViewerSession:
    target: str
    expires_at: float
    page_loaded: bool = False


class ViewerAuth:
    def __init__(self, html_root="/usr/local/nginx/html",
                 password_file="/etc/nginx/secrets/.htpasswd", lifetime=12 * 60 * 60):
        self.html_root = Path(html_root).resolve()
        self.password_file = str(password_file)
        self.lifetime = lifetime
        self.sessions: Dict[str, ViewerSession] = {}

    def _session(self, token: Optional[str]) -> Optional[ViewerSession]:
        session = self.sessions.get(token)
        if session and session.expires_at <= time.monotonic():
            self.revoke(token)
            return None
        return session

    def is_authenticated(self, token: Optional[str]) -> bool:
        session = self._session(token)
        return bool(session and session.page_loaded)

    def revoke(self, token: Optional[str]):
        self.sessions.pop(token, None)

    def _target(self, value: str) -> str:
        # Redirect only to an existing viewer page on this server.
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return "/"
        try:
            parsed = urlsplit(value)
        except ValueError:
            return "/"
        pages = {"/"} | {"/" + page.name for page in self.html_root.glob("*.html")
                         if page.name != "login.html"}
        if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in pages:
            return "/"
        return parsed.path + ("?" + parsed.query if parsed.query else "")

    def _login_page(self, target: str, error="", status_code=200):
        template = (self.html_root / "login.html").read_text()
        response = HTMLResponse(
            template.replace("{{return_to}}", escape(target, quote=True))
                    .replace("{{error}}", escape(error)),
            status_code=status_code,
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )
        response.delete_cookie(COOKIE_NAME, httponly=True, samesite="strict")
        return response

    def _verify_password(self, username: str, password: str) -> bool:
        try:
            return bool(HtpasswdFile(self.password_file, context=PASSWORD_CONTEXT)
                        .check_password(username, password))
        except (OSError, ValueError, TypeError):
            return False

    async def protect(self, request: Request, call_next):
        if request.url.path == "/auth/login" and request.method == "POST":
            response = await call_next(request)
        else:
            token = request.cookies.get(COOKIE_NAME)
            session = self._session(token)
            document = request.url.path == "/" or request.url.path.endswith(".html")
            if document:
                target = self._target(request.url.path +
                                      ("?" + request.url.query if request.url.query else ""))
                # Only the redirect after a successful login can admit a page.
                if (request.method != "GET" or not session or session.page_loaded
                        or session.target != target):
                    self.revoke(token)
                    return self._login_page(target)
                session.page_loaded = True
            elif not self.is_authenticated(token):
                response = JSONResponse({"detail": "Sign in to view this resource."}, status_code=401)
                response.delete_cookie(COOKIE_NAME, httponly=True, samesite="strict")
                response.headers["Cache-Control"] = "no-store"
                return response
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def login(self, request: Request):
        origin = request.headers.get("origin")
        if origin and origin != "{}://{}".format(request.url.scheme, request.headers["host"]):
            return Response(status_code=403)
        self.revoke(request.cookies.get(COOKIE_NAME))
        if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            return Response(status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return Response(status_code=413)
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except (ValueError, UnicodeError):
            return Response(status_code=400)
        target = self._target(fields.get("return_to", ["/"])[0])
        username = fields.get("username", [""])[0]
        password = fields.get("password", [""])[0]
        if not username or not password or not await run_in_threadpool(self._verify_password, username, password):
            return self._login_page(target, "Incorrect username or password.", 401)
        now = time.monotonic()
        for token, session in list(self.sessions.items()):
            if session.expires_at <= now:
                self.revoke(token)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = ViewerSession(target, now + self.lifetime)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
        return response

    async def serve_file(self, file_path: str):
        if file_path == "stat":
            return Response(headers={"X-Accel-Redirect": "/_stream_stats"})
        parts = file_path.split("/")
        if any(part in (".", "..") for part in parts) or "\\" in file_path or "\x00" in file_path:
            return Response(status_code=404)
        if parts[0] in {"hls", "hls1", "hls2", "hls3", "hls4", "hls5"}:
            if len(parts) < 2 or Path(file_path).suffix not in (".m3u8", ".ts"):
                return Response(status_code=404)
            return Response(headers={"X-Accel-Redirect": "/_stream_hls/" + quote(file_path, safe="/")})
        path = (self.html_root / (file_path or "index.html")).resolve()
        try:
            relative = path.relative_to(self.html_root)
        except ValueError:
            return Response(status_code=404)
        if not path.is_file() or path.name == "login.html":
            return Response(status_code=404)
        if path.suffix == ".html":
            document = await run_in_threadpool(path.read_text)
            # Back/forward cache restoration can skip HTTP altogether.
            guard = '<script>addEventListener("pageshow",function(e){if(e.persisted){location.reload();}});</script>'
            return HTMLResponse(document.replace("</head>", guard + "</head>", 1))
        return Response(headers={"X-Accel-Redirect": "/_stream_files/" + quote(relative.as_posix(), safe="/")})

    def install(self, app):
        app.middleware("http")(self.protect)
        app.add_api_route("/auth/login", self.login, methods=["POST"])
        # Install after the API so this fallback cannot hide its routes.
        app.add_api_route("/{file_path:path}", self.serve_file, methods=["GET", "HEAD"])
