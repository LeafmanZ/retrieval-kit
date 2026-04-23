# RetrievalKit

A RAG (Retrieval-Augmented Generation) backend powered by AWS Bedrock Knowledge Bases. Provides document upload, hybrid search, agentic multi-tool retrieval, and conversational chat — exposed as a Flask Blueprint that can be mounted into any host app or run standalone.

## Architecture

```
                         ┌──────────────────┐
                         │   Host App or    │
                         │  Standalone Flask │
                         │                  │
                         │  ┌────────────┐  │
                         │  │ retrieval  │  │
                         │  │ _kit       │  │
                         │  │ Blueprint  │  │
                         │  └──┬───┬───┬─┘  │
                         └─────┤   │   ├────┘
                               │   │   │
                 ┌─────────────┘   │   └─────────────┐
                 ▼                 ▼                  ▼
        ┌────────────────┐ ┌─────────────┐  ┌────────────────┐
        │  S3 (Originals)│ │ S3 (KB Src) │  │ Bedrock Runtime│
        │  raw uploads   │ │ PDF-ready   │  │  Converse API  │
        └────────────────┘ └──────┬──────┘  └────────────────┘
                                  │
                           ┌──────▼──────┐
                           │   Bedrock   │
                           │ Knowledge   │
                           │    Base     │
                           └─────────────┘
```

**Two modes:**
- **Standalone** — `python run.py` starts a Flask app on port 5000 with no auth. Config loaded from `.env`.
- **Host integration** — `pip install retrieval-kit`, call `create_blueprint(config)`, register the returned Blueprint on your Flask app. The host injects its own boto3 clients, auth decorator, and config. No separate process.

## What It Does

- **Document Management** — Upload files (PDF, DOCX, PPTX, images, audio, video, CSV, TXT), auto-converts Office formats to PDF via LibreOffice, stores originals and KB-ready copies in S3. 50 MB max. Duplicate detection, retry conversion, orphan cleanup.
- **Hybrid Search** — Semantic + keyword search via Bedrock Knowledge Base with automatic ingestion sync.
- **Smart Search (Agentic)** — LLM decides which retrieval tools to call, iterates up to 4 rounds. Results stream via SSE. Supports `scoped_keys` filtering and `search_config`.
- **Chat** — Multi-turn conversational RAG with tool-use orchestration and citation tracking.
- **Auto-Sync** — Background poller detects new S3 objects and triggers KB ingestion automatically.
- **Stats & Health** — Document counts by type, KB sync status, orphan detection.

## Installation

**From Git (recommended):**
```bash
pip install git+https://github.com/LeafmanZ/retrieval-kit.git@v1.0.0
```

**From local path (development):**
```bash
pip install -e /path/to/retrieval-kit
```

## Standalone Usage

```bash
cp .env.example .env
# Edit .env with your AWS config
python run.py
# → http://localhost:5000
```

## Host App Integration

```python
from retrieval_kit import create_blueprint, get_attributes

# Build config with your own boto3 clients
config = {
    "s3_client": your_s3_client,
    "bedrock_agent": your_bedrock_agent_client,
    "bedrock_agent_runtime": your_bedrock_agent_runtime_client,
    "bedrock_runtime": your_bedrock_runtime_client,
    "app_prefix": "myapp-",                    # → myapp-retrieval-kit-source-documents
    "knowledge_base_id": "YOUR_KB_ID",
    "data_source_id": "YOUR_DS_ID",
    "model_id": "amazon.nova-pro-v1:0",
    "api_base": "/docs",                       # JS fetch prefix (empty for standalone)
    "enable_sync_poller": True,
    "auth_decorator": your_auth_decorator,     # callable(attribute) → decorator
    "route_auth_map": {                        # route → attribute for auth enforcement
        "/api/documents": "documentation:view",
        "/api/upload": "documentation:upload",
        "/api/chat": "documentation:chat",
        # ...
    },
}

bp = create_blueprint(config)
app.register_blueprint(bp, url_prefix="/docs")

# Merge retrieval-kit's RBAC attributes into your permission system
for attr in get_attributes():
    register_attribute(attr["attribute_name"], attr["description"])
```

### Config Dict Contract

| Key | Type | Description | Default |
|-----|------|-------------|---------|
| `s3_client` | boto3 client | S3 client | *(required)* |
| `bedrock_agent` | boto3 client | bedrock-agent client | *(required)* |
| `bedrock_agent_runtime` | boto3 client | bedrock-agent-runtime client | *(required)* |
| `bedrock_runtime` | boto3 client | bedrock-runtime client | *(required)* |
| `app_prefix` | str | Bucket name prefix | `""` |
| `knowledge_base_id` | str | Bedrock Knowledge Base ID | *(required)* |
| `data_source_id` | str | Bedrock Data Source ID | *(required)* |
| `model_id` | str | Bedrock model ID | `"amazon.nova-pro-v1:0"` |
| `api_base` | str | URL prefix for JS fetch calls | `""` |
| `enable_sync_poller` | bool | Start background KB sync thread | `True` |
| `auth_decorator` | callable | `auth_decorator(attribute)` → decorator | no-op |
| `route_auth_map` | dict | `{route_rule: attribute_name}` | `{}` |

### Bucket Naming

Bucket names are derived from `app_prefix`:
- `{app_prefix}retrieval-kit-source-documents` — KB-ready files (PDF-converted)
- `{app_prefix}retrieval-kit-original-documents` — Raw uploaded originals

| `app_prefix` | Source bucket | Originals bucket |
|---|---|---|
| `""` (standalone) | `retrieval-kit-source-documents` | `retrieval-kit-original-documents` |
| `"ceta-central-"` | `ceta-central-retrieval-kit-source-documents` | `ceta-central-retrieval-kit-original-documents` |

## RBAC Attributes

Retrieval-kit ships its own attribute definitions in `attributes.csv`. Host apps can load them via `get_attributes()` and merge into their permission system.

| Attribute | Description |
|---|---|
| `documentation:view` | View document list and download documents |
| `documentation:upload` | Upload new documents |
| `documentation:delete` | Delete documents |
| `documentation:chat` | Use AI chat for document Q&A |
| `documentation:search` | Use smart search across documents |
| `documentation:retry` | Retry failed document conversions |
| `documentation:manage-orphans` | Delete orphaned documents from KB |
| `documentation:view-stats` | View document stats and sync status |
| `documentation:view-ingestion` | Check knowledge base ingestion job status |

Page-level visibility (e.g. `page-view:documentation`) is **not** included — that's the host app's responsibility.

## API Endpoints

| Method | Path | Auth Attribute | Description |
|--------|------|----------------|-------------|
| GET | `/` | *(page-level)* | Built-in UI |
| POST | `/upload` | `documentation:upload` | Single file upload |
| POST | `/api/upload` | `documentation:upload` | Multi-file upload |
| GET | `/api/documents` | `documentation:view` | List documents with presigned URLs |
| DELETE | `/api/documents/<key>` | `documentation:delete` | Delete a document |
| POST | `/api/documents/<key>/retry` | `documentation:retry` | Re-convert and re-push to KB |
| DELETE | `/api/orphans/<key>` | `documentation:manage-orphans` | Delete orphaned original |
| GET | `/api/stats` | `documentation:view-stats` | Counts, sync status, orphans |
| GET | `/api/ingestion/<job_id>` | `documentation:view-ingestion` | Ingestion job status |
| POST | `/api/query` | `documentation:search` | Simple hybrid retrieval |
| POST | `/api/smart_search` | `documentation:search` | Agentic multi-tool search (SSE) |
| POST | `/api/chat` | `documentation:chat` | Conversational RAG (SSE) |

## Supported File Formats

| Category | Extensions |
|----------|-----------|
| Documents | pdf, csv, txt |
| Images | png, jpg, jpeg, tiff, bmp, webp |
| Audio | mp3, wav, flac, ogg, amr |
| Video | mp4, webm, mkv, avi, mov |
| Office (→ PDF) | doc, docx, ppt, pptx, xls, xlsx, rtf, odt, odp, ods, html, htm |

## Retrieval Tools

The agentic endpoints (`/api/smart_search`, `/api/chat`) use an LLM to route queries:

| Tool | When Used |
|------|-----------|
| `semantic_search` | Questions, topics, general content searches |
| `exact_text_search` | Numbers, codes, IDs, special characters |
| `filename_search` | Finding files by name |
| `search_within_document` | Questions about a specific named document |

The LLM may call multiple tools per round and iterate up to 4 rounds.

## SSE Event Protocol

`/api/smart_search` and `/api/chat` stream Server-Sent Events:

| Event | Payload | Description |
|-------|---------|-------------|
| `status` | `{message, tool?, input?, error?}` | Progress updates |
| `tool_result` | `{tool, input, result}` | Raw retrieval results per tool call |
| `done` | `{query, tool_results}` | Search complete (smart_search only) |
| `answer` | `{answer, citations, tools_used}` | Final answer (chat only) |

## Configuration (.env) — Standalone Mode

```env
# Required
REGION=us-gov-west-1
KNOWLEDGE_BASE_ID=<your_kb_id>
DATA_SOURCE_ID=<your_ds_id>

# Optional
BEDROCK_MODEL_ID=amazon.nova-pro-v1:0
APP_PREFIX=

# Auth (pick one)
AWS_PROFILE=my-sso-profile
# ACCESS_KEY=<access_key>
# SECRET_ACCESS_KEY=<secret_access_key>
```

## Prerequisites

- Python 3.10+
- AWS account with Bedrock Knowledge Base + Data Source provisioned
- LibreOffice (for Office → PDF conversion): `sudo apt install libreoffice-core libreoffice-writer`
- Two S3 buckets (see Bucket Naming above)

## IAM Permissions Required

```json
{
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:*",
            "Resource": [
                "arn:aws:s3:::{prefix}retrieval-kit-source-documents",
                "arn:aws:s3:::{prefix}retrieval-kit-source-documents/*",
                "arn:aws:s3:::{prefix}retrieval-kit-original-documents",
                "arn:aws:s3:::{prefix}retrieval-kit-original-documents/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "bedrock-agent:StartIngestionJob",
                "bedrock-agent:ListIngestionJobs",
                "bedrock-agent:GetIngestionJob",
                "bedrock-agent-runtime:Retrieve",
                "bedrock-runtime:Converse"
            ],
            "Resource": "*"
        }
    ]
}
```

## Project Structure

```
retrieval-kit/
├── pyproject.toml                    # Package metadata + deps
├── run.py                            # Standalone entry point
├── .env.example                      # Example config
├── README.md
└── src/
    └── retrieval_kit/
        ├── __init__.py               # Exports: create_blueprint, create_standalone_app, get_attributes
        ├── core.py                   # Blueprint factory, routes, search tools, helpers
        ├── attributes.csv            # RBAC attribute definitions shipped with package
        ├── templates/
        │   └── documentation-page.html
        └── static/
            ├── css/
            ├── js/
            ├── icons/
            ├── images/
            └── webfonts/
```

## Error Responses

| Code | Condition |
|------|-----------|
| 400 | Missing query/message, no file, invalid filename, empty file |
| 404 | Document not found |
| 409 | Duplicate document |
| 413 | File exceeds 50 MB |
| 500 | Conversion failure, S3 error, Bedrock error |

## Versioning

Host apps pin to a specific tag:
```
retrieval-kit @ git+https://github.com/LeafmanZ/retrieval-kit.git@v1.0.0
```

To upgrade: update the version pin and `pip install`. No host code changes needed unless the config contract changed (major version bump).