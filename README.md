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
- **Audit Trail** — Every upload, delete, retry, and orphan removal is logged to S3 as JSON with user identity and IP address. Uploader metadata is stamped on S3 objects and shown in the UI.

## Installation

```bash
# From Git (recommended)
pip install git+https://github.com/LeafmanZ/retrieval-kit.git@v1.3.0

# From local path (development)
pip install -e /path/to/retrieval-kit
```

## Standalone Usage

See the deployment guide below for running locally, in Docker, or on ECS.

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
| `get_current_user` | callable | Returns `{id, email, display_name, role}` for audit | anonymous default |

### Bucket Naming

Bucket names are derived from `app_prefix`:
- `{app_prefix}retrieval-kit-source-documents` — KB-ready files (PDF-converted)
- `{app_prefix}retrieval-kit-original-documents` — Raw uploaded originals
- `{app_prefix}retrieval-kit-audit-logs` — Audit trail (JSON per event, date-partitioned)

| `app_prefix` | Source bucket | Originals bucket | Audit bucket |
|---|---|---|---|
| `""` (standalone) | `retrieval-kit-source-documents` | `retrieval-kit-original-documents` | `retrieval-kit-audit-logs` |
| `"ceta-central-"` | `ceta-central-retrieval-kit-source-documents` | `ceta-central-retrieval-kit-original-documents` | `ceta-central-retrieval-kit-audit-logs` |

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
| `documentation:view-audit` | View audit log of document actions |

`documentation:admin-users` is an internal attribute used only in standalone mode for user/role administration. It is not exported via `get_attributes()` and does not affect host apps.

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
| GET | `/api/audit` | `documentation:view-audit` | List audit logs (filterable) |
| GET | `/api/audit/document/<key>` | `documentation:view-audit` | Audit history for one document |
| POST | `/api/auth/login` | *(standalone only)* | Login |
| POST | `/api/auth/logout` | *(standalone only)* | Logout |
| GET | `/api/auth/me` | *(standalone only)* | Current user info |
| POST | `/api/auth/change-password` | *(standalone only)* | Change own password |
| GET | `/api/admin/users` | *(standalone only)* | List users (admin only) |
| POST | `/api/admin/users` | *(standalone only)* | Create user (admin only) |
| PATCH | `/api/admin/users/<id>` | *(standalone only)* | Update user role/status (admin only) |
| DELETE | `/api/admin/users/<id>` | *(standalone only)* | Deactivate user (admin only) |
| DELETE | `/api/admin/users/<id>/delete` | *(standalone only)* | Permanently delete user (admin only) |
| POST | `/api/admin/users/<id>/reset-password` | *(standalone only)* | Reset user password (admin only) |
| GET | `/api/admin/roles` | *(standalone only)* | List roles + available attributes |
| POST | `/api/admin/roles` | *(standalone only)* | Create role |
| PATCH | `/api/admin/roles/<name>` | *(standalone only)* | Update role attributes |
| DELETE | `/api/admin/roles/<name>` | *(standalone only)* | Delete role |

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

## Environment Variables

All configuration is done through environment variables. Locally these come from a `.env` file. In Docker/ECS they are injected via `-e` flags, task definition `environment`, or Secrets Manager.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REGION` | **yes** | — | AWS region (e.g. `us-gov-west-1`) |
| `KNOWLEDGE_BASE_ID` | **yes** | — | Bedrock Knowledge Base ID |
| `DATA_SOURCE_ID` | **yes** | — | Bedrock Data Source ID |
| `BEDROCK_MODEL_ID` | no | `amazon.nova-pro-v1:0` | Bedrock model for search/chat |
| `APP_PREFIX` | no | `""` | Prefix for S3 bucket names |
| `AWS_PROFILE` | no | — | Named AWS SSO profile (local dev only) |
| `ACCESS_KEY` | no | — | Static AWS access key (not recommended) |
| `SECRET_ACCESS_KEY` | no | — | Static AWS secret key (not recommended) |
| `ADMIN_EMAIL` | no | — | Root admin email, seeded on first run |
| `ADMIN_PASSWORD` | no | — | Root admin password (**use Secrets Manager in ECS**) |
| `REGISTER_EMAIL` | no | — | Contact email shown on login page |
| `SECRET_KEY` | no | auto-generated | Flask session secret (**set explicitly in ECS**) |

**AWS credential priority:**
1. ECS task role (automatic in ECS — no config needed)
2. `AWS_PROFILE` (local dev with SSO)
3. `ACCESS_KEY` / `SECRET_ACCESS_KEY` (static keys)
4. Default credential chain (`~/.aws/credentials`, instance role, etc.)

## Prerequisites

- Python 3.10+
- AWS account with Bedrock Knowledge Base + Data Source provisioned
- LibreOffice for Office → PDF conversion (included in Docker image)
- Three S3 buckets (see Bucket Naming above)

## Running Locally (Development)

```bash
# 1. Install
pip install -e .

# 2. Install LibreOffice (if converting Office files)
sudo apt install libreoffice-core libreoffice-writer   # Linux
brew install --cask libreoffice                         # macOS

# 3. Configure
cp .env.example .env
# Edit .env — at minimum set REGION, KNOWLEDGE_BASE_ID, DATA_SOURCE_ID
# Set AWS_PROFILE for SSO auth, or rely on default credential chain

# 4. Run
python run.py
# → http://localhost:5000
```

Flask runs in debug mode with auto-reload. Sessions persist across reloads via a `.secret_key` file.

## Running in Docker (Local)

```bash
# Build
docker build -t retrieval-kit .

# Run
docker run -d -p 5000:5000 \
  -e REGION="us-gov-west-1" \
  -e KNOWLEDGE_BASE_ID="<your_kb_id>" \
  -e DATA_SOURCE_ID="<your_ds_id>" \
  -e BEDROCK_MODEL_ID="amazon.nova-pro-v1:0" \
  -e ADMIN_EMAIL="admin@root" \
  -e ADMIN_PASSWORD="<password>" \
  -e REGISTER_EMAIL="<contact_email>" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e AWS_ACCESS_KEY_ID="<access_key>" \
  -e AWS_SECRET_ACCESS_KEY="<secret_access_key>" \
  -e AWS_SESSION_TOKEN="<session_token>" \
  --name retrieval-kit retrieval-kit
```

Replace the `<placeholder>` values with your actual config. To get AWS session credentials from SSO:

```bash
aws configure export-credentials --profile your-profile --format env
```

Or pass them from your current shell session:

```bash
docker run -d -p 5000:5000 \
  -e REGION="us-gov-west-1" \
  -e KNOWLEDGE_BASE_ID="<your_kb_id>" \
  -e DATA_SOURCE_ID="<your_ds_id>" \
  -e BEDROCK_MODEL_ID="amazon.nova-pro-v1:0" \
  -e ADMIN_EMAIL="admin@root" \
  -e ADMIN_PASSWORD="<password>" \
  -e REGISTER_EMAIL="<contact_email>" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  -e AWS_SESSION_TOKEN="$AWS_SESSION_TOKEN" \
  --name retrieval-kit retrieval-kit
```

```bash
# Logs
docker logs -f retrieval-kit

# Cleanup
docker stop retrieval-kit && docker rm retrieval-kit

# Export image as tar for transfer to air-gapped / disconnected environments
docker save retrieval-kit:latest -o retrieval-kit.tar

# Load on the target machine
# docker load -i retrieval-kit.tar
```

## Deploying to ECS (Production)

### 1. Create Secrets in Secrets Manager

Store sensitive values in AWS Secrets Manager so they never appear in task definitions or environment variables:

```bash
aws secretsmanager create-secret \
  --name RetrievalKitConfig \
  --secret-string '{"admin_password":"<password>","secret_key":"<random_hex_64>"}' \
  --region us-gov-west-1
```

### 2. Push Image to ECR

```bash
docker build -t retrieval-kit .
aws ecr get-login-password --region us-gov-west-1 | \
  docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-gov-west-1.amazonaws.com
docker tag retrieval-kit:latest <account-id>.dkr.ecr.us-gov-west-1.amazonaws.com/retrieval-kit:latest
docker push <account-id>.dkr.ecr.us-gov-west-1.amazonaws.com/retrieval-kit:latest
```

### 3. Create ECS Task Definition

The container runs on port 5000 behind an ALB (which handles TLS). The ECS task role provides AWS credentials — no static keys.

```json
{
  "family": "retrieval-kit",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "taskRoleArn": "arn:aws-us-gov:iam::<account-id>:role/RetrievalKitTaskRole",
  "executionRoleArn": "arn:aws-us-gov:iam::<account-id>:role/ecsTaskExecutionRole",
  "containerDefinitions": [
    {
      "name": "retrieval-kit",
      "image": "<account-id>.dkr.ecr.us-gov-west-1.amazonaws.com/retrieval-kit:latest",
      "portMappings": [{"containerPort": 5000, "protocol": "tcp"}],
      "environment": [
        {"name": "REGION", "value": "us-gov-west-1"},
        {"name": "KNOWLEDGE_BASE_ID", "value": "<your_kb_id>"},
        {"name": "DATA_SOURCE_ID", "value": "<your_ds_id>"},
        {"name": "BEDROCK_MODEL_ID", "value": "amazon.nova-pro-v1:0"},
        {"name": "APP_PREFIX", "value": ""},
        {"name": "ADMIN_EMAIL", "value": "admin@example.com"},
        {"name": "REGISTER_EMAIL", "value": "admin@example.com"}
      ],
      "secrets": [
        {"name": "ADMIN_PASSWORD", "valueFrom": "arn:aws-us-gov:secretsmanager:us-gov-west-1:<account-id>:secret:RetrievalKitConfig:admin_password::"},
        {"name": "SECRET_KEY", "valueFrom": "arn:aws-us-gov:secretsmanager:us-gov-west-1:<account-id>:secret:RetrievalKitConfig:secret_key::"}
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/retrieval-kit",
          "awslogs-region": "us-gov-west-1",
          "awslogs-stream-prefix": "ecs"
        }
      },
      "healthCheck": {
        "command": ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:5000/login')\" || exit 1"],
        "interval": 30,
        "timeout": 10,
        "retries": 3
      }
    }
  ]
}
```

### 4. Create ALB + Target Group

- ALB listener: HTTPS 443 → target group on port 5000
- Health check path: `/login`
- Stickiness: enabled (session cookies)

### 5. Create ECS Service

```bash
aws ecs create-service \
  --cluster your-cluster \
  --service-name retrieval-kit \
  --task-definition retrieval-kit \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxx],securityGroups=[sg-xxx],assignPublicIp=ENABLED}" \
  --load-balancers "targetGroupArn=arn:aws-us-gov:elasticloadbalancing:...,containerName=retrieval-kit,containerPort=5000" \
  --region us-gov-west-1
```

### ECS Task Role Permissions

The task role needs S3 access to the three buckets plus Bedrock permissions. See the IAM Permissions Required section below. No static credentials — ECS injects the task role automatically.

### What's Different Between Local and ECS

| | Local (`python run.py`) | Docker Local | ECS (Production) |
|---|---|---|---|
| AWS auth | `AWS_PROFILE` or SSO | Session tokens via `-e` | ECS task role (automatic) |
| Config source | `.env` file | `-e` flags | Task definition + Secrets Manager |
| Secret key | Auto-generated `.secret_key` file | `-e SECRET_KEY` | Secrets Manager |
| Admin password | `.env` file | `-e ADMIN_PASSWORD` | Secrets Manager |
| TLS | None (http://localhost:5000) | None (http://localhost:5000) | ALB handles HTTPS |
| Server | Flask debug server | Gunicorn (8 threads) | Gunicorn (8 threads) |
| LibreOffice | Install manually | Included in image | Included in image |
| Auto-reload | Yes (debug mode) | No | No |

## Standalone Auth & Administration

When running standalone (`python run.py` or Docker/ECS), retrieval-kit provides a complete user management system:

- **Login page** — Email/password authentication at `/login`. No self-registration; admins create accounts.
- **User accounts** — Stored as JSON in the audit S3 bucket under `_users/` prefix. No database required.
- **Role-based access** — Roles stored under `_roles/` prefix. Each role has a set of attributes that control what the user can see and do.
- **Root admin** — Seeded from `ADMIN_EMAIL`/`ADMIN_PASSWORD` on first run. Cannot be modified, deactivated, or deleted. Password managed only via env vars.
- **User administration** — Create, activate/deactivate, delete users. Assign roles. Reset passwords. Accounts must be deactivated before deletion.
- **Role administration** — Create custom roles, toggle attributes on/off per role. Built-in `admin` and `user` roles cannot be deleted.
- **Password management** — Users can change their own password. Admins can reset any user's password.
- **Session tracking** — Last login time and online status (active within 5 minutes) shown in the admin panel.
- **Contact for access** — Login page shows `REGISTER_EMAIL` as a contact for account requests.

None of this affects host apps. Host apps handle their own auth and user management — retrieval-kit only provides the audit trail and document permission attributes to them.

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
                "arn:aws:s3:::{prefix}retrieval-kit-original-documents/*",
                "arn:aws:s3:::{prefix}retrieval-kit-audit-logs",
                "arn:aws:s3:::{prefix}retrieval-kit-audit-logs/*"
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
        │   ├── documentation-page.html
        │   └── login-page.html       # Standalone login page
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
| 401 | Login required, invalid credentials |
| 403 | Access denied, account deactivated, root admin protected |
| 404 | Document not found |
| 409 | Duplicate document |
| 413 | File exceeds 50 MB |
| 500 | Conversion failure, S3 error, Bedrock error |

## Versioning

Host apps pin to a specific tag:
```
retrieval-kit @ git+https://github.com/LeafmanZ/retrieval-kit.git@v1.3.0
```

To upgrade: update the version pin and `pip install`. No host code changes needed unless the config contract changed (major version bump).
