import os
import re
import json
import subprocess
import tempfile
import logging
import threading
import boto3
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify(error="File too large — maximum size is 50 MB"), 413

# Bedrock Data Automation supported formats
BDA_NATIVE = {
    "pdf", "png", "jpg", "jpeg", "tiff", "bmp", "webp",
    "mp3", "mp4", "wav", "flac", "ogg", "amr", "webm",
    "mkv", "avi", "mov",
    "csv", "txt",
}

# Office formats that need LibreOffice conversion to PDF
CONVERT_TO_PDF = {
    "ppt", "pptx", "doc", "docx", "xls", "xlsx",
    "rtf", "odt", "odp", "ods", "html", "htm",
}

ALLOWED_EXTENSIONS = BDA_NATIVE | CONVERT_TO_PDF

_APP_PREFIX = os.getenv("APP_PREFIX") or ""
BUCKET = f"{_APP_PREFIX}retrieval-kit-source-documents"
ORIGINALS_BUCKET = f"{_APP_PREFIX}retrieval-kit-original-documents"
KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID")
DATA_SOURCE_ID = os.getenv("DATA_SOURCE_ID")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")

_aws_kw = {"region_name": os.getenv("REGION")}
_ak, _sk = os.getenv("ACCESS_KEY"), os.getenv("SECRET_ACCESS_KEY")
if _ak and _sk:
    _aws_kw.update(aws_access_key_id=_ak, aws_secret_access_key=_sk)
_session = boto3.Session(profile_name=os.getenv("AWS_PROFILE"), **_aws_kw)

if _ak and _sk:
    logger.info("AWS auth: static keys from .env (ACCESS_KEY)")
elif os.getenv("AWS_PROFILE"):
    logger.info("AWS auth: named profile '%s'", os.getenv("AWS_PROFILE"))
else:
    logger.info("AWS auth: default credential chain (env vars / ~/.aws/credentials / instance role)")

s3 = _session.client("s3")
bedrock_agent = _session.client("bedrock-agent")
bedrock_agent_runtime = _session.client("bedrock-agent-runtime")
bedrock_runtime = _session.client("bedrock-runtime")


def make_safe_name(filename):
    name = secure_filename(filename)
    if not name:
        return None
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    ext = ext.lower()
    if not stem:
        return None
    return stem, ext


def convert_to_pdf(data, ext):
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f"input{ext}")
        with open(src, "wb") as fh:
            fh.write(data)
        result = subprocess.run(
            ["libreoffice", "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmp, src],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        with open(os.path.join(tmp, "input.pdf"), "rb") as fh:
            return fh.read()


def _count_s3_objects(bucket):
    count = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        count += page.get("KeyCount", 0)
    return count


def _get_latest_ingestion():
    try:
        resp = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            maxResults=1,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        )
        jobs = resp.get("ingestionJobSummaries", [])
        if not jobs:
            return None, 0
        job = jobs[0]
        scanned = job.get("statistics", {}).get("numberOfDocumentsScanned", 0)
        return job["status"], scanned
    except Exception as e:
        logger.error("Failed to get ingestion status: %s", e)
        return None, 0


def _sync_if_needed():
    if not KNOWLEDGE_BASE_ID or not DATA_SOURCE_ID:
        return
    try:
        status, scanned_count = _get_latest_ingestion()
        if status in ("STARTING", "IN_PROGRESS"):
            return
        source_count = _count_s3_objects(BUCKET)
        if source_count == scanned_count:
            return
        logger.info("Source (%d) != scanned (%d), triggering sync", source_count, scanned_count)
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
        )
        logger.info("KB sync triggered: %s", resp["ingestionJob"]["ingestionJobId"])
    except Exception as e:
        logger.error("KB sync check failed: %s", e)


def _sync_poller():
    while True:
        _sync_if_needed()
        threading.Event().wait(10)


def process_upload(f):
    if not f or not f.filename:
        return {"error": "No file provided"}, 400

    result = make_safe_name(f.filename)
    if not result:
        return {"error": "Invalid filename"}, 400

    safe_name, ext = result
    raw_ext = ext.lstrip(".")
    if raw_ext not in ALLOWED_EXTENSIONS:
        return {"error": f"File type '{ext}' not allowed"}, 400

    data = f.read()
    if len(data) == 0:
        return {"error": "Empty file"}, 400

    original_key = f"{safe_name}{ext}"

    # Check originals bucket for duplicate
    try:
        s3.head_object(Bucket=ORIGINALS_BUCKET, Key=original_key)
        return {"error": f"Document '{original_key}' already exists"}, 409
    except s3.exceptions.ClientError:
        pass

    # Convert Office formats to PDF first — fail before any S3 writes
    if raw_ext in CONVERT_TO_PDF:
        try:
            kb_data = convert_to_pdf(data, ext)
        except Exception as e:
            return {"error": f"Conversion to PDF failed: {e}"}, 500
        s3_key = f"{safe_name}_{raw_ext}.pdf"
    else:
        kb_data = data
        s3_key = original_key

    # Upload to KB source bucket first
    try:
        s3.put_object(
            Bucket=BUCKET, Key=s3_key, Body=kb_data,
            ContentType="application/pdf" if raw_ext in CONVERT_TO_PDF else (f.content_type or "application/octet-stream"),
        )
    except Exception as e:
        return {"error": f"Failed to upload to knowledge base: {e}"}, 500

    # Only save original after KB source succeeds
    try:
        s3.put_object(
            Bucket=ORIGINALS_BUCKET, Key=original_key, Body=data,
            ContentType=f.content_type or "application/octet-stream",
        )
    except Exception as e:
        # Rollback KB source
        try:
            s3.delete_object(Bucket=BUCKET, Key=s3_key)
        except Exception:
            pass
        return {"error": f"Failed to save original: {e}"}, 500

    return {
        "message": f"Uploaded → s3://{BUCKET}/{s3_key}",
        "key": s3_key,
        "original_key": original_key,
    }, 200


@app.route("/")
def index():
    exts = sorted(ALLOWED_EXTENSIONS)
    return render_template("documentation-page.html", allowed_extensions=exts, accept_string=",".join(f".{e}" for e in exts))


@app.route("/api/stats", methods=["GET"])
def api_stats():
    type_counts = {}
    total = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=ORIGINALS_BUCKET):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/"):
                    continue
                total += 1
                ext = obj["Key"].rsplit(".", 1)[-1].lower() if "." in obj["Key"] else "other"
                type_counts[ext] = type_counts.get(ext, 0) + 1
        kb_source_count = _count_s3_objects(BUCKET)
    except Exception as e:
        logger.error("AWS credentials error in stats: %s", e)
        return jsonify(error=f"AWS credentials error: {e}"), 503

    # Detect orphans: files in originals with no matching KB source file
    kb_keys = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            kb_keys.add(obj["Key"])
    orphans = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=ORIGINALS_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
            if ext in CONVERT_TO_PDF:
                stem = key.rsplit(".", 1)[0]
                expected = f"{stem}_{ext}.pdf"
            else:
                expected = key
            if expected not in kb_keys:
                orphans.append(key)
    # Latest ingestion job details
    sync = {"status": "UNKNOWN"}
    try:
        resp = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            maxResults=1,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        )
        jobs = resp.get("ingestionJobSummaries", [])
        if jobs:
            job_summary = jobs[0]
            # Get full job details (summary doesn't include failureReasons)
            detail_resp = bedrock_agent.get_ingestion_job(
                knowledgeBaseId=KNOWLEDGE_BASE_ID,
                dataSourceId=DATA_SOURCE_ID,
                ingestionJobId=job_summary["ingestionJobId"],
            )
            job = detail_resp["ingestionJob"]
            s = job.get("statistics", {})
            sync = {
                "status": job["status"],
                "started_at": job.get("startedAt", "").isoformat() if hasattr(job.get("startedAt", ""), "isoformat") else str(job.get("startedAt", "")),
                "updated_at": job.get("updatedAt", "").isoformat() if hasattr(job.get("updatedAt", ""), "isoformat") else str(job.get("updatedAt", "")),
                "source_files": s.get("numberOfDocumentsScanned", 0),
                "metadata_files": s.get("numberOfMetadataDocumentsScanned", 0),
                "added": s.get("numberOfNewDocumentsIndexed", 0),
                "modified": s.get("numberOfModifiedDocumentsIndexed", 0),
                "deleted": s.get("numberOfDocumentsDeleted", 0),
                "failed": s.get("numberOfDocumentsFailed", 0),
                "metadata_modified": s.get("numberOfMetadataDocumentsModified", 0),
                "failure_reasons": job.get("failureReasons", []),
            }
    except Exception as e:
        logger.error("Failed to get ingestion details: %s", e)
    return jsonify(total=total, kb_source=kb_source_count, by_type=type_counts, sync=sync, orphans=orphans), 200


@app.route("/api/documents/<path:key>/retry", methods=["POST"])
def retry_convert(key):
    """Re-read original from S3, convert if needed, and push to KB source bucket."""
    try:
        obj = s3.get_object(Bucket=ORIGINALS_BUCKET, Key=key)
        data = obj["Body"].read()
    except Exception:
        return jsonify(error="Original not found"), 404

    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    if ext in CONVERT_TO_PDF:
        try:
            kb_data = convert_to_pdf(data, f".{ext}")
        except Exception as e:
            return jsonify(error=f"Conversion failed: {e}"), 500
        stem = key.rsplit(".", 1)[0]
        s3_key = f"{stem}_{ext}.pdf"
        content_type = "application/pdf"
    else:
        kb_data = data
        s3_key = key
        content_type = obj.get("ContentType", "application/octet-stream")

    try:
        s3.put_object(Bucket=BUCKET, Key=s3_key, Body=kb_data, ContentType=content_type)
    except Exception as e:
        return jsonify(error=f"Upload to KB failed: {e}"), 500

    return jsonify(message=f"Pushed to KB: {s3_key}", kb_key=s3_key), 200


@app.route("/api/orphans/<path:key>", methods=["DELETE"])
def delete_orphan(key):
    try:
        s3.delete_object(Bucket=ORIGINALS_BUCKET, Key=key)
        return jsonify(message=f"Removed orphan: {key}"), 200
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/api/documents", methods=["GET"])
def list_documents():
    # Build set of KB source keys for lookup
    kb_keys = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            kb_keys.add(obj["Key"])

    docs = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=ORIGINALS_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
            converted = ext in CONVERT_TO_PDF
            if converted:
                stem = key.rsplit(".", 1)[0]
                kb_key = f"{stem}_{ext}.pdf"
            else:
                kb_key = key
            docs.append({
                "key": key,
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
                "converted": converted,
                "in_kb": kb_key in kb_keys,
                "kb_key": kb_key,
                "view_url": s3.generate_presigned_url(
                    "get_object", Params={"Bucket": ORIGINALS_BUCKET, "Key": key}, ExpiresIn=3600,
                ),
                "download_url": s3.generate_presigned_url(
                    "get_object", Params={
                        "Bucket": ORIGINALS_BUCKET, "Key": key,
                        "ResponseContentDisposition": f'attachment; filename="{key}"',
                    }, ExpiresIn=3600,
                ),
            })
    return jsonify(documents=docs), 200


@app.route("/api/documents/<path:key>", methods=["DELETE"])
def delete_document(key):
    # Delete original
    try:
        s3.head_object(Bucket=ORIGINALS_BUCKET, Key=key)
        s3.delete_object(Bucket=ORIGINALS_BUCKET, Key=key)
    except s3.exceptions.ClientError:
        return jsonify(error="Document not found"), 404
    # Delete from KB source bucket (may be PDF-converted version)
    stem = os.path.splitext(key)[0]
    try:
        s3.delete_object(Bucket=BUCKET, Key=key)
    except Exception:
        pass
    # Also try the PDF version in case it was converted
    if not key.endswith(".pdf"):
        try:
            s3.delete_object(Bucket=BUCKET, Key=f"{stem}.pdf")
        except Exception:
            pass
    return jsonify(message=f"Deleted {key}"), 200


@app.route("/api/ingestion/<job_id>", methods=["GET"])
def ingestion_status(job_id):
    try:
        resp = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            ingestionJobId=job_id,
        )
        job = resp["ingestionJob"]
        stats = job.get("statistics", {})
        return jsonify(
            status=job["status"],
            failure_reasons=job.get("failureReasons", []),
            stats={
                "scanned": stats.get("numberOfDocumentsScanned", 0),
                "indexed": stats.get("numberOfNewDocumentsIndexed", 0),
                "updated": stats.get("numberOfModifiedDocumentsIndexed", 0),
                "failed": stats.get("numberOfDocumentsFailed", 0),
            },
        ), 200
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/upload", methods=["POST"])
def upload():
    resp, code = process_upload(request.files.get("file"))
    return jsonify(resp), code


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("files") or [request.files.get("file")]
    results = []
    for f in files:
        resp, code = process_upload(f)
        resp["status"] = code
        results.append(resp)
    return jsonify(results=results), 200


SEARCH_SYSTEM = """You are a search routing agent. Given the user's search query, decide which retrieval tools to call.
You have 4 tools:
- semantic_search: Search document contents by meaning/topic. Use for questions, topics, or content searches.
- exact_text_search: Find documents containing an exact literal string. Use when the query contains specific numbers, codes, special characters (pipes |, commas in numbers, decimals), IDs, or any precise text that must match exactly.
- filename_search: Find documents by filename substring. Use when the query looks like a filename or the user wants to find files by name.
- search_within_document: Semantic search scoped to a specific document. Use when the user asks about the content of a specific named document. For the filename parameter, use just the name stem without extension (e.g. "jordan_hillis_resume" not "jordan_hillis_resume.pdf").

You may call multiple tools if needed. Always call at least one tool.
When the query contains exact numbers, codes, or special characters, prefer exact_text_search.

IMPORTANT rules:
- If a tool returns 0 results, try a different strategy (e.g. semantic_search with the document name in the query, or exact_text_search for literal matches).
- If a tool already returned good results (>0), do NOT call additional tools unless the results were clearly empty or irrelevant. Stop and let the user see what was found.
- Do NOT repeat searches that overlap with results you already have."""


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _apply_search_config(result, search_config):
    """Apply score_threshold and min/max_results filtering to a tool result."""
    if not search_config or "results" not in result:
        return result
    threshold = search_config.get("score_threshold", 0)
    min_r = search_config.get("min_results", 1)
    max_r = search_config.get("max_results", 5)
    items = result["results"]
    if threshold > 0:
        filtered = [r for r in items if (r.get("score") or 0) >= threshold / 100]
        items = filtered if len(filtered) >= min_r else items[:min_r]
    result["results"] = items[:max_r]
    return result


@app.route("/api/smart_search", methods=["POST"])
def smart_search():
    body = request.get_json(silent=True) or {}
    q = body.get("query", "").strip()
    if not q:
        return jsonify(error="Missing 'query'"), 400
    scoped_keys = body.get("scoped_keys")
    search_config = body.get("search_config")

    def generate():
        yield _sse("status", {"message": f"Analyzing your query{' (filtered)' if scoped_keys else ''}…"})

        messages = [{"role": "user", "content": [{"text": q}]}]
        resp = _converse(messages, SEARCH_SYSTEM, tools=RETRIEVAL_TOOLS)
        output = resp["output"]["message"]
        stop_reason = resp["stopReason"]

        all_results = []
        tool_round = 0
        MAX_TOOL_ROUNDS = 4

        while stop_reason == "tool_use" and tool_round < MAX_TOOL_ROUNDS:
            tool_round += 1
            tool_names = [b["toolUse"]["name"] for b in output["content"] if "toolUse" in b]
            yield _sse("status", {"message": f"Decided to use: {', '.join(t.replace('_', ' ') for t in tool_names)}"})

            tool_results = []
            for block in output["content"]:
                if "toolUse" not in block:
                    continue
                tool = block["toolUse"]
                name = tool["name"]
                inp = tool["input"]
                friendly = name.replace("_", " ")
                yield _sse("status", {"message": f"Running {friendly}…", "tool": name, "input": inp})
                try:
                    result = _execute_tool(name, inp, scoped_keys=scoped_keys, search_config=search_config)
                    result = _apply_search_config(result, search_config)
                    count = len(result.get("results", result.get("documents", [])))
                    yield _sse("tool_result", {"tool": name, "input": inp, "result": result})
                    yield _sse("status", {"message": f"{friendly} returned {count} result{'s' if count != 1 else ''}"})
                    tool_results.append({"toolResult": {"toolUseId": tool["toolUseId"], "content": [{"json": result}]}})
                    all_results.append({"tool": name, "input": inp, "result": result})
                except Exception as e:
                    logger.error("Smart search tool %s failed: %s", name, e)
                    yield _sse("status", {"message": f"{friendly} failed: {str(e)}", "error": True})
                    tool_results.append({"toolResult": {"toolUseId": tool["toolUseId"], "content": [{"json": {"error": str(e)}}], "status": "error"}})

            messages.append(output)
            messages.append({"role": "user", "content": tool_results})

            yield _sse("status", {"message": "Checking if more retrieval is needed…"})
            resp = _converse(messages, SEARCH_SYSTEM, tools=RETRIEVAL_TOOLS)
            output = resp["output"]["message"]
            stop_reason = resp["stopReason"]

        if tool_round >= MAX_TOOL_ROUNDS and stop_reason == "tool_use":
            yield _sse("status", {"message": f"Stopped after {MAX_TOOL_ROUNDS} rounds"})
        yield _sse("done", {"query": q, "tool_results": all_results})

    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/query", methods=["POST"])
def query():
    body = request.get_json(silent=True) or {}
    q = body.get("query", "").strip()
    if not q:
        return jsonify(error="Missing 'query'"), 400

    n_results = body.get("n_results", 5)

    resp = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": q},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": n_results,
                "overrideSearchType": "HYBRID",
            }
        },
    )

    sources = []
    for result in resp.get("retrievalResults", []):
        loc = result.get("location", {})
        s3_loc = loc.get("s3Location", {})
        content = result.get("content", {})
        metadata = result.get("metadata", {})
        content_type = content.get("type", "TEXT")

        # Extract text based on content type
        if content_type == "AUDIO":
            audio = content.get("audio", {})
            text = audio.get("transcription", "")
        elif content_type == "VIDEO":
            video = content.get("video", {})
            text = video.get("summary", "")
        else:
            text = content.get("text", "")

        source = {
            "text": text,
            "score": result.get("score"),
            "uri": s3_loc.get("uri", ""),
            "content_type": content_type,
            "image_url": None,
            "description": metadata.get("x-amz-bedrock-kb-description", ""),
            "page": metadata.get("x-amz-bedrock-kb-document-page-number"),
            "start_time_ms": metadata.get("x-amz-bedrock-kb-chunk-start-time-in-millis"),
            "end_time_ms": metadata.get("x-amz-bedrock-kb-chunk-end-time-in-millis"),
        }
        if content_type == "IMAGE":
            img_src = metadata.get("x-amz-bedrock-kb-byte-content-source", s3_loc.get("uri", ""))
            source["image_url"] = _presign_uri(img_src)
        sources.append(source)

    return jsonify(query=q, sources=sources), 200


def _presign_uri(s3_uri):
    if not s3_uri or not s3_uri.startswith("s3://"):
        return None
    try:
        parts = s3_uri.replace("s3://", "").split("/", 1)
        if len(parts) != 2:
            return None
        bucket, key = parts
        return s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600,
        )
    except Exception:
        return None


# ── Chat agent with tool-use orchestration ──────────────────

RETRIEVAL_TOOLS = [
    {
        "toolSpec": {
            "name": "semantic_search",
            "description": "Search documents by meaning/topic. Use when the user asks a question about content, wants information, or needs answers from documents.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The semantic search query"},
                    "n_results": {"type": "integer", "description": "Number of results (default 5)"},
                },
                "required": ["query"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "exact_text_search",
            "description": "Find documents containing an exact text string. Use when the user searches for specific numbers, codes, IDs, exact phrases with special characters (pipes, commas, decimals), or any literal string that semantic search might miss.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The exact text string to find (literal match)"},
                    "filename": {"type": "string", "description": "Optional: scope search to a specific document"},
                },
                "required": ["query"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "filename_search",
            "description": "Find documents by filename substring match. Use when the user asks about a specific document by name, wants to find/list files, or references a document title.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match against document filenames"},
                },
                "required": ["query"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "search_within_document",
            "description": "Semantic search scoped to a specific document. Use when the user asks about the content of a specific named document, e.g. 'what does the resume say about cybersecurity' or 'summarize the audio file'.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The semantic search query"},
                    "filename": {"type": "string", "description": "Document filename or substring to scope the search to"},
                    "n_results": {"type": "integer", "description": "Number of results (default 5)"},
                },
                "required": ["query", "filename"],
            }},
        }
    },
]

ORCHESTRATOR_SYSTEM = """You are a retrieval orchestrator. Given the user's message and conversation history, decide which retrieval tools to call.

You have 4 tools:
- semantic_search: Search all documents by meaning/topic
- exact_text_search: Find documents containing an exact literal string. Use for specific numbers, codes, special characters (pipes, commas in numbers, decimals), or any text that must match verbatim.
- filename_search: Find documents by name
- search_within_document: Semantic search within a specific document. For the filename parameter, use just the name stem without extension (e.g. "jordan_hillis_resume" not "jordan_hillis_resume.pdf").

Call one or more tools as needed. You may call multiple tools in parallel.
If the user is just chatting (greeting, thanks, etc.) respond directly without tools.
When the query contains exact numbers, codes, or special characters, prefer exact_text_search.

IMPORTANT rules:
- If a tool returns 0 results, try a different strategy (e.g. semantic_search with the document name in the query, or exact_text_search for literal matches).
- If a tool already returned good results (>0), do NOT call additional tools unless the results were clearly empty or irrelevant. Stop and let the user see what was found.
- Do NOT repeat searches that overlap with results you already have."""

CHAT_SYSTEM = """You are a helpful document assistant. Answer the user's question using the retrieved context provided.
Be concise and accurate. Cite your sources by mentioning the document filename.
If the context doesn't contain enough information to answer, say so.
For audio/video content, the text is a transcription or summary of the media."""


def _execute_tool(name, input_data, scoped_keys=None, search_config=None):
    cfg = search_config or {}
    n = cfg.get("max_results", input_data.get("n_results", 5))
    st = cfg.get("search_type", "HYBRID")
    if name == "semantic_search":
        return _do_semantic_search(input_data["query"], n, scoped_keys=scoped_keys, search_type=st)
    elif name == "filename_search":
        return _do_filename_search(input_data["query"], scoped_keys=scoped_keys)
    elif name == "search_within_document":
        return _do_scoped_search(input_data["query"], input_data["filename"], n, scoped_keys=scoped_keys, search_type=st)
    elif name == "exact_text_search":
        return _do_exact_text_search(input_data["query"], input_data.get("filename"), scoped_keys=scoped_keys, search_type=st)
    return {"error": f"Unknown tool: {name}"}


def _do_exact_text_search(query, filename=None, scoped_keys=None, search_type="HYBRID"):
    """Retrieve many chunks and filter by literal substring match."""
    search_query = query if len(query) > 30 else f"document containing {query}"
    vector_config = {
        "numberOfResults": 25,
        "overrideSearchType": search_type,
    }
    if filename:
        candidate = filename.rsplit(".", 1)[0] if "." in filename else filename
        vector_config["filter"] = {
            "stringContains": {
                "key": "x-amz-bedrock-kb-source-uri",
                "value": candidate,
            }
        }
    resp = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": search_query},
        retrievalConfiguration={"vectorSearchConfiguration": vector_config},
    )
    # Filter results by exact substring
    normalized_query = query.replace("\u00a0", " ")
    results = []
    for r in resp.get("retrievalResults", []):
        content = r.get("content", {})
        metadata = r.get("metadata", {})
        text = content.get("text", "")
        if not text:
            text = metadata.get("x-amz-bedrock-kb-description", "")
        if normalized_query in text or normalized_query in text.replace("\u00a0", " "):
            uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
            fn = uri.split("/")[-1] if uri else "unknown"
            if scoped_keys and not any(k in uri for k in scoped_keys):
                continue
            entry = {
                "filename": fn, "text": text, "score": r.get("score"),
                "content_type": "TEXT", "retrieval_method": "exact text match",
                "page": metadata.get("x-amz-bedrock-kb-document-page-number"),
                "start_time_ms": metadata.get("x-amz-bedrock-kb-chunk-start-time-in-millis"),
                "end_time_ms": metadata.get("x-amz-bedrock-kb-chunk-end-time-in-millis"),
            }
            original_key = _resolve_original_key(fn)
            if original_key:
                entry["source_url"] = s3.generate_presigned_url(
                    "get_object", Params={"Bucket": ORIGINALS_BUCKET, "Key": original_key}, ExpiresIn=3600,
                )
            results.append(entry)
    return {"results": results}


def _do_semantic_search(query, n_results=5, scoped_keys=None, search_type="HYBRID"):
    resp = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {
            "numberOfResults": n_results if not scoped_keys else max(n_results * 3, 15),
            "overrideSearchType": search_type,
        }},
    )
    result = _format_retrieval_results(resp, retrieval_method="prompt → hybrid search", scoped_keys=scoped_keys)
    if scoped_keys:
        result["results"] = result["results"][:n_results]
    return result


def _do_filename_search(query, scoped_keys=None):
    q = query.lower()
    matches = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=ORIGINALS_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/") and q in key.lower():
                if scoped_keys:
                    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
                    if ext in CONVERT_TO_PDF:
                        stem = key.rsplit(".", 1)[0]
                        kb_key = f"{stem}_{ext}.pdf"
                    else:
                        kb_key = key
                    if kb_key not in scoped_keys:
                        continue
                matches.append({"key": key, "size": obj["Size"]})
    if not matches:
        return {"documents": [], "results": []}
    # Pull content chunks from KB for matched files using a broad retrieval query
    all_chunks = []
    for doc in matches:
        stem = doc["key"].rsplit(".", 1)[0] if "." in doc["key"] else doc["key"]
        retrieval_query = f"What are the main contents, topics, and key information in {doc['key']}?"
        chunks = _do_scoped_search(retrieval_query, stem, n_results=3, retrieval_method=f"file match '{doc['key']}' → content pull", scoped_keys=scoped_keys)
        all_chunks.extend(chunks.get("results", []))
    return {"documents": matches, "results": all_chunks}


def _do_scoped_search(query, filename, n_results=5, retrieval_method=None, scoped_keys=None, search_type="HYBRID"):
    method = retrieval_method or f"prompt → scoped search '{filename}'"
    # Try with the given filename first, then fall back to stem-only
    # (handles converted files like resume.pptx -> resume_pptx.pdf)
    candidates = [filename]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    if stem != filename:
        candidates.append(stem)

    for candidate in candidates:
        vector_config = {
            "numberOfResults": n_results,
            "overrideSearchType": search_type,
            "filter": {
                "stringContains": {
                    "key": "x-amz-bedrock-kb-source-uri",
                    "value": candidate,
                }
            },
        }
        resp = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vector_config},
        )
        result = _format_retrieval_results(resp, retrieval_method=method, scoped_keys=scoped_keys)
        if result["results"]:
            return result
    return result


def _resolve_original_key(kb_filename):
    """Map a KB bucket filename back to the original document key.
    Converted files like 'resume_pptx.pdf' map back to 'resume.pptx'."""
    if not kb_filename or kb_filename == "unknown":
        return None
    # Check if it exists directly in originals
    try:
        s3.head_object(Bucket=ORIGINALS_BUCKET, Key=kb_filename)
        return kb_filename
    except Exception:
        pass
    # Try reversing the conversion pattern: stem_ext.pdf -> stem.ext
    if kb_filename.endswith(".pdf"):
        stem = kb_filename[:-4]
        for ext in CONVERT_TO_PDF:
            if stem.endswith(f"_{ext}"):
                original = stem[:-(len(ext) + 1)] + f".{ext}"
                try:
                    s3.head_object(Bucket=ORIGINALS_BUCKET, Key=original)
                    return original
                except Exception:
                    pass
    return None


def _format_retrieval_results(resp, retrieval_method="semantic", scoped_keys=None):
    results = []
    for r in resp.get("retrievalResults", []):
        content = r.get("content", {})
        metadata = r.get("metadata", {})
        content_type = content.get("type", "TEXT")
        if content_type == "AUDIO":
            text = content.get("audio", {}).get("transcription", "")
        elif content_type == "VIDEO":
            text = content.get("video", {}).get("summary", "")
        else:
            text = content.get("text", "")
        if not text.strip():
            text = metadata.get("x-amz-bedrock-kb-description", "")
        uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
        filename = uri.split("/")[-1] if uri else "unknown"
        if scoped_keys and not any(k in uri for k in scoped_keys):
            continue
        entry = {
            "filename": filename,
            "text": text,
            "score": r.get("score"),
            "content_type": content_type,
            "retrieval_method": retrieval_method,
            "page": metadata.get("x-amz-bedrock-kb-document-page-number"),
            "start_time_ms": metadata.get("x-amz-bedrock-kb-chunk-start-time-in-millis"),
            "end_time_ms": metadata.get("x-amz-bedrock-kb-chunk-end-time-in-millis"),
        }
        if content_type == "IMAGE":
            img_src = metadata.get("x-amz-bedrock-kb-byte-content-source", uri)
            entry["image_url"] = _presign_uri(img_src)
        # Presigned URL to the original document for "view source"
        original_key = _resolve_original_key(filename)
        if original_key:
            entry["source_url"] = s3.generate_presigned_url(
                "get_object", Params={"Bucket": ORIGINALS_BUCKET, "Key": original_key}, ExpiresIn=3600,
            )
        results.append(entry)
    return {"results": results}


def _converse(messages, system, tools=None):
    kwargs = {
        "modelId": MODEL_ID,
        "messages": messages,
        "system": [{"text": system}],
    }
    if tools:
        kwargs["toolConfig"] = {"tools": tools}
    return bedrock_runtime.converse(**kwargs)


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(silent=True) or {}
    history = body.get("history", [])
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return jsonify(error="Missing 'message'"), 400
    scoped_keys = body.get("scoped_keys")
    search_config = body.get("search_config")

    def generate():
        yield _sse("status", {"message": f"Analyzing your question{' (filtered)' if scoped_keys else ''}…"})

        orch_messages = []
        for msg in history:
            orch_messages.append({"role": msg["role"], "content": [{"text": msg["content"]}]})
        orch_messages.append({"role": "user", "content": [{"text": user_msg}]})

        orch_resp = _converse(orch_messages, ORCHESTRATOR_SYSTEM, tools=RETRIEVAL_TOOLS)
        orch_output = orch_resp["output"]["message"]
        stop_reason = orch_resp["stopReason"]

        retrieved_context = []
        tool_round = 0
        MAX_TOOL_ROUNDS = 4

        while stop_reason == "tool_use" and tool_round < MAX_TOOL_ROUNDS:
            tool_round += 1
            tool_names = [b["toolUse"]["name"] for b in orch_output["content"] if "toolUse" in b]
            yield _sse("status", {"message": f"Decided to use: {', '.join(t.replace('_', ' ') for t in tool_names)}"})

            tool_results = []
            for block in orch_output["content"]:
                if "toolUse" not in block:
                    continue
                tool = block["toolUse"]
                name = tool["name"]
                inp = tool["input"]
                friendly = name.replace("_", " ")
                yield _sse("status", {"message": f"Running {friendly}…", "tool": name, "input": inp})
                try:
                    result = _execute_tool(name, inp, scoped_keys=scoped_keys, search_config=search_config)
                    result = _apply_search_config(result, search_config)
                    count = len(result.get("results", result.get("documents", [])))
                    yield _sse("tool_result", {"tool": name, "input": inp, "result": result})
                    yield _sse("status", {"message": f"{friendly} returned {count} result{'s' if count != 1 else ''}"})
                    tool_results.append({"toolResult": {"toolUseId": tool["toolUseId"], "content": [{"json": result}]}})
                    retrieved_context.append({"tool": name, "input": inp, "result": result})
                except Exception as e:
                    logger.error("Tool %s failed: %s", name, e)
                    yield _sse("status", {"message": f"{friendly} failed: {str(e)}", "error": True})
                    tool_results.append({"toolResult": {"toolUseId": tool["toolUseId"], "content": [{"json": {"error": str(e)}}], "status": "error"}})

            orch_messages.append(orch_output)
            orch_messages.append({"role": "user", "content": tool_results})
            yield _sse("status", {"message": "Checking if more retrieval is needed…"})
            orch_resp = _converse(orch_messages, ORCHESTRATOR_SYSTEM, tools=RETRIEVAL_TOOLS)
            orch_output = orch_resp["output"]["message"]
            stop_reason = orch_resp["stopReason"]

        if tool_round >= MAX_TOOL_ROUNDS and stop_reason == "tool_use":
            yield _sse("status", {"message": f"Stopped after {MAX_TOOL_ROUNDS} rounds"})

        if not retrieved_context:
            answer = ""
            for block in orch_output["content"]:
                if "text" in block:
                    answer += block["text"]
            yield _sse("answer", {"answer": answer, "citations": [], "tools_used": []})
            return

        yield _sse("status", {"message": "Generating answer…"})

        context_text = ""
        tools_used = []
        for ctx in retrieved_context:
            tools_used.append({"tool": ctx["tool"], "input": ctx["input"]})
            result = ctx["result"]
            if "results" in result:
                for r in result["results"]:
                    context_text += f"\n[{r['filename']}] (score: {r.get('score', 'N/A')})\n{r['text']}\n"
            if "documents" in result:
                for d in result["documents"]:
                    context_text += f"\n[Found document: {d['key']}] ({d['size']} bytes)\n"

        chat_messages = []
        for msg in history:
            chat_messages.append({"role": msg["role"], "content": [{"text": msg["content"]}]})
        chat_messages.append({"role": "user", "content": [{"text": f"Retrieved context:\n{context_text}\n\nUser question: {user_msg}"}]})

        chat_resp = _converse(chat_messages, CHAT_SYSTEM)
        answer = ""
        for block in chat_resp["output"]["message"]["content"]:
            if "text" in block:
                answer += block["text"]

        citations = list({r["filename"] for ctx in retrieved_context for r in ctx["result"].get("results", [])})
        yield _sse("answer", {"answer": answer, "citations": citations, "tools_used": tools_used})
        yield _sse("status", {"message": "Done"})

    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Start background sync poller once on first request
_poller_started = False

@app.before_request
def _start_poller():
    global _poller_started
    if not _poller_started:
        _poller_started = True
        threading.Thread(target=_sync_poller, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
