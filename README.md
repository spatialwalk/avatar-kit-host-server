# AvatarKit Host Mode Demo Server

A Python WebSocket server that bridges the AvatarKit Server SDK with client demos (Web/iOS/Android).

Reads a pre-recorded PCM file, streams it through the Server SDK for animation generation, and forwards both audio and animation frames to connected clients in real-time.

## Architecture

```
Client Demo (Web/iOS/Android)
    │  WebSocket (binary)
    ▼
Demo Server (this)
    │  AvatarKit Python Server SDK
    ▼
SpatialReal Cloud (animation inference)
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env with your API key, App ID, Avatar ID from https://app.spatialreal.ai/

# 3. Run
python server.py

# Optional: fast mode (skip real-time pacing)
python server.py --fast
```

Server listens on `ws://0.0.0.0:8765` by default.

## Client Protocol

Clients connect via WebSocket and exchange binary messages.

### Client → Server

Send a JSON text message to start a session:

```json
{
  "action": "start",
  "avatar_id": "optional_avatar_id",
  "sample_rate": 16000
}
```

### Server → Client

Binary messages with 2-byte header:

```
[1 byte: type] [1 byte: flags] [payload...]
```

| Type | Name | Payload | Flags |
|------|------|---------|-------|
| `0x01` | Audio | Raw PCM bytes | bit0: is_last |
| `0x02` | Animation | Length-prefixed frames (see below) | bit0: is_last |
| `0x03` | Metadata | JSON UTF-8 | — |
| `0x04` | Error | JSON UTF-8 | — |

**Animation frame payload:**

```
[4 bytes: frame_count (uint32 BE)]
[4 bytes: frame1_length (uint32 BE)] [frame1_bytes...]
[4 bytes: frame2_length (uint32 BE)] [frame2_bytes...]
...
```

Each frame is a protobuf-encoded `Message` — pass directly to `yieldFramesData()`.

### Client Integration Pseudocode

```javascript
// Web example
const ws = new WebSocket('ws://localhost:8765')
ws.binaryType = 'arraybuffer'

ws.onopen = () => {
  ws.send(JSON.stringify({ action: 'start', avatar_id: 'your_avatar_id' }))
}

let conversationId = null

ws.onmessage = (event) => {
  const data = new Uint8Array(event.data)
  const type = data[0]
  const flags = data[1]
  const payload = data.slice(2)

  if (type === 0x01) {
    // Audio chunk
    const isLast = (flags & 0x01) !== 0
    conversationId = controller.yieldAudioData(payload, isLast)
  } else if (type === 0x02) {
    // Animation frames — parse length-prefixed array
    const frames = parseFrames(payload)
    if (conversationId) {
      controller.yieldFramesData(frames, conversationId)
    }
  } else if (type === 0x03) {
    // Metadata (JSON)
    const meta = JSON.parse(new TextDecoder().decode(payload))
    console.log('Session started:', meta)
  }
}
```

## Configuration

All settings via `.env` file (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AVATARKIT_API_KEY` | — | API key from SpatialReal Studio |
| `AVATARKIT_APP_ID` | — | App ID from SpatialReal Studio |
| `AVATARKIT_REGION` | `ap-northeast` | `ap-northeast` or `us-west` |
| `AVATARKIT_AVATAR_ID` | — | Default avatar ID |
| `PCM_FILE_PATH` | `audio/sample.pcm` | Pre-recorded PCM audio file |
| `SAMPLE_RATE` | `16000` | Audio sample rate (Hz) |
| `AUDIO_CHUNK_MS` | `100` | Audio chunk size (ms) |
| `WS_HOST` | `0.0.0.0` | Server bind address |
| `WS_PORT` | `8765` | Server port |

## Audio File

The included `audio/sample.pcm` is a 16kHz mono 16-bit PCM file (~20s).

To use your own audio:

```bash
ffmpeg -i input.wav -f s16le -ar 16000 -ac 1 audio/sample.pcm
```

## Going Further

For a full-featured Host Mode example with ASR + LLM + TTS integration, see [hostmode-demo](https://github.com/spatialwalk/hostmode-demo).
