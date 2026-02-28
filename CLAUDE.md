# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

This is the **TwelveLabs Developer Experience** repository — a documentation and examples repo, not a compiled software package. It contains quickstart Jupyter notebooks, partner integration examples, the TwelveLabs REST API OpenAPI specification, and utility scripts.

There are no build systems, test suites, linters, or CI/CD pipelines.

## Repository Structure

```
api-spec/          OpenAPI 3.1.0 spec for the TwelveLabs REST API v1.3 (single YAML file)
quickstarts/       Google Colab-compatible notebooks for Search, Analyze, and Embeddings
integrations/      Self-contained partner integration examples (Chroma, LanceDB, Langflow, Oracle, Poe, VideoDB, Weaviate)
examples/          Standalone example notebooks
scripts/           Utility scripts (e.g., multipart upload CLI)
```

## TwelveLabs API Essentials

**Base URL**: `https://api.twelvelabs.io/v1.3`
**Auth**: `x-api-key` header

### Two core models
- **Marengo** (`marengo3.0`) — multimodal embedding/search model, 1024-dimensional vectors
- **Pegasus** (`pegasus1.2`) — video understanding/generation model for analysis, summarization, RAG

### Key API resources
- `/assets` — upload videos (direct or multipart)
- `/indexes` + `/indexes/{id}/indexed-assets` — organize and index videos
- `/search` — text/image-to-video search
- `/analyze`, `/summarize`, `/gist` — video text generation
- `/embed-v2/tasks` — create video embeddings (current generation, supersedes `/embed/tasks`)
- `/entity-collections` — structured entities linked to videos
- `/tasks` — **legacy** bundled upload+index endpoint, being deprecated

### Standard SDK workflow (all quickstarts follow this)

```python
from twelvelabs import TwelveLabs
client = TwelveLabs(api_key=TL_API_KEY)

asset = client.assets.create(method="url", url="<VIDEO_URL>")
index = client.indexes.create(
    index_name="my-index",
    models=[{"model_name": "marengo3.0", "model_options": ["visual", "audio"]}]
)
indexed_asset = client.indexes.indexed_assets.create(index_id=index.id, asset_id=asset.id)
# Poll until indexed_asset.status == "ready", then search/analyze/embed
```

## Conventions for Adding Content

### Quickstart notebooks
- Follow existing structure: markdown prerequisites cell, install SDK, configure API key via Colab Secrets (`TL_API_KEY`), step-by-step procedure, "Next steps" links
- Include a Google Colab badge at the top

### Integration examples
- Create a subdirectory under `integrations/<PartnerName>/`
- Include a `README.md` with prerequisites, env var setup, and instructions
- Each integration must be self-contained (installs its own dependencies)
- Use environment variables for all secrets

## API Specification

`api-spec/openapi-1.3.yaml` (~8,400 lines) is the single source of truth for the REST API. Keep `info.version` in sync with the actual API version when editing.

## Multipart Upload Script

```bash
python scripts/multipart.py --file video.mp4 --api-key tlk_YOUR_KEY --filename "my-video.mp4"
```

Only requires `pip install requests`. Supports `--base-url`, `--type`, `--batch-size` flags.
