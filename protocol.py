"""
AvatarKit Host Mode Demo Server — Binary WebSocket Protocol

Message format: [1 byte: type] [1 byte: flags] [payload...]

Types:
  0x01 AUDIO      — raw PCM bytes,        flags bit0 = is_last
  0x02 ANIMATION  — length-prefixed frames, flags bit0 = is_last
  0x03 METADATA   — JSON UTF-8
  0x04 ERROR      — JSON UTF-8

Animation payload:
  [4B frame_count uint32 BE] [4B frame1_len] [frame1_bytes] ...
"""

import json
import struct

MSG_AUDIO = 0x01
MSG_ANIMATION = 0x02
MSG_METADATA = 0x03
MSG_ERROR = 0x04


def encode_audio(chunk: bytes, is_last: bool) -> bytes:
    flags = 0x01 if is_last else 0x00
    return bytes([MSG_AUDIO, flags]) + chunk


def encode_animation(frames: list[bytes], is_last: bool) -> bytes:
    flags = 0x01 if is_last else 0x00
    header = bytes([MSG_ANIMATION, flags])
    parts = [header, struct.pack(">I", len(frames))]
    for frame in frames:
        parts.append(struct.pack(">I", len(frame)))
        parts.append(frame)
    return b"".join(parts)


def encode_metadata(data: dict) -> bytes:
    payload = json.dumps(data).encode("utf-8")
    return bytes([MSG_METADATA, 0x00]) + payload


def encode_error(message: str) -> bytes:
    payload = json.dumps({"error": message}).encode("utf-8")
    return bytes([MSG_ERROR, 0x00]) + payload


# --- Decoding (for client-side reference / testing) ---

def decode_message(data: bytes) -> dict:
    """Decode a binary WebSocket message. Returns dict with type and payload."""
    if len(data) < 2:
        raise ValueError("Message too short")

    msg_type = data[0]
    flags = data[1]
    payload = data[2:]

    if msg_type == MSG_AUDIO:
        return {
            "type": "audio",
            "is_last": bool(flags & 0x01),
            "data": payload,
        }
    elif msg_type == MSG_ANIMATION:
        frames = []
        offset = 0
        if len(payload) < 4:
            raise ValueError("Animation payload too short")
        frame_count = struct.unpack(">I", payload[offset : offset + 4])[0]
        offset += 4
        for _ in range(frame_count):
            if offset + 4 > len(payload):
                raise ValueError("Truncated animation frame")
            frame_len = struct.unpack(">I", payload[offset : offset + 4])[0]
            offset += 4
            frames.append(payload[offset : offset + frame_len])
            offset += frame_len
        return {
            "type": "animation",
            "is_last": bool(flags & 0x01),
            "frames": frames,
        }
    elif msg_type == MSG_METADATA:
        return {
            "type": "metadata",
            "data": json.loads(payload.decode("utf-8")),
        }
    elif msg_type == MSG_ERROR:
        return {
            "type": "error",
            "data": json.loads(payload.decode("utf-8")),
        }
    else:
        raise ValueError(f"Unknown message type: {msg_type:#x}")
