"""
AvatarKit Host Mode Demo Server

Bridges the Python Server SDK with client demos via WebSocket.
Client sends audio data, server feeds it to Server SDK for animation
generation, then returns audio + animation frames together.

Usage:
    python server.py
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import websockets
from dotenv import load_dotenv

from protocol import (
    MSG_AUDIO,
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

        # 3. Create Server SDK session
        from avatarkit import new_avatar_session

        frame_queue: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def on_frames(frame_data: bytes, is_last: bool):
            loop.call_soon_threadsafe(frame_queue.put_nowait, (frame_data, is_last))

        def on_error(error):
            logger.error("Server SDK error: %s", error)

        def on_close():
            logger.info("Server SDK session closed")

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

        # 4. Send ready to client — client can now send audio
        await websocket.send(encode_metadata({
            "sample_rate": sample_rate,
            "channels": 1,
            "avatar_id": avatar_id,
            "connection_id": connection_id,
        }))

        # 5. Forward frames to client as they arrive
        async def forward_frames():
            try:
                while True:
                    frame_data, is_last = await frame_queue.get()
                    await websocket.send(encode_animation([frame_data], is_last))
                    if is_last:
                        return
            except websockets.ConnectionClosed:
                pass

        forward_task = asyncio.create_task(forward_frames())

        # 6. Receive audio from client, forward to Server SDK
        while True:
            msg = await asyncio.wait_for(websocket.recv(), timeout=60)
            if isinstance(msg, str):
                continue
            data = msg if isinstance(msg, bytes) else bytes(msg)
            if len(data) < 2 or data[0] != MSG_AUDIO:
                continue
            is_last = bool(data[1] & 0x01)
            audio_chunk = data[2:]
            await session.send_audio(audio_chunk, end=is_last)
            if is_last:
                break

        # 7. Wait for remaining frames to be forwarded
        try:
            await asyncio.wait_for(forward_task, timeout=30)
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for animation frames")
            forward_task.cancel()

        # 8. Cleanup
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
    logger.info("Sample rate: %d Hz", SAMPLE_RATE)

    if not API_KEY:
        logger.warning("AVATARKIT_API_KEY not set — configure .env before connecting clients")

    async with websockets.serve(handle_client, WS_HOST, WS_PORT, max_size=10 * 1024 * 1024):
        logger.info("Listening on ws://%s:%d", WS_HOST, WS_PORT)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
