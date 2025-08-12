# VideoDB + TwelveLabs Integration

Unlock real-time video understanding with VideoDB’s RTStream and TwelveLabs’ Pegasus 1.2 model — from live camera feeds to instant, actionable alerts.

- Safety and Security: proactive alerts during emergencies (e.g., flash floods, intruders)
- Home + Consumer: intelligent monitoring (e.g., baby crib monitoring)
- Enterprise: searchable, indexed video knowledge for workflows and collaboration

This folder contains two end-to-end sample notebooks:  
- Flash Flood Detection: `Flash_Flood_Detection_with_VideoDB_and_TwelveLabs.ipynb`  
- Baby Crib Monitoring: `Baby_Crib_Monitoring_with_VideoDB_and_TwelveLabs.ipynb`

Both examples use VideoDB’s real-time infrastructure and TwelveLabs’ Pegasus model, natively integrated within VideoDB — just pass `model_name="twelvelabs-pegasus-1.2"`.

---

## Why this integration

Human monitoring of live video is expensive, exhausting, and doesn’t scale. With VideoDB + TwelveLabs:  
- Consolidate ingestion, indexing, and alerting behind one API  
- Avoid GPU management and scaling pitfalls  
- Hit real-time needs with low complexity and straightforward webhooks  

Reference: Notion — “Unlock Real-Time Video Understanding with VideoDB and TwelveLabs”.

---

## Architecture at a glance

1. RTStream ingest (RTSP or similar)  
2. Scene indexing with AI descriptions (Pegasus 1.2)  
3. Event definitions (prompt-driven)  
4. Real-time alerts → webhook callbacks (includes confidence, explanation, stream_url)

```mermaid
flowchart LR
  Cam[IP Camera / RTSP] --> VDB[VideoDB RTStream]
  VDB --> IDX[Scene Indexing (Pegasus 1.2)]
  IDX --> EVT[Event Definition]
  EVT --> ALT[Alert Engine]
  ALT --> WH[Webhook Callback]
```

---

## Prerequisites

- Python 3.9+  
- `pip install videodb`  
- VideoDB API key available as an environment variable or via input prompt  

No additional TwelveLabs account or API key is required inside VideoDB — selecting `model_name="twelvelabs-pegasus-1.2"` enables Pegasus.

---

## Quick start (pattern used in both notebooks)

```python
import videodb
from videodb import SceneExtractionType

conn = videodb.connect()
coll = conn.get_collection()

# 1) Connect a live stream
rtsp_url = "rtsp://samples.rts.videodb.io:8554/..."
rtstream = coll.connect_rtstream(name="My Stream", url=rtsp_url)

# 2) Create a scene index using TwelveLabs Pegasus
scene_index = rtstream.index_scenes(
    extraction_type=SceneExtractionType.time_based,
    extraction_config={"time": 10, "frame_count": 6},
    prompt="Describe what matters for my use case...",
    model_name="twelvelabs-pegasus-1.2",
    name="My_Index"
)

# 3) Define an event and attach a webhook alert
event_id = conn.create_event(event_prompt="Detect X", label="x_event")
alert_id = scene_index.create_alert(event_id, callback_url="https://your-webhook")
```

---

## Sample 1 — Flash Flood Detection (Arizona riverbed)

**Goal:** detect sudden water surges across dry land and send immediate alerts.

Highlights:  
- Stream: `rtsp://samples.rts.videodb.io:8554/floods`  
- Index prompt: monitor riverbed; declare flash flood on moving water across land  
- Event label: `flash_flood`  
- Alert payload contains: `label`, `confidence`, `explanation`, `stream_url`

Open the notebook: `Flash_Flood_Detection_with_VideoDB_and_TwelveLabs.ipynb`

---

## Sample 2 — Baby Crib Monitoring

**Goal:** detect baby standing, attempting to climb out, or escaping; alert caregivers.

Highlights:  
- Stream: `rtsp://samples.rts.videodb.io:8554/crib`  
- Index prompt: watch baby behavior (standing, climbing, escaping)  
- Event label: `baby_escape`  
- Alert payload contains: `label`, `confidence`, `explanation`, `stream_url`

Open the notebook: `Baby_Crib_Monitoring_with_VideoDB_and_TwelveLabs.ipynb`

---

## Webhook alert format (example)

```json
{
  "event_id": "event-...",
  "label": "flash_flood",
  "confidence": 0.95,
  "explanation": "...why this matched...",
  "timestamp": "2025-05-29T01:51:33.907Z",
  "start_time": "2025-05-29T01:50:34.289Z",
  "end_time": "2025-05-29T01:50:39.891Z",
  "stream_url": "https://rt.stream.videodb.io/manifests/.../1748483434000000-1748483440000000.m3u8"
}
```

Tip: the `stream_url` is a short-lived HLS manifest you can embed in dashboards or link in notifications for instant review.

---

## Operational notes

- **Latency:** end-to-end depends on stream sampling, indexing window (e.g., 10 s), and webhook delivery. For near-real-time, prefer shorter time windows and smaller frame counts.  
- **Costs:** processing cost scales with video duration and sampling settings.  
- **Reliability:** treat webhooks as at-least-once delivery; dedupe via `event_id` on your side.  
- **Security:** do not hardcode secrets in notebooks; use environment variables or secret managers. Webhooks should be HTTPS and may include verification tokens.

---

## Troubleshooting

- **No scenes returned:** ensure the index is `running` and the stream is `connected`.  
- **No alerts received:** verify `callback_url` is reachable from the internet and returns 2xx.  
- **Prompt misses events:** tighten instructions in the `prompt`, shorten windows, or increase `frame_count`.  
- **Player not loading:** confirm the `stream_url` is within its validity window and your network allows HLS playback.

---

## Learn more

- VideoDB docs: <https://docs.videodb.io/>  
- TwelveLabs docs: <https://docs.twelvelabs.io/docs/get-started/introduction>  
- TwelveLabs developer hub: <https://www.twelvelabs.io/developer-hub>

If you build something cool with this integration, share it with us on Discord: <https://discord.gg/mwHQKFv7En>
