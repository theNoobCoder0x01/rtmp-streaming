"""Authenticated reports and loopback-only RTMP notifications."""

import csv
from datetime import datetime, timezone
import io
import ipaddress
import logging
import sqlite3
import time
from typing import Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .store import ROLES


logger = logging.getLogger(__name__)


def filters(start: Optional[datetime] = None, end: Optional[datetime] = None,
            stream: Optional[str] = Query(None, max_length=512),
            ip: Optional[str] = None, role: Optional[str] = None, status: Optional[str] = None):
    now = time.time()
    if any(value is not None and value.tzinfo is None for value in (start, end)):
        raise HTTPException(422, "Dates must include a UTC offset or Z.")
    lower, upper = start.timestamp() if start else now - 86400, end.timestamp() if end else now
    if lower >= upper:
        raise HTTPException(422, "Start must be before end.")
    if lower > now:
        raise HTTPException(422, "Start cannot be in the future.")
    if role is not None and role not in ROLES:
        raise HTTPException(422, "Invalid connection role.")
    if status is not None and status not in ("active", "ended"):
        raise HTTPException(422, "Invalid connection status.")
    if ip:
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            raise HTTPException(422, "Enter a valid IP address.")
    return {"start": lower, "end": upper, "stream": stream, "ip": ip, "role": role, "status": status}


def safe_csv(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def session_csv(store, selected, now):
    maximum = store.max_session_id()
    columns = ("id", "role", "application", "stream_name", "ip_address", "username", "started_at",
               "last_seen_at", "ended_at", "duration_seconds", "estimated", "status", "start_source", "end_reason")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([column + "_utc" if column in ("started_at", "last_seen_at", "ended_at") else column for column in columns])
    yield output.getvalue()
    after = 0
    while True:
        rows = store.export_page(selected, after, maximum, now)
        if not rows:
            return
        output.seek(0)
        output.truncate(0)
        for row in rows:
            values = []
            for column in columns:
                value = row[column]
                if column in ("started_at", "last_seen_at", "ended_at") and value is not None:
                    value = datetime.fromtimestamp(value, timezone.utc).isoformat()
                values.append(safe_csv(value))
            writer.writerow(values)
        yield output.getvalue()
        after = rows[-1]["id"]


def install_routes(app, monitoring):
    router = APIRouter()

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        logger.error("Monitoring query failed", exc_info=error)
        return JSONResponse({"detail": "Monitoring storage is unavailable."}, status_code=503)

    def ready():
        if not monitoring.enabled:
            raise HTTPException(503, monitoring.storage_error or "Monitoring is starting.")
        monitoring.maintain()

    @router.post("/internal/rtmp/{event}")
    async def notification(event: str, request: Request):
        # ViewerAuth and Nginx both independently restrict this route.
        body = await request.body()
        if len(body) > 8192:
            return Response(status_code=200)
        try:
            fields = {key: values[0] for key, values in parse_qs(body.decode(), keep_blank_values=True).items()}
            from starlette.concurrency import run_in_threadpool
            await run_in_threadpool(monitoring.callback, event, fields)
        except (ValueError, UnicodeError):
            monitoring.dropped_events += 1
            logger.warning("Ignored malformed RTMP notification")
        # Notifications record observations; they never authorize broadcasting.
        return Response(status_code=200)

    @router.get("/api/monitoring/dashboard")
    def dashboard(_=Depends(ready)):
        try:
            return monitoring.dashboard()
        except sqlite3.Error:
            raise HTTPException(503, "Monitoring storage is unavailable.")

    @router.get("/api/monitoring/sessions")
    def sessions(selected=Depends(filters), limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0), _=Depends(ready)):
        return monitoring.store.sessions(selected, limit, offset)

    @router.get("/api/monitoring/events")
    def events(selected=Depends(filters), limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0), _=Depends(ready)):
        return monitoring.store.events(selected, limit, offset)

    @router.get("/api/monitoring/reports")
    def report(selected=Depends(filters), _=Depends(ready)):
        return monitoring.store.report(selected)

    @router.get("/api/monitoring/sessions.csv")
    def export(selected=Depends(filters), _=Depends(ready)):
        return StreamingResponse(session_csv(monitoring.store, selected, time.time()), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="stream-sessions.csv"'})

    app.include_router(router)
