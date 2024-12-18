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
import xml.etree.ElementTree as ET
import aiohttp

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
    bandwidth_in: float = 0.0
    bandwidth_out: float = 0.0
    bytes_in: float = 0.0
    bytes_out: float = 0.0
    quality: str = "unknown"

    def to_dict(self):
        return asdict(self)

class StreamManager:
    def __init__(self):
        self.streams: Dict[str, Stream] = {}
        self.connected_clients: List[WebSocket] = []
        self.stat_url = "http://localhost:8080/stat"

    async def fetch_rtmp_stats(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.stat_url) as response:
                    if response.status == 200:
                        xml_content = await response.text()
                        logger.debug(f"Received XML content: {xml_content[:200]}...")  # Log first 200 chars
                        root = ET.fromstring(xml_content)
                        # Verify we can find basic elements
                        servers = root.findall(".//server")
                        logger.debug(f"Found {len(servers)} server elements")
                        return root
                    else:
                        logger.error(f"Failed to fetch RTMP stats: HTTP {response.status}")
                        return None
        except Exception as e:
            logger.error(f"Error fetching RTMP stats: {e}", exc_info=True)
            return None

    async def monitor_streams(self):
        while True:
            try:
                stats = await self.fetch_rtmp_stats()
                if stats is not None:
                    self.process_rtmp_stats(stats)
                await self.broadcast_updates()
            except Exception as e:
                logger.error(f"Error in monitor_streams: {e}")
            await asyncio.sleep(1)

    def process_rtmp_stats(self, stats):
        current_streams = set()
        
        try:
            # Find all servers
            for server in stats.findall(".//server"):
                # Process each application in the server
                for app in server.findall(".//application"):
                    app_name = app.find("name")
                    if app_name is None or app_name.text != "live":
                        continue
                    
                    # Find live streams
                    live = app.find("live")
                    if live is None:
                        continue
                    
                    # Process each stream in the live section
                    for stream in live.findall("stream"):
                        name = stream.find("name")
                        if name is None or not name.text:
                            continue
                        
                        # Get the port from the server config
                        port = 1935  # Default RTMP port
                        port_elem = server.find("port")
                        if port_elem is not None and port_elem.text:
                            try:
                                port = int(port_elem.text)
                            except ValueError:
                                logger.warning(f"Invalid port value: {port_elem.text}")
                        
                        stream_id = f"{port}_{name.text}"
                        current_streams.add(stream_id)
                        
                        # Get bandwidth and bytes for monitoring purposes
                        bw_in = stream.find("bw_in")
                        bw_out = stream.find("bw_out")
                        bytes_in = stream.find("bytes_in")
                        bytes_out = stream.find("bytes_out")

                        bandwidth_in = float(bw_in.text) / 1_000_000 if bw_in is not None and bw_in.text else 0.0
                        bandwidth_out = float(bw_out.text) / 1_000_000 if bw_out is not None and bw_out.text else 0.0
                        bytes_in_val = float(bytes_in.text) / 1_000_000 if bytes_in is not None and bytes_in.text else 0.0  # Convert to MB
                        bytes_out_val = float(bytes_out.text) / 1_000_000 if bytes_out is not None and bytes_out.text else 0.0  # Convert to MB
                        
                        # Get video metadata
                        meta = stream.find("meta")
                        video = meta.find("video") if meta is not None else None
                        width = video.find("width") if video is not None else None
                        height = video.find("height") if video is not None else None
                        
                        # Determine quality based on video height
                        quality = "unknown"
                        if height is not None and height.text:
                            try:
                                h = int(height.text)
                                if h >= 1080:
                                    quality = "1080p"
                                elif h >= 720:
                                    quality = "720p"
                                elif h >= 480:
                                    quality = "480p"
                                else:
                                    quality = f"{h}p"
                            except ValueError:
                                logger.warning(f"Invalid height value: {height.text}")
                        
                        # Get clients count
                        nclients = stream.find("nclients")
                        active = nclients is not None and nclients.text and int(nclients.text) > 0
                        
                        if stream_id not in self.streams:
                            # New stream
                            self.streams[stream_id] = Stream(
                                id=stream_id,
                                name=name.text,
                                port=port,
                                start_time=time.time(),
                                bandwidth_in=bandwidth_in,
                                bandwidth_out=bandwidth_out,
                                bytes_in=bytes_in_val,
                                bytes_out=bytes_out_val,
                                status="active" if active else "inactive",
                                quality=quality
                            )
                            logger.info(f"New stream detected: {stream_id} ({quality})")
                        else:
                            # Update existing stream
                            self.streams[stream_id].bandwidth_in = bandwidth_in
                            self.streams[stream_id].bandwidth_out = bandwidth_out
                            self.streams[stream_id].bytes_in = bytes_in_val
                            self.streams[stream_id].bytes_out = bytes_out_val
                            self.streams[stream_id].status = "active" if active else "inactive"
                            self.streams[stream_id].quality = quality
                        
            # Remove ended streams
            ended_streams = set(self.streams.keys()) - current_streams
            for stream_id in ended_streams:
                logger.info(f"Stream ended: {stream_id}")
                self.streams.pop(stream_id)
            
        except Exception as e:
            logger.error(f"Error processing RTMP stats: {e}", exc_info=True)
            
        # Log current state
        if self.streams:
            logger.info(f"Active streams: {list(self.streams.keys())}")
        else:
            logger.debug("No active streams")

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

@app.get("/debug/rtmp-stats")
async def debug_rtmp_stats():
    """Debug endpoint to view raw RTMP stats"""
    stats = await stream_manager.fetch_rtmp_stats()
    if stats is not None:
        # Convert XML to dictionary for better visibility
        def xml_to_dict(element):
            result = {}
            for child in element:
                if len(child) > 0:
                    result[child.tag] = xml_to_dict(child)
                else:
                    result[child.tag] = child.text
            return result
        
        return xml_to_dict(stats)
    return {"error": "Could not fetch RTMP stats"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
