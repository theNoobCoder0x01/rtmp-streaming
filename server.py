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
import base64

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
            message = await websocket.receive_text()
            data = json.loads(message)
            
            if data['type'] == 'select_stream':
                stream_id = data['stream_id']
                # Start sending video data for the selected stream
                asyncio.create_task(send_video_stream(websocket, stream_id))
            elif data['type'] == 'request_status':
                # Handle status request
                await websocket.send_json({
                    "type": "status_update",
                    "streams": [stream.to_dict() for stream in stream_manager.streams.values()]
                })
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        stream_manager.connected_clients.remove(websocket)

async def send_video_stream(websocket: WebSocket, stream_id: str):
    try:
        stream = stream_manager.streams.get(stream_id)
        if not stream:
            logger.error(f"Stream {stream_id} not found")
            return

        logger.info(f"Starting video stream for {stream.name}")
        rtmp_url = f'rtmp://localhost:1935/live/{stream.name}'

        # Check if stream exists using ffprobe
        probe_process = await asyncio.create_subprocess_exec(
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'stream=codec_type',
            '-of', 'json',
            rtmp_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        probe_output, probe_error = await probe_process.communicate()
        if probe_process.returncode != 0:
            logger.error(f"RTMP stream not found: {probe_error.decode()}")
            return
        
        logger.info(f"RTMP stream found: {probe_output.decode()}")

        # Modified FFmpeg command for continuous streaming
        process = await asyncio.create_subprocess_exec(
            'ffmpeg',
            '-i', rtmp_url,
            '-c:v', 'libx264',
            '-profile:v', 'baseline',
            '-level', '3.0',
            '-preset', 'ultrafast',
            '-tune', 'zerolatency',
            '-c:a', 'aac',
            '-ar', '44100',
            '-b:a', '128k',
            '-f', 'mp4',
            '-movflags', 'frag_keyframe+empty_moov+omit_tfhd_offset+default_base_moof',
            '-frag_duration', '1000',
            '-min_frag_duration', '1000',
            '-flush_packets', '1',
            'pipe:1',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Create a task for handling stderr
        async def log_stderr():
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                logger.debug(f"FFmpeg: {line.decode().strip()}")

        stderr_task = asyncio.create_task(log_stderr())

        # Buffer for accumulating MP4 fragments
        buffer = bytearray()
        moof_start = b'moof'
        mdat_start = b'mdat'
        
        while True:
            # Read data in smaller chunks
            chunk = await process.stdout.read(4096)
            if not chunk:
                logger.warning("FFmpeg process ended")
                break

            buffer.extend(chunk)
            
            # Look for complete fragments (moof+mdat pairs)
            while True:
                # Find moof box
                moof_idx = buffer.find(moof_start)
                if moof_idx == -1:
                    break

                # Find corresponding mdat box
                mdat_idx = buffer.find(mdat_start, moof_idx)
                if mdat_idx == -1:
                    break

                # Find next moof or end of buffer to determine fragment end
                next_moof_idx = buffer.find(moof_start, mdat_idx)
                if next_moof_idx == -1:
                    # If no next moof, keep accumulating data
                    break

                # Extract complete fragment
                fragment = buffer[:next_moof_idx]
                buffer = buffer[next_moof_idx:]

                # Send the fragment
                try:
                    await websocket.send_json({
                        "type": "video-data",
                        "data": base64.b64encode(fragment).decode(),
                        "isInit": False
                    })
                    logger.debug(f"Sent fragment of size {len(fragment)}")
                except Exception as e:
                    logger.error(f"Error sending fragment: {e}")
                    return

            # Prevent buffer from growing too large
            if len(buffer) > 10485760:  # 10MB
                logger.warning("Buffer too large, resetting")
                buffer = bytearray()

            # Small delay to prevent overwhelming the connection
            await asyncio.sleep(0.001)

    except Exception as e:
        logger.error(f"Error streaming video: {e}", exc_info=True)
    finally:
        if 'process' in locals():
            process.terminate()
            await process.wait()
        if 'stderr_task' in locals():
            stderr_task.cancel()

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
