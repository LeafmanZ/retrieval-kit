# RetrievalKit

A self-contained RAG (Retrieval-Augmented Generation) backend powered by AWS Bedrock Knowledge Bases. It provides document upload, hybrid search, agentic multi-tool retrieval, and a chat interface — all exposed as a Flask API with SSE streaming.

## Architecture

```
                         ┌──────────────┐
                         │  Flask API   │
                         │   (app.py)   │
                         └──┬───┬───┬───┘
                            │   │   │
              ┌─────────────┘   │   └─────────────┐
              ▼                 ▼                  ▼
     ┌────────────────┐ ┌─────────────┐  ┌────────────────┐
     │  S3 (Originals)│ │ S3 (KB Src) │  │ Bedrock Runtime│
     │  raw uploads   │ │ PDF-ready   │  │  Claude LLM    │
     └────────────────┘ └──────┬──────┘  └────────────────┘
                               │
                        ┌──────▼──────┐
                        │   Bedrock   │
                        │ Knowledge   │
                        │    Base     │
                        └─────────────┘
```

**Upload flow:** file → LibreOffice conversion (if Office format) → write to KB source bucket → write original to originals bucket. If the original write fails, the KB source is rolled back.

**Auto-sync:** A background thread polls every 10s, compares the KB source bucket object count against the last ingestion's scanned count, and triggers a new ingestion job if they differ.

## What It Does

- **Document Management** — Upload files (PDF, DOCX, PPTX, images, audio, video, CSV, TXT), auto-converts Office formats to PDF via LibreOffice, stores originals and KB-ready copies in S3. 50 MB max upload size. Duplicate detection, retry conversion, and orphan cleanup included.
- **Hybrid Search** — Semantic + keyword search via Bedrock Knowledge Base with automatic ingestion sync.
- **Smart Search (Agentic)** — An LLM decides which retrieval tools to call and iterates up to 4 rounds until satisfied. Results stream via SSE. Supports `scoped_keys` filtering and `search_config` (score threshold, min/max results, search type).
- **Chat** — Multi-turn conversational RAG with tool-use orchestration and citation tracking.
- **Auto-Sync** — Background poller detects new S3 objects and triggers KB ingestion automatically.
- **Stats & Health** — Dashboard endpoint with document counts by type, KB sync status, and orphan detection.

## Supported File Formats

| Category | Extensions |
|----------|-----------|
| Documents | pdf, csv, txt |
| Images | png, jpg, jpeg, tiff, bmp, webp |
| Audio | mp3, wav, flac, ogg, amr |
| Video | mp4, webm, mkv, avi, mov |
| Office (→ PDF) | doc, docx, ppt, pptx, xls, xlsx, rtf, odt, odp, ods, html, htm |

Office formats are converted to PDF via LibreOffice before ingestion. The converted file is stored in the KB source bucket as `{stem}_{ext}.pdf` (e.g. `report_docx.pdf`).

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves the built-in UI |
| POST | `/upload` | Single file upload (`file` form field) |
| POST | `/api/upload` | Multi-file upload (`files` form field) |
| GET | `/api/documents` | List all documents with presigned view/download URLs and KB sync status |
| DELETE | `/api/documents/<key>` | Delete a document (original + KB source) |
| POST | `/api/documents/<key>/retry` | Re-convert and re-push a document to the KB source bucket |
| DELETE | `/api/orphans/<key>` | Delete an orphaned original (no matching KB source file) |
| GET | `/api/stats` | Document counts, type breakdown, latest ingestion details, orphan list |
| GET | `/api/ingestion/<job_id>` | Poll ingestion job status |
| POST | `/api/query` | Simple hybrid retrieval (non-agentic) |
| POST | `/api/smart_search` | Agentic multi-tool search (SSE stream) |
| POST | `/api/chat` | Conversational RAG (SSE stream) |

## API Usage Examples

### Upload

```bash
# Single file
curl -X POST -F "file=@report.pdf" http://localhost:5000/upload

# Multiple files
curl -X POST -F "files=@a.pdf" -F "files=@b.docx" http://localhost:5000/api/upload
```

Response:
```json
{"message": "Uploaded → s3://kb-bucket/report.pdf", "key": "report.pdf", "original_key": "report.pdf"}
```

### Simple Query

```bash
curl -X POST http://localhost:5000/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the refund policy?", "n_results": 5}'
```

Response:
```json
{
  "query": "What is the refund policy?",
  "sources": [
    {
      "text": "...",
      "score": 0.82,
      "uri": "s3://kb-bucket/policy.pdf",
      "content_type": "TEXT",
      "page": 3,
      "image_url": null,
      "description": "",
      "start_time_ms": null,
      "end_time_ms": null
    }
  ]
}
```

### Smart Search (SSE)

```bash
curl -X POST http://localhost:5000/api/smart_search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "find invoice 2024-0042",
    "scoped_keys": ["invoices_xlsx.pdf"],
    "search_config": {
      "score_threshold": 50,
      "min_results": 1,
      "max_results": 10,
      "search_type": "HYBRID"
    }
  }'
```

### Chat (SSE)

```bash
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Summarize the Q3 earnings report",
    "history": [
      {"role": "user", "content": "What documents do we have?"},
      {"role": "assistant", "content": "I found several documents including..."}
    ],
    "scoped_keys": ["q3_earnings_pdf.pdf"],
    "search_config": {"max_results": 5}
  }'
```

## Retrieval Tools

The agentic endpoints (`/api/smart_search`, `/api/chat`) use an LLM to route queries to these tools:

| Tool | Description | When Used |
|------|-------------|-----------|
| `semantic_search` | Hybrid search across all documents by meaning/topic | Questions, topics, general content searches |
| `exact_text_search` | Literal substring match (retrieves 25 chunks, filters locally) | Numbers, codes, IDs, special characters |
| `filename_search` | S3 key substring match + content pull for matched files | Finding files by name, listing documents |
| `search_within_document` | Semantic search filtered to a specific document's URI | Questions about a specific named document |

The LLM may call multiple tools per round and iterate up to 4 rounds. It stops early if good results are found and retries with different strategies if a tool returns 0 results.

## Content Type Handling

Bedrock KB returns different content structures depending on the source media:

| Content Type | Extracted Field | Notes |
|-------------|----------------|-------|
| `TEXT` / `PDF` | `content.text` | Standard text chunks with page numbers |
| `IMAGE` | `content.text` + presigned URL | Image description + viewable URL via `image_url` |
| `AUDIO` | `content.audio.transcription` | Transcribed text with start/end timestamps (ms) |
| `VIDEO` | `content.video.summary` | Video summary with start/end timestamps (ms) |

All results include a `source_url` presigned link back to the original document in the originals bucket.

## SSE Event Protocol

`/api/smart_search` and `/api/chat` stream Server-Sent Events:

| Event | Payload | Description |
|-------|---------|-------------|
| `status` | `{message, tool?, input?, error?}` | Progress updates (tool selection, execution, round checks) |
| `tool_result` | `{tool, input, result}` | Raw retrieval results per tool call |
| `done` | `{query, tool_results}` | Search complete — smart_search only |
| `answer` | `{answer, citations, tools_used}` | Final generated answer — chat only |

## Error Responses

| Code | Condition |
|------|-----------|
| 400 | Missing query/message, no file provided, invalid filename, empty file |
| 404 | Document not found (delete/retry) |
| 409 | Duplicate document (upload) |
| 413 | File exceeds 50 MB |
| 500 | Conversion failure, S3 error, Bedrock error |

## Prerequisites

- Python 3.10+
- AWS account with Bedrock access (Knowledge Base + Data Source already provisioned)
- LibreOffice (for Office → PDF conversion)
- Two S3 buckets: `retrieval-kit-source-documents` and `retrieval-kit-original-documents` (or prefixed variants — see `APP_PREFIX`)

## Setup

```bash
pip install -r requirements.txt

# Install LibreOffice (Ubuntu/Debian)
sudo apt install libreoffice-core libreoffice-writer
```

## Configuration (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `APP_PREFIX` | Optional prefix prepended to the hardcoded bucket names (`retrieval-kit-source-documents`, `retrieval-kit-original-documents`). Use when deploying multiple instances or in a different environment. | *(none)* |
| `REGION` | AWS region | *(required)* |
| `BEDROCK_MODEL_ID` | Bedrock model for LLM routing/chat | `amazon.nova-pro-v1:0` |
| `KNOWLEDGE_BASE_ID` | Bedrock Knowledge Base ID | *(required)* |
| `DATA_SOURCE_ID` | Bedrock Data Source ID | *(required)* |
| `AWS_PROFILE` | Named AWS CLI profile (for SSO or named profiles) | *(none — uses default)* |
| `ACCESS_KEY` | AWS access key ID (static credentials, testing only) | *(none)* |
| `SECRET_ACCESS_KEY` | AWS secret access key (static credentials, testing only) | *(none)* |

### Authentication

Credentials are resolved in this order:

1. **AWS SSO / named profile** — Set `AWS_PROFILE` to your SSO profile name. Run `aws sso login --profile <profile>` before starting the app.
2. **Default credential chain** — If neither `AWS_PROFILE` nor static keys are set, boto3 uses its [default chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html) (env vars `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `~/.aws/credentials`, instance profile, ECS task role, etc.).
3. **Static keys (testing only)** — Set `ACCESS_KEY` and `SECRET_ACCESS_KEY` in `.env`. Not recommended for production.

```env
# ── Required ──
REGION=us-east-1
KNOWLEDGE_BASE_ID=<your_kb_id>
DATA_SOURCE_ID=<your_ds_id>

# ── Optional: bucket prefix ──
# Buckets default to retrieval-kit-source-documents and retrieval-kit-original-documents.
# Set APP_PREFIX to prepend a custom prefix, e.g. APP_PREFIX=myapp- produces
# myapp-retrieval-kit-source-documents and myapp-retrieval-kit-original-documents.
# APP_PREFIX=myapp-

# ── Auth: pick ONE approach ──
# Option A: SSO / named profile
AWS_PROFILE=my-sso-profile

# Option B: Static keys (testing only)
# ACCESS_KEY=<access_key>
# SECRET_ACCESS_KEY=<secret_access_key>
```

## Run

```bash
python app.py
# → http://localhost:5000
```

## Project Structure

```
RetrievalKit/
├── app.py              # All backend logic
├── requirements.txt    # Python dependencies (boto3, flask, python-dotenv, werkzeug)
├── .env                # AWS credentials & config
├── templates/
│   └── documentation-page.html      # Built-in frontend UI
└── static/
    ├── css/            # Stylesheets
    ├── js/             # Frontend scripts
    ├── icons/          # App icons
    ├── images/         # Static images
    └── webfonts/       # Font files
```
