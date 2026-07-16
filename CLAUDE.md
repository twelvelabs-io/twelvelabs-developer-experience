# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

This is the **TwelveLabs Developer Experience** repository — a documentation and examples repo, not a compiled software package. It contains quickstart Jupyter notebooks, Jockey (agentic video understanding) guides and recipes, partner integration examples, the TwelveLabs REST API OpenAPI specification, and utility scripts.

There are no build systems, test suites, linters, or CI/CD pipelines. Notebooks are meant to run in Google Colab or any Jupyter environment.

## Repository Structure

```
api-spec/          OpenAPI 3.1.0 spec for the TwelveLabs REST API v1.3 (single YAML file)
quickstarts/       Google Colab-compatible notebooks for Search, Analyze, Segment, and Embeddings
quickstarts/jockey/  Jockey (agentic video understanding) notebooks — guides/ and recipes/
integrations/      Self-contained partner integration examples (Chroma, LanceDB, Langflow, Oracle, Poe, VideoDB, Weaviate)
examples/          Standalone example notebooks (e.g., Olympics_Video_Content_Search.ipynb)
scripts/           Utility scripts (e.g., multipart upload CLI)
```

## TwelveLabs API Essentials

**Base URL**: `https://api.twelvelabs.io/v1.3`
**Auth**: `x-api-key` header

### Core models
- **Marengo** (`marengo3.0`) — multimodal embedding/search model, 1024-dimensional vectors (legacy: `marengo2.7`)
- **Pegasus** (`pegasus1.5`) — video understanding model for analysis, summarization, segmentation, RAG
- **Jockey** (`jockey1.0`) — agentic video-understanding model behind the Jockey Responses API (see below)

> **Version note:** the quickstart notebooks track the latest models (`pegasus1.5`, `marengo3.0`). The committed OpenAPI spec still documents `pegasus1.2` and does **not** yet describe the Jockey `/responses` endpoint or the async segmentation (`time_based_metadata`) feature — the notebooks are ahead of the spec. Prefer the notebooks for current SDK usage; treat the spec as the reference for the documented v1.3 REST surface.

### Key API resources
- `/assets` (+ `/assets/multipart-uploads`) — upload videos (direct, URL, or multipart)
- `/indexes` + `/indexes/{id}/indexed-assets` — organize and index videos (required for search/embed)
- `/search` — text/image-to-video search
- `/analyze` (+ `/summarize`, `/gist`) — video text generation
- `/embed-v2` + `/embed-v2/tasks` — create embeddings (current generation, supersedes `/embed` and `/embed/tasks`)
- `/entity-collections` (+ `/entities`) — structured entities linked to videos
- `/tasks` — **legacy** bundled upload+index endpoint, being deprecated in favor of the `/assets` → `/indexed-assets` workflow
- `/responses` — **Jockey** agentic Responses API (not in the OpenAPI spec; see Jockey section)

### Standard SDK workflow

Two shapes depending on the capability:

**Search / Embed** — content must live in an index:

```python
from twelvelabs import TwelveLabs
client = TwelveLabs(api_key=TL_API_KEY)

index = client.indexes.create(
    index_name="my-index",
    models=[{"model_name": "marengo3.0", "model_options": ["visual", "audio"]}],
)
asset = client.assets.create(method="url", url="<VIDEO_URL>")
# Poll client.assets.retrieve(asset.id) until asset.status == "ready"
indexed_asset = client.indexes.indexed_assets.create(index_id=index.id, asset_id=asset.id)
# Poll client.indexes.indexed_assets.retrieve(index_id, indexed_asset_id) until status == "ready"
results = client.search.query(index_id=index.id, query_text="...", search_options=["visual", "audio"])
```

**Analyze / Segment** — operate directly on an asset, no index required:

```python
from twelvelabs.types import VideoContext_AssetId, AnalyzePromptV2

asset = client.assets.create(method="url", url="<VIDEO_URL>")
# Poll client.assets.retrieve(asset.id) until asset.status == "ready"
text = client.analyze(
    model_name="pegasus1.5",
    video=VideoContext_AssetId(asset_id=asset.id),
    prompt_v_2=AnalyzePromptV2(input_text="Summarize this video"),
)
```

Notable SDK surface used across quickstarts:
- `client.assets.create/retrieve` — assets are polled to `ready` before use
- `client.analyze` / `client.analyze_stream` — synchronous / streaming (SSE) analysis; pass `response_format=SyncResponseFormat(type="json_schema", json_schema=...)` for structured output
- `client.analyze_async.tasks.create/retrieve` — async analysis (used for segmentation)
- Typed request/response models live in `twelvelabs.types` (e.g., `VideoContext_AssetId`, `AnalyzePromptV2`, `SyncResponseFormat`, `AsyncResponseFormat`)

### Video segmentation

The Segment quickstart uses async analysis to break a video into structured, timestamped segments:

```python
from twelvelabs.types import AsyncResponseFormat, VideoContext_AssetId

task = client.analyze_async.tasks.create(
    video=VideoContext_AssetId(asset_id=asset.id),
    model_name="pegasus1.5",
    analysis_mode="time_based_metadata",
    response_format=AsyncResponseFormat(
        type="segment_definitions",
        segment_definitions=[{"id": "scenes", "description": "...", "fields": [...]}],
    ),
)
# Poll client.analyze_async.tasks.retrieve(task.task_id) until status == "ready"
data = json.loads(task.result.data)  # results are a JSON-encoded string
```

Supports up to 10 segment definitions per request, up to 20 typed custom fields per definition (string/boolean/number/integer/array), `enum` constraints, min/max segment durations, and reference images.

## Jockey (Agentic Video Understanding)

`quickstarts/jockey/` covers Jockey, TwelveLabs' agentic layer that answers natural-language questions over a collection of videos, grounded in specific clips. Jockey is accessed via the **Responses API** using raw REST (the notebooks use `requests`, not the SDK):

```python
requests.post(f"{BASE_URL}/responses", headers={"x-api-key": API_KEY}, json={
    "model": "jockey1.0",
    "input": [{"type": "message", "role": "user", "content": "What are the main themes?"}],
    "knowledge_store_id": STORE_ID,
})
```

Core concepts:
- **Knowledge store** — a persistent, queryable collection of videos plus derived understanding (spatiotemporal context, a typed ontology, and embeddings).
- **Session** — omit `session_id` on the first request; pass the returned id back to continue a multi-turn conversation.
- **Streaming** — set `"stream": True` in the body *and* `stream=True` on `requests.post()` to receive SSE events.
- **Structured output** — pass a JSON Schema via the `text` parameter to force typed output.
- **Instructions** — an `instructions` field acts as a system prompt to specialize behavior.

`quickstarts/jockey/guides/` (authentication, uploading_content, building_knowledge_stores, ingestion_config, querying, streaming, structured_output, multi_turn_sessions, error_handling) teach individual capabilities. `quickstarts/jockey/recipes/` (search_videos, extract_entities, enrich_content, get_corpus_overview, find_organization_axes, organize_video_library, assemble_highlight_reels, build_content_agent) are end-to-end task walkthroughs.

A Jockey MCP server ("Jockey Prod") is also available for agentic use outside these notebooks.

## Conventions for Adding Content

### Quickstart notebooks
- Follow existing structure: markdown prerequisites cell, install SDK, configure API key via Colab Secrets (`TL_API_KEY`), step-by-step procedure, "Next steps" links
- Include a Google Colab badge at the top (verify the badge path points to the notebook's real location)
- Poll assets and indexed assets to `ready` (handle the `failed` status) before using them
- Jockey notebooks read the key from the `TWELVELABS_API_KEY` environment variable and use raw `requests`

### Integration examples
- Create a subdirectory under `integrations/<PartnerName>/`
- Include a `README.md` with prerequisites, env var setup, and instructions
- Each integration must be self-contained (installs its own dependencies)
- Use environment variables for all secrets

## API Specification

`api-spec/openapi-1.3.yaml` (~8,400 lines) is the single source of truth for the documented REST API. Keep `info.version` in sync with the actual API version when editing. See the version note above — the notebooks may exercise newer models/endpoints than the spec currently documents.

## Multipart Upload Script

```bash
python scripts/multipart.py --file video.mp4 --api-key tlk_YOUR_KEY --filename "my-video.mp4"
```

Only requires `pip install requests`. Supports `--base-url`, `--type`, `--batch-size` flags.
