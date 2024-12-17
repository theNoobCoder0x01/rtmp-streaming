from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import os
import time
from typing import Dict, List
import aiofiles
import json
from dataclasses import dataclass, asdict
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@dataclass
class Stream:
    id: str
    name: str
    port: int
    start_time: float
    status: str = "active"
    bandwidth: float = 0.0
    quality: str = "unknown"

    def to_dict(self):
        return asdict(self)

class StreamManager:
    def __init__(self):
        self.streams: Dict[str, Stream] = {}
        self.connected_clients: List[WebSocket] = []
        self.hls_paths = [
            ("/tmp/hls", 1935, 8080),
            ("/tmp/hls1", 1936, 8081),
            ("/tmp/hls2", 1937, 8082),
            ("/tmp/hls3", 1938, 8083),
            ("/tmp/hls4", 1939, 8084),
            ("/tmp/hls5", 1940, 8085),
        ]

    async def monitor_streams(self):
        while True:
            try:
                for hls_path, rtmp_port, http_port in self.hls_paths:
                    await self.check_new_streams(hls_path, rtmp_port)
                await self.update_stream_stats()
                await self.broadcast_updates()
            except Exception as e:
                logger.error(f"Error in monitor_streams: {e}")
            await asyncio.sleep(1)

    async def check_new_streams(self, hls_path: str, rtmp_port: int):
        try:
            if os.path.exists(hls_path):
                files = os.listdir(hls_path)
                m3u8_files = [f for f in files if f.endswith('.m3u8')]
                
                # Check for new streams
                for m3u8_file in m3u8_files:
                    stream_name = m3u8_file.replace('.m3u8', '')
                    stream_id = f"{rtmp_port}_{stream_name}"
                    
                    if stream_id not in self.streams:
                        # New stream detected
                        stream = Stream(
                            id=stream_id,
                            name=stream_name,
                            port=rtmp_port,
                            start_time=time.time()
                        )
                        self.streams[stream_id] = stream
                        await self.notify_new_stream(stream)

                # Check for ended streams
                current_streams = set(f"{rtmp_port}_{f.replace('.m3u8', '')}" for f in m3u8_files)
                ended_streams = [
                    stream_id for stream_id in list(self.streams.keys())
                    if stream_id.startswith(str(rtmp_port)) and stream_id not in current_streams
                ]
                
                for stream_id in ended_streams:
                    await self.remove_stream(stream_id)

        except Exception as e:
            logger.error(f"Error checking streams in {hls_path}: {e}")

    async def update_stream_stats(self):
        for stream in self.streams.values():
            try:
                # Calculate bandwidth based on file sizes
                hls_path = f"/tmp/hls{'' if stream.port == 1935 else stream.port - 1935}"
                stream_files = [
                    f for f in os.listdir(hls_path)
                    if f.startswith(stream.name) and f.endswith('.ts')
                ]
                
                if stream_files:
                    latest_file = max(stream_files, key=lambda x: os.path.getmtime(f"{hls_path}/{x}"))
                    file_size = os.path.getsize(f"{hls_path}/{latest_file}")
                    # Convert to Mbps (assuming 3-second fragments)
                    stream.bandwidth = (file_size * 8) / (3 * 1_000_000)
                    
                    # Estimate quality based on bandwidth
                    if stream.bandwidth > 5:
                        stream.quality = "1080p"
                    elif stream.bandwidth > 2.5:
                        stream.quality = "720p"
                    else:
                        stream.quality = "480p"

            except Exception as e:
                logger.error(f"Error updating stats for stream {stream.id}: {e}")

    async def remove_stream(self, stream_id: str):
        if stream_id in self.streams:
            stream = self.streams.pop(stream_id)
            stream.status = "ended"
            await self.notify_stream_ended(stream)

    async def broadcast_updates(self):
        if not self.connected_clients:
            return

        update_data = {
            "type": "status_update",
            "streams": [stream.to_dict() for stream in self.streams.values()]
        }
        
        for client in self.connected_clients[:]:
            try:
                await client.send_json(update_data)
            except Exception:
                self.connected_clients.remove(client)

    async def notify_new_stream(self, stream: Stream):
        update_data = {
            "type": "new_stream",
            "stream": stream.to_dict()
        }
        await self.broadcast_json(update_data)

    async def notify_stream_ended(self, stream: Stream):
        update_data = {
            "type": "stream_ended",
            "streamId": stream.id
        }
        await self.broadcast_json(update_data)

    async def broadcast_json(self, data: dict):
        for client in self.connected_clients[:]:
            try:
                await client.send_json(data)
            except Exception:
                self.connected_clients.remove(client)

stream_manager = StreamManager()

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(stream_manager.monitor_streams())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    stream_manager.connected_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        stream_manager.connected_clients.remove(websocket)

@app.get("/api/streams")
async def get_streams():
    return {
        "streams": [stream.to_dict() for stream in stream_manager.streams.values()]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
