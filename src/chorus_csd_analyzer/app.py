"""FastAPI application for CSD Converter Web UI."""
import asyncio
import html
import json as _json
import logging
import logging.config
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

# Auto-load .env for local development (no-op if python-dotenv not installed or .env missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Request, UploadFile, File, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from chorus_forms.csd.parser import parse_csd_file

from chorus_v1_client import ChorusV1Client

from chorus_csd_analyzer.ai_client import AIGatewayClient
from chorus_csd_analyzer.enricher import enrich_forms
from chorus_csd_analyzer.agent import analyze_forms, DEFAULT_MODEL
from chorus_csd_analyzer.converter import convert_files


# ---------------------------------------------------------------------------
# Structured logging — JSON in production, human-readable in development
# ---------------------------------------------------------------------------

class _JSONFormatter(logging.Formatter):
    """JSON log formatter for production observability."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return _json.dumps(log_entry)


def _configure_logging() -> None:
    """Configure logging based on LOG_FORMAT env var (json or text)."""
    log_format = os.environ.get("LOG_FORMAT", "text").lower()
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    if log_format == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter())
        logging.root.handlers = [handler]
        logging.root.setLevel(log_level)
    else:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )


_configure_logging()
logger = logging.getLogger(__name__)

# In-memory session store: session_id -> {"dir": Path, "files": [Path, ...], "created_at": float}
sessions: dict[str, dict] = {}
SESSION_TTL_SECONDS = 3600  # 1 hour


def _validate_config() -> None:
    """Validate configuration at startup and log a summary."""
    chorus_url = os.environ.get("CHORUS_URL", "")
    chorus_user = os.environ.get("CHORUS_USER", "")
    ai_url = os.environ.get("AI_GATEWAY_URL", "")
    ai_key = os.environ.get("AI_GATEWAY_KEY", "")

    issues = []
    if chorus_url and not chorus_url.startswith(("http://", "https://")):
        issues.append(f"CHORUS_URL must start with http:// or https:// (got: {chorus_url[:30]})")
    if chorus_url and not chorus_user:
        issues.append("CHORUS_URL is set but CHORUS_USER is missing")
    if ai_url and not ai_key:
        issues.append("AI_GATEWAY_URL is set but AI_GATEWAY_KEY is missing")

    for issue in issues:
        logger.warning("Config issue: %s", issue)

    logger.info(
        "Startup config: chorus=%s enrichment=%s ai=%s log_format=%s",
        "configured" if chorus_url else "disabled",
        "enabled" if (chorus_url and chorus_user) else "disabled",
        "configured" if (ai_url and ai_key) else "disabled",
        os.environ.get("LOG_FORMAT", "text"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan: startup validation, cleanup task, graceful shutdown."""
    _validate_config()
    cleanup_task = asyncio.create_task(_session_cleanup_loop())
    logger.info("CSD Converter Web started")
    yield
    # Graceful shutdown
    logger.info("Shutting down — cleaning up sessions and pending tasks")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    # Evict all remaining sessions
    for sid in list(sessions.keys()):
        data = sessions.pop(sid, {})
        session_dir = data.get("dir")
        if session_dir and Path(session_dir).exists():
            shutil.rmtree(session_dir, ignore_errors=True)
    logger.info("Shutdown complete")


async def _session_cleanup_loop() -> None:
    """Background task: evict sessions older than SESSION_TTL_SECONDS every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        _evict_expired_sessions()


def _evict_expired_sessions() -> None:
    now = time.time()
    expired = [
        sid for sid, data in list(sessions.items())
        if now - data.get("created_at", 0) > SESSION_TTL_SECONDS
    ]
    for sid in expired:
        data = sessions.pop(sid, {})
        session_dir = data.get("dir")
        if session_dir and Path(session_dir).exists():
            shutil.rmtree(session_dir, ignore_errors=True)


app = FastAPI(title="CSD Converter Web", version="0.2.0", lifespan=lifespan)

# CORS — restrict to same origin by default; override via CORS_ORIGINS env var
_cors_origins = os.environ.get("CORS_ORIGINS", "").split(",")
_cors_origins = [o.strip() for o in _cors_origins if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for Docker HEALTHCHECK and load balancers."""
    return {
        "status": "ok",
        "version": os.environ.get("APP_VERSION", "dev"),
    }


@app.get("/health/ready")
async def health_ready() -> dict[str, str | bool]:
    """Readiness probe — checks downstream dependencies."""
    chorus_ok = False
    if chorus_client.available:
        try:
            chorus_ok = await chorus_client.check_connection()
        except Exception:
            pass
    ai_ok = False
    if ai_client.available:
        try:
            ai_ok = await ai_client.check_connection()
        except Exception:
            pass
    ready = chorus_ok or ai_ok or (not chorus_client.available and not ai_client.available)
    return {"status": "ready" if ready else "degraded", "chorus": chorus_ok, "ai": ai_ok}


templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)

VALID_EXTENSIONS = {".csd", ".lkp"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB per file
MAX_SESSION_SIZE = 500 * 1024 * 1024  # 500 MB total per session

# AI Gateway configuration from environment
ai_client = AIGatewayClient(
    url=os.environ.get("AI_GATEWAY_URL"),
    api_key=os.environ.get("AI_GATEWAY_KEY"),
)
AI_GATEWAY_MODEL = os.environ.get("AI_GATEWAY_MODEL", DEFAULT_MODEL)

_chorus_base = os.environ.get("CHORUS_URL", "")
_chorus_ctx = os.environ.get("CHORUS_CONTEXT", "awdServer")
chorus_client = ChorusV1Client(
    base_url=f"{_chorus_base.rstrip('/')}/{_chorus_ctx}/awd/services/v1" if _chorus_base else None,
    user=os.environ.get("CHORUS_USER"),
    password=os.environ.get("CHORUS_PASSWORD"),
)

# Use system temp dir (cross-platform)
TEMP_BASE = Path(tempfile.gettempdir()) / "conversions"


def _get_or_create_session(request: Request, response: Response) -> tuple[str, dict]:
    """Get existing session or create a new one."""
    session_id = request.cookies.get("csd_session")
    if session_id and session_id in sessions:
        return session_id, sessions[session_id]

    session_id = str(uuid.uuid4())
    session_dir = TEMP_BASE / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    session_data = {"dir": session_dir, "files": [], "created_at": time.time()}
    sessions[session_id] = session_data
    response.set_cookie("csd_session", session_id, httponly=True, samesite="strict", max_age=3600)
    return session_id, session_data


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main page."""
    session_id = request.cookies.get("csd_session")
    files = []
    if session_id and session_id in sessions:
        files = [p.name for p in sessions[session_id]["files"]]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "files": files,
            "ai_configured": ai_client.available,
            "chorus_configured": chorus_client.available,
        },
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, files: list[UploadFile] = File(...)):
    """Handle file upload, return HTMX partial with file list."""
    response = HTMLResponse()
    session_id, session_data = _get_or_create_session(request, response)

    invalid = []
    added = []

    for f in files:
        name = f.filename or "unknown"
        ext = Path(name).suffix.lower()
        if ext not in VALID_EXTENSIONS:
            invalid.append(name)
            continue
        content = await f.read(MAX_FILE_SIZE + 1)
        if len(content) > MAX_FILE_SIZE:
            invalid.append(f"{name} exceeds 50 MB limit")
            continue
        dest = session_data["dir"] / name
        dest.write_bytes(content)
        session_data["files"].append(dest)
        added.append(name)

    # Build HTML fragment
    parts = []
    if invalid:
        for name in invalid:
            safe_name = html.escape(name)
            parts.append(
                f'<div class="file-error">Invalid file type: {safe_name} '
                f"(only .csd and .lkp accepted)</div>"
            )
    for path in session_data["files"]:
        safe_name = html.escape(path.name)
        parts.append(f'<div class="file-item">{safe_name}</div>')

    html_content = "\n".join(parts)
    response = HTMLResponse(content=html_content)
    response.set_cookie("csd_session", session_id, httponly=True, samesite="strict", max_age=3600)
    return response


@app.post("/convert")
async def convert(request: Request):
    """Convert uploaded files and return ZIP."""
    session_id = request.cookies.get("csd_session")
    if not session_id or session_id not in sessions:
        return JSONResponse(
            status_code=400,
            content={"error": "No files uploaded. Please upload files first."},
        )

    session_data = sessions[session_id]
    if not session_data["files"]:
        return JSONResponse(
            status_code=400,
            content={"error": "No files uploaded. Please upload files first."},
        )

    work_dir = session_data["dir"] / "output"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: Parse + optional enrichment
    parsed_forms = []
    parse_errors = []
    for fpath in session_data["files"]:
        try:
            parsed_forms.append(parse_csd_file(fpath))
        except Exception as e:
            logger.error("Failed to parse %s: %s", fpath.name, e)
            parse_errors.append(fpath.name)

    field_cache = {}
    domain_cache = {}

    try:
        body = await request.form()
        use_enrich = body.get("use_enrich") == "true"
        use_ai = body.get("use_ai") == "true"
    except Exception:
        use_enrich = False
        use_ai = False

    # Load cached field knowledge from prior conversions in this session
    cache_path = session_data["dir"] / "_field_cache.json"
    if cache_path.exists():
        try:
            import json as _json
            cached = _json.loads(cache_path.read_text(encoding="utf-8"))
            field_cache = cached.get("fields", {})
            domain_cache = cached.get("domains", {})
            logger.info("Loaded cached field knowledge: %d fields, %d domains",
                        len(field_cache), len(domain_cache))
        except Exception:
            pass

    if use_enrich and chorus_client.available and parsed_forms:
        parsed_forms, field_cache, domain_cache = await enrich_forms(
            parsed_forms, chorus_client,
            existing_field_cache=field_cache,
            existing_domain_cache=domain_cache,
        )
        # Persist enriched cache for future conversions in this session
        try:
            import json as _json
            cache_path.write_text(_json.dumps({
                "fields": field_cache,
                "domains": domain_cache,
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

    # Phase 2: Optional AI agent analysis
    ai_results: Optional[dict] = None
    if use_ai and ai_client.available and parsed_forms:
        ai_results = await analyze_forms(
            parsed_forms,
            field_cache,
            domain_cache,
            gateway_url=os.environ.get("AI_GATEWAY_URL", ""),
            gateway_key=os.environ.get("AI_GATEWAY_KEY", ""),
            model=AI_GATEWAY_MODEL,
            progress_path=session_data["dir"] / "_ai_progress.json",
        )

    zip_bytes = convert_files(session_data["files"], work_dir, ai_results)

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=csd-conversion.zip",
        },
    )


@app.get("/status")
async def status():
    """Check AI Gateway and Chorus connection status."""
    ai_connected = False
    if ai_client.available:
        try:
            ai_connected = await ai_client.check_connection()
        except Exception:
            pass

    chorus_connected = False
    if chorus_client.available:
        try:
            chorus_connected = await chorus_client.check_connection()
        except Exception:
            pass

    return JSONResponse(content={
        "ai_available": ai_client.available,
        "ai_connected": ai_connected,
        "ai_model": AI_GATEWAY_MODEL,
        "chorus_available": chorus_client.available,
        "chorus_connected": chorus_connected,
    })
