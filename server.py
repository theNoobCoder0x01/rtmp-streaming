from dataclasses import dataclass, asdict
import asyncio
import json
import logging
import os
import sqlite3
import time
from typing import Dict, List
import xml.etree.ElementTree as ET

import aiohttp
from fastapi import FastAPI, WebSocket
from starlette.concurrency import run_in_threadpool

from monitoring.api import install_routes
from monitoring.service import MonitoringService, parse_stats
from viewer_auth import COOKIE_NAME, ViewerAuth


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
monitoring = MonitoringService(os.environ.get('MONITOR_DB_PATH', '/var/lib/stream-monitor/monitoring.sqlite3'))
viewer_auth = ViewerAuth()
viewer_auth.on_session_end = monitoring.end_browser


async def observe_hls(request, file_path, session):
    if request.client:
        try:
            await run_in_threadpool(monitoring.hls_request, request.cookies.get(COOKIE_NAME),
                                    session.username, request.client.host, file_path)
        except (ValueError, OSError):
            logger.warning('Unable to record browser viewing observation', exc_info=True)


viewer_auth.on_hls_request = observe_hls


@dataclass
class Stream:
    id: str
    name: str
    port: int
    start_time: float
    status: str = 'active'
    bandwidth_in: float = 0.0
    bandwidth_out: float = 0.0
    bytes_in: float = 0.0
    bytes_out: float = 0.0
    quality: str = 'unknown'

    def to_dict(self):
        return asdict(self)


class StreamManager:
    def __init__(self):
        self.streams: Dict[str, Stream] = {}
        self.connected_clients: List[WebSocket] = []
        self.stat_url = 'http://127.0.0.1:8090/stat'

    async def fetch_rtmp_stats(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(self.stat_url) as response:
                    if response.status != 200:
                        raise ValueError('Nginx statistics returned HTTP {}'.format(response.status))
                    return ET.fromstring(await response.text())
        except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError, ValueError):
            logger.warning('Unable to fetch Nginx statistics', exc_info=True)
            return None

    def process_rtmp_stats(self, root):
        snapshot = parse_stats(root)
        current = {}
        for item in snapshot['streams']:
            if item['application'] != 'live':
                continue
            previous = self.streams.get(item['id'])
            start = previous.start_time if previous and previous.status == item['status'] else time.time()
            current[item['id']] = Stream(
                id=item['id'], name=item['name'], port=item['port'], start_time=start,
                status=item['status'], bandwidth_in=item['bandwidth_in'], bandwidth_out=item['bandwidth_out'],
                bytes_in=item['bytes_in'], bytes_out=item['bytes_out'], quality=item['quality'])
        self.streams = current
        return snapshot

    async def monitor_streams(self):
        while True:
            try:
                observed_at = time.time()
                root = await self.fetch_rtmp_stats()
                if root is None:
                    monitoring.failed_poll()
                else:
                    snapshot = self.process_rtmp_stats(root)
                    await run_in_threadpool(monitoring.observe, snapshot, now=observed_at)
                await run_in_threadpool(monitoring.maintain)
                await self.broadcast_updates()
            except Exception:
                monitoring.failed_poll()
                logger.exception('Monitoring iteration failed; retaining last-known state')
            await asyncio.sleep(2)

    def update_data(self):
        return {'type': 'status_update', 'streams': [stream.to_dict() for stream in self.streams.values()]}

    async def broadcast_updates(self):
        for client in self.connected_clients[:]:
            try:
                if not viewer_auth.is_authenticated(client.cookies.get(COOKIE_NAME)):
                    await client.close(code=1008)
                    if client in self.connected_clients:
                        self.connected_clients.remove(client)
                    continue
                await client.send_json(self.update_data())
            except Exception:
                if client in self.connected_clients:
                    self.connected_clients.remove(client)


stream_manager = StreamManager()
monitor_task = None


@app.on_event('startup')
async def startup_event():
    global monitor_task
    try:
        await run_in_threadpool(monitoring.initialize)
    except (sqlite3.Error, OSError, RuntimeError):
        monitoring.storage_error = 'Monitoring storage could not be initialized. Check the server logs.'
        logger.exception('Monitoring storage initialization failed; video remains available')
    monitor_task = asyncio.create_task(stream_manager.monitor_streams())


@app.on_event('shutdown')
async def shutdown_event():
    if monitor_task:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)


@app.websocket('/ws')
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get(COOKIE_NAME)
    origin = websocket.headers.get('origin')
    scheme = 'https' if websocket.url.scheme == 'wss' else 'http'
    if (not viewer_auth.is_authenticated(token)
            or (origin and origin != '{}://{}'.format(scheme, websocket.headers['host']))):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    stream_manager.connected_clients.append(websocket)
    try:
        while viewer_auth.is_authenticated(token):
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=1)
                try:
                    if json.loads(message).get('type') == 'request_status':
                        await websocket.send_json(stream_manager.update_data())
                except (ValueError, AttributeError):
                    pass
            except asyncio.TimeoutError:
                pass
        await websocket.close(code=1008)
    except Exception:
        pass
    finally:
        if websocket in stream_manager.connected_clients:
            stream_manager.connected_clients.remove(websocket)


@app.get('/api/streams')
async def get_streams():
    return {'streams': [stream.to_dict() for stream in stream_manager.streams.values()]}


@app.get('/debug/rtmp-stats')
async def debug_rtmp_stats():
    stats = await stream_manager.fetch_rtmp_stats()
    if stats is None:
        return {'error': 'Could not fetch RTMP stats'}
    return {'xml': ET.tostring(stats, encoding='unicode')}


install_routes(app, monitoring)
viewer_auth.install(app)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000)
