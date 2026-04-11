"""
main.py — Shadow-Lab Agent: Inference Provider Server
============================================
Exposes OpenAI-compatible REST endpoints backed by a local Ollama instance.

Endpoints
---------
  Public (API-key protected):
    GET  /v1/models                   list available models
    POST /v1/chat/completions         chat (streaming + non-streaming)
    GET  /v1/health                   health + metrics

  Admin (admin-token protected):
    GET  /admin/keys                  list all API keys
    POST /admin/keys                  create API key
    POST /admin/keys/revoke           disable key
    DELETE /admin/keys                permanently delete key
    GET  /admin/metrics               full system metrics
    POST /admin/models/pull           pull a model from Ollama Hub
    DELETE /admin/models              delete a local model
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import auth
import config
import metrics
import ollama_client
import schemas
import model_tester
import api_tester
import uptime_tracker
import structured_logger

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

AGENT_VERSION = "1.0.0"

# ── Semaphore for concurrent request limiting ─────────────────────────────────
_semaphore: asyncio.Semaphore = None  # type: ignore


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _semaphore
    admin_token = config.ensure_admin_token()
    log.info(f"Server Agent v{AGENT_VERSION} starting on {config.AGENT_HOST}:{config.AGENT_PORT}")
    _semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)
    ok = await ollama_client.ping_ollama()
    if ok:
        log.info("Ollama is reachable ✓")
    else:
        log.warning("Ollama is NOT reachable — requests will fail until it starts")
    # Record boot for uptime tracking
    uptime_tracker.record_boot()
    yield
    # Record shutdown for uptime tracking
    uptime_tracker.record_shutdown()
    client = ollama_client._client
    if client and not client.is_closed:
        await client.aclose()


app = FastAPI(
    title="Shadow-Lab Agent",
    description="Inference provider and control-plane endpoint for Shadow-Lab",
    version=AGENT_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Structured request logging middleware
app.add_middleware(structured_logger.RequestIdMiddleware)


# ── Dependency: validate API key ──────────────────────────────────────────────

def get_api_key(authorization: str = Header(...)):
    """
    Expect header:  Authorization: Bearer sk-xxxx
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header must be 'Bearer <key>'")
    key = authorization.split(" ", 1)[1].strip()
    record = auth.validate_key(key)
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid or disabled API key")
    return key


def require_admin(authorization: str = Header(...)):
    """Require the admin token for management endpoints."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header must be 'Bearer <admin-token>'")
    token = authorization.split(" ", 1)[1].strip()
    if token != config.ensure_admin_token():
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return token


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/v1/health", response_model=schemas.HealthResponse, tags=["Public"])
async def health():
    ok = await ollama_client.ping_ollama()
    m = await metrics.snapshot()
    return {
        "status": "ok" if ok else "degraded",
        "ollama_running": ok,
        "agent_version": AGENT_VERSION,
        "uptime_seconds": m["agent_uptime_seconds"],
    }


# ── Models ────────────────────────────────────────────────────────────────────

@app.get("/v1/models", tags=["Public"])
async def list_models(api_key: str = Depends(get_api_key)):
    """OpenAI-compatible /v1/models response."""
    try:
        models = await ollama_client.list_models()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama error: {e}")
    data = [
        {
            "id": m["name"],
            "object": "model",
            "owned_by": "ollama",
            "created": 0,
            "size": m.get("size", 0),
            "details": m.get("details", {}),
        }
        for m in models
    ]
    return {"object": "list", "data": data}


# ── Chat completions ──────────────────────────────────────────────────────────

@app.post("/v1/chat/completions", tags=["Public"])
async def chat_completions(
    body: schemas.ChatCompletionRequest,
    api_key: str = Depends(get_api_key),
):
    """
    OpenAI-compatible chat completions endpoint.
    Supports both streaming (stream=true) and non-streaming responses.
    """
    messages = [m.model_dump() for m in body.messages]

    async with _semaphore:
        if body.stream:
            async def event_stream() -> AsyncIterator[bytes]:
                prompt_tokens = 0
                completion_tokens = 0
                try:
                    async for chunk in ollama_client.chat_completion_stream(
                        model=body.model,
                        messages=messages,
                        temperature=body.temperature,
                        max_tokens=body.max_tokens,
                    ):
                        yield chunk.encode()
                        # rough token counting for usage
                        completion_tokens += 1
                except Exception as e:
                    log.error(f"Streaming error: {e}")
                finally:
                    auth.record_usage(api_key, tokens_in=prompt_tokens, tokens_out=completion_tokens)

            return StreamingResponse(event_stream(), media_type="text/event-stream")
        else:
            try:
                result = await ollama_client.chat_completion(
                    model=body.model,
                    messages=messages,
                    temperature=body.temperature,
                    max_tokens=body.max_tokens,
                )
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Ollama error: {e}")

            usage = result.get("usage", {})
            auth.record_usage(
                api_key,
                tokens_in=usage.get("prompt_tokens", 0),
                tokens_out=usage.get("completion_tokens", 0),
            )
            return JSONResponse(result)


# ── Admin: metrics ────────────────────────────────────────────────────────────

@app.get("/admin/metrics", tags=["Admin"])
async def admin_metrics(_: str = Depends(require_admin)):
    m = await metrics.snapshot()
    ollama_ok = await ollama_client.ping_ollama()
    m["ollama_running"] = ollama_ok
    return m


# ── Admin: API key management ─────────────────────────────────────────────────

@app.get("/admin/keys", tags=["Admin"])
def admin_list_keys(_: str = Depends(require_admin)):
    return auth.list_keys()


@app.post("/admin/keys", tags=["Admin"])
def admin_create_key(body: schemas.CreateKeyRequest, _: str = Depends(require_admin)):
    return auth.create_key(label=body.label, limit_rpm=body.limit_rpm)


@app.post("/admin/keys/revoke", tags=["Admin"])
def admin_revoke_key(body: schemas.RevokeKeyRequest, _: str = Depends(require_admin)):
    ok = auth.revoke_key(body.key)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"status": "revoked"}


@app.delete("/admin/keys", tags=["Admin"])
def admin_delete_key(body: schemas.DeleteKeyRequest, _: str = Depends(require_admin)):
    ok = auth.delete_key(body.key)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"status": "deleted"}


@app.post("/admin/keys/rotate", tags=["Admin"])
def admin_rotate_key(body: schemas.RevokeKeyRequest, _: str = Depends(require_admin)):
    """Rotate an API key: disable old key and generate a new one with the same metadata."""
    result = auth.rotate_key(body.key)
    if result is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return result


@app.get("/admin/keys/usage", tags=["Admin"])
def admin_key_usage(key: str, _: str = Depends(require_admin)):
    """Get detailed usage stats for a specific API key."""
    usage = auth.get_usage(key)
    if usage is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return usage


# ── Admin: model management ───────────────────────────────────────────────────

@app.post("/admin/models/pull", tags=["Admin"])
async def admin_pull_model(body: schemas.PullModelRequest, _: str = Depends(require_admin)):
    """
    Pull a model from Ollama Hub. Streams progress as NDJSON.
    e.g. POST /admin/models/pull  {"name": "llama3:8b"}
    """
    async def pull_stream():
        async for line in ollama_client.pull_model(body.name):
            yield line + "\n"

    return StreamingResponse(pull_stream(), media_type="application/x-ndjson")


@app.delete("/admin/models", tags=["Admin"])
async def admin_delete_model(body: schemas.DeleteModelRequest, _: str = Depends(require_admin)):
    ok = await ollama_client.delete_model(body.name)
    if not ok:
        raise HTTPException(status_code=404, detail="Model not found or could not be deleted")
    return {"status": "deleted", "model": body.name}


@app.get("/admin/models", tags=["Admin"])
async def admin_list_models(_: str = Depends(require_admin)):
    """List models with full details (no API key needed from dashboard)."""
    try:
        models = await ollama_client.list_models()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama error: {e}")
    return {"models": models}


# ── Control Plane: Model Testing ──────────────────────────────────────────────

@app.post("/admin/test-model", tags=["Admin"])
async def admin_test_model(body: schemas.TestModelRequest, _: str = Depends(require_admin)):
    """Test a specific model to verify it is installed, loadable, and responding."""
    result = await model_tester.test_model(
        model_name=body.name,
        prompt=body.prompt or None,
    )
    return result


@app.get("/admin/models/health", tags=["Admin"])
async def admin_models_health(_: str = Depends(require_admin)):
    """Run a health check on all installed models."""
    results = await model_tester.health_check_all_models()
    passed = sum(1 for r in results if r.get("status") == "pass")
    return {
        "total_models": len(results),
        "healthy": passed,
        "unhealthy": len(results) - passed,
        "models": results,
    }


# ── Control Plane: API Integrity Testing ──────────────────────────────────────

@app.post("/admin/test-api", tags=["Admin"])
async def admin_test_api(body: schemas.TestApiRequest, _: str = Depends(require_admin)):
    """Run the full API integrity test suite against a specific model."""
    result = await api_tester.test_api_integrity(model=body.model)
    return result


# ── Control Plane: Uptime & Runtime ───────────────────────────────────────────

@app.get("/metrics/uptime", tags=["Metrics"])
async def metrics_uptime(_: str = Depends(require_admin)):
    """Return current session uptime and boot information."""
    return uptime_tracker.get_uptime()


@app.get("/metrics/monthly-runtime", tags=["Metrics"])
async def metrics_monthly_runtime(_: str = Depends(require_admin)):
    """Return monthly runtime accumulation data."""
    return uptime_tracker.get_monthly_runtime()


# ── Control Plane: Server Lifecycle ───────────────────────────────────────────

@app.post("/admin/lifecycle/restart-ollama", tags=["Admin"])
async def admin_restart_ollama(_: str = Depends(require_admin)):
    """Restart the local Ollama service via systemctl."""
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "restart", "ollama"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return {"status": "ok", "message": "Ollama restart issued"}
        else:
            return {"status": "error", "message": result.stderr.strip() or "Non-zero exit code"}
    except FileNotFoundError:
        raise HTTPException(503, "systemctl not available on this system")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Timeout waiting for Ollama restart")


@app.get("/admin/lifecycle/status", tags=["Admin"])
async def admin_lifecycle_status(_: str = Depends(require_admin)):
    """Return Ollama and agent process status."""
    import subprocess
    ollama_ok = await ollama_client.ping_ollama()

    # Try to get systemd status
    ollama_systemd = "unknown"
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "ollama"],
            capture_output=True, text=True, timeout=5,
        )
        ollama_systemd = result.stdout.strip()
    except Exception:
        pass

    return {
        "ollama_reachable": ollama_ok,
        "ollama_systemd_status": ollama_systemd,
        "agent_version": AGENT_VERSION,
        "agent_uptime": uptime_tracker.get_uptime(),
    }


# ── Control Plane: Structured Logs ────────────────────────────────────────────

@app.get("/logs/recent", tags=["Logs"])
async def logs_recent(limit: int = 100, _: str = Depends(require_admin)):
    """Return the most recent structured log entries."""
    limit = min(max(limit, 1), 500)
    return {"entries": structured_logger.get_recent_logs(limit)}


@app.get("/logs/errors", tags=["Logs"])
async def logs_errors(limit: int = 50, _: str = Depends(require_admin)):
    """Return recent ERROR-level log entries."""
    limit = min(max(limit, 1), 200)
    return {"entries": structured_logger.get_error_logs(limit)}


# ── Error handling ────────────────────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.detail, "type": "api_error", "code": exc.status_code}},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal server error", "type": "internal_error", "code": 500}},
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.AGENT_HOST,
        port=config.AGENT_PORT,
        reload=False,
        log_level="info",
    )
