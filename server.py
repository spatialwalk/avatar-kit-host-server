"""
AvatarKit Host Mode Demo Server

Bridges the Python Server SDK with client demos via WebSocket.
Reads a pre-recorded PCM file, streams it through the Server SDK,
and forwards audio + animation frames to connected clients.

Usage:
    python server.py [--fast]

    --fast  Skip real-time pacing (send audio as fast as possible)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets
from dotenv import load_dotenv

from protocol import (
    encode_animation,
    encode_audio,
    encode_error,
    encode_metadata,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("demo-server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY = os.getenv("AVATARKIT_API_KEY", "")
APP_ID = os.getenv("AVATARKIT_APP_ID", "")
REGION = os.getenv("AVATARKIT_REGION", "ap-northeast")
DEFAULT_AVATAR_ID = os.getenv("AVATARKIT_AVATAR_ID", "")
EXPIRE_HOURS = int(os.getenv("AVATARKIT_EXPIRE_HOURS", "1"))

PCM_FILE_PATH = os.getenv("PCM_FILE_PATH", "audio/sample.pcm")
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
AUDIO_CHUNK_MS = int(os.getenv("AUDIO_CHUNK_MS", "100"))

WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("WS_PORT", "8765"))

ENDPOINTS = {
    "ap-northeast": {
        "console": "https://console.ap-northeast.spatialwalk.cloud/v1/console",
        "ingress": "wss://api.ap-northeast.spatialwalk.cloud/v2/driveningress",
    },
    "us-west": {
        "console": "https://console.us-west.spatialwalk.cloud/v1/console",
        "ingress": "wss://api.us-west.spatialwalk.cloud/v2/driveningress",
    },
}

# Bytes per chunk: sample_rate * 2 (16-bit) * 1 (mono) * chunk_ms / 1000
AUDIO_CHUNK_BYTES = SAMPLE_RATE * 2 * AUDIO_CHUNK_MS // 1000

FAST_MODE = "--fast" in sys.argv


# ---------------------------------------------------------------------------
# Session handler
# ---------------------------------------------------------------------------

async def handle_client(websocket):
    """Handle a single client WebSocket connection."""
    peer = websocket.remote_address
    logger.info("Client connected: %s", peer)

    try:
        # 1. Wait for start message
        raw = await asyncio.wait_for(websocket.recv(), timeout=30)
        request = json.loads(raw)
        if request.get("action") != "start":
            await websocket.send(encode_error("Expected {\"action\": \"start\"}"))
            return

        avatar_id = request.get("avatar_id") or DEFAULT_AVATAR_ID
        if not avatar_id:
            await websocket.send(encode_error("No avatar_id provided and no default configured"))
            return

        sample_rate = request.get("sample_rate", SAMPLE_RATE)
        logger.info("Starting session for avatar=%s sample_rate=%d", avatar_id, sample_rate)

        # 2. Validate config
        if not API_KEY or not APP_ID:
            await websocket.send(encode_error("Server not configured: missing API_KEY or APP_ID"))
            return

        if REGION not in ENDPOINTS:
            await websocket.send(encode_error(f"Unknown region: {REGION}"))
            return

        # 3. Load PCM file
        pcm_path = Path(PCM_FILE_PATH)
        if not pcm_path.exists():
            await websocket.send(encode_error(f"PCM file not found: {PCM_FILE_PATH}"))
            return
        pcm_data = pcm_path.read_bytes()
        logger.info("Loaded PCM: %d bytes (%.1fs)", len(pcm_data), len(pcm_data) / (sample_rate * 2))

        # 4. Create Server SDK session
        from avatarkit import new_avatar_session

        frame_queue: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue()
        session_error: list[str] = []
        session_closed = asyncio.Event()

        def on_frames(frame_data: bytes, is_last: bool):
            frame_queue.put_nowait((frame_data, is_last))

        def on_error(error):
            msg = str(error)
            logger.error("Server SDK error: %s", msg)
            session_error.append(msg)

        def on_close():
            logger.info("Server SDK session closed")
            session_closed.set()

        endpoints = ENDPOINTS[REGION]
        expire_at = datetime.now(timezone.utc) + timedelta(hours=EXPIRE_HOURS)

        session = new_avatar_session(
            avatar_id=avatar_id,
            api_key=API_KEY,
            app_id=APP_ID,
            console_endpoint_url=endpoints["console"],
            ingress_endpoint_url=endpoints["ingress"],
            expire_at=expire_at,
            sample_rate=sample_rate,
            transport_frames=on_frames,
            on_error=on_error,
            on_close=on_close,
        )

        await session.init()
        connection_id = await session.start()
        logger.info("Server SDK connected: connection_id=%s", connection_id)

        # 5. Send metadata to client
        await websocket.send(encode_metadata({
            "sample_rate": sample_rate,
            "channels": 1,
            "avatar_id": avatar_id,
            "connection_id": connection_id,
        }))

        # 6. Frame forwarding task
        frames_done = asyncio.Event()

        async def forward_frames():
            try:
                while True:
                    frame_data, is_last = await frame_queue.get()
                    await websocket.send(encode_animation([frame_data], is_last))
                    logger.debug("Forwarded animation frame (%d bytes, is_last=%s)", len(frame_data), is_last)
                    if is_last:
                        frames_done.set()
                        return
            except websockets.ConnectionClosed:
                logger.warning("Client disconnected during frame forwarding")

        forward_task = asyncio.create_task(forward_frames())

        # 7. Stream audio chunks to both client and Server SDK
        chunk_bytes = sample_rate * 2 * AUDIO_CHUNK_MS // 1000
        total_chunks = (len(pcm_data) + chunk_bytes - 1) // chunk_bytes

        for i in range(0, len(pcm_data), chunk_bytes):
            chunk = pcm_data[i : i + chunk_bytes]
            is_last = (i + chunk_bytes >= len(pcm_data))
            chunk_idx = i // chunk_bytes + 1

            # Send to client first (so it can call yieldAudioData before frames arrive)
            await websocket.send(encode_audio(chunk, is_last))
            # Send to Server SDK (triggers animation generation)
            await session.send_audio(chunk, end=is_last)

            if chunk_idx % 10 == 0 or is_last:
                logger.info("Audio chunk %d/%d sent (is_last=%s)", chunk_idx, total_chunks, is_last)

            if not is_last and not FAST_MODE:
                await asyncio.sleep(AUDIO_CHUNK_MS / 1000)

        # 8. Wait for all animation frames
        logger.info("Audio streaming complete, waiting for animation frames...")
        try:
            await asyncio.wait_for(frames_done.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for animation frames")
            await websocket.send(encode_error("Timeout waiting for animation frames"))

        # 9. Cleanup
        forward_task.cancel()
        await session.close()
        logger.info("Session complete for %s", peer)

    except websockets.ConnectionClosed:
        logger.info("Client disconnected: %s", peer)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON from client")
    except Exception as e:
        logger.exception("Unexpected error handling client %s", peer)
        try:
            await websocket.send(encode_error(str(e)))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    logger.info("AvatarKit Host Mode Demo Server")
    logger.info("Region: %s", REGION)
    logger.info("PCM file: %s", PCM_FILE_PATH)
    logger.info("Audio: %d Hz, %d ms/chunk (%d bytes/chunk)", SAMPLE_RATE, AUDIO_CHUNK_MS, AUDIO_CHUNK_BYTES)
    logger.info("Fast mode: %s", FAST_MODE)

    if not API_KEY:
        logger.warning("AVATARKIT_API_KEY not set — configure .env before connecting clients")

    async with websockets.serve(handle_client, WS_HOST, WS_PORT):
        logger.info("Listening on ws://%s:%d", WS_HOST, WS_PORT)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
