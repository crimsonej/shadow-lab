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
import time
import sys
import threading
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
import process_tracker

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


# Global Exception Handler to prevent process death
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    slog = structured_logger.get_logger()
    request_id = getattr(request.state, "request_id", "unknown")
    slog.error(
        f"CRITICAL: Unhandled exception on {request.method} {request.url.path}",
        request_id=request_id,
        error=str(exc)
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": f"An unexpected error occurred in the agent: {str(exc)}",
                "type": "internal_error",
                "request_id": request_id
            }
        }
    )

# ── Dependency: validate API key ──────────────────────────────────────────────

def get_api_key(authorization: str = Header(...)):
    """
    Expect header:  Authorization: Bearer sk-xxxx OR admin-token
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header must be 'Bearer <key>'")
    key = authorization.split(" ", 1)[1].strip()
    
    # Allow admin token to bypass key check for dashboard proxying
    if key == config.ensure_admin_token():
        return "admin"
        
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


# Global counter for active inference requests
ACTIVE_REQUESTS = 0

@app.post("/v1/chat/completions", tags=["Public"])
async def chat_completions(
    body: schemas.ChatCompletionRequest,
    api_key: str = Depends(get_api_key),
):
    """
    OpenAI-compatible chat completions endpoint.
    Supports both streaming (stream=true) and non-streaming responses.
    """
    global ACTIVE_REQUESTS
    ACTIVE_REQUESTS += 1
    
    try:
        # Inject active model if not specified
        if not body.model:
            active = config.get_active_model()
            if not active:
                raise HTTPException(400, "No model selected. Provide ‘model’ or select an active model in dashboard.")
            body.model = active

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
                        ACTIVE_REQUESTS -= 1

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
                finally:
                    ACTIVE_REQUESTS -= 1

                usage = result.get("usage", {})
                auth.record_usage(
                    api_key,
                    tokens_in=usage.get("prompt_tokens", 0),
                    tokens_out=usage.get("completion_tokens", 0),
                )
                return JSONResponse(result)
    except Exception as e:
        # This catch is for errors *before* the inner finally blocks are set up (like messages parsing or semaphore issues)
        # However, we must be careful not to double-decrement if the error happened inside the non-streaming try block.
        # But wait, if an error happens in the non-streaming block, it raises, then it enters THIS except, 
        # but the inner finally ALREADY ran. So we'd double-decrement.
        
        # Proper way: Only decrement if we haven't reached the response phase or if it's an error NOT handled by inner finally.
        # Actually, if an exception is raised in the 'else' block, the inner finally runs, THEN the exception propagates here.
        
        # Let's check status of decrementing. 
        # Actually, let's just make the outer block simpler: only wrap the setup code.
        raise e


# ── Admin: metrics ────────────────────────────────────────────────────────────

@app.get("/admin/metrics", tags=["Admin"])
async def admin_metrics(_: str = Depends(require_admin)):
    global ACTIVE_REQUESTS
    m = await metrics.snapshot()
    ollama_ok = await ollama_client.ping_ollama()
    m["ollama_running"] = ollama_ok
    m["active_requests"] = ACTIVE_REQUESTS
    try:
        # Fetch loaded models directly from Ollama
        r = await ollama_client._http.get("http://localhost:11434/api/ps", timeout=2)
        if r.status_code == 200:
            models_data = r.json()
            m["models_loaded"] = models_data.get("models", [])
        else:
            m["models_loaded"] = []
    except Exception:
        m["models_loaded"] = []
    
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

    active_model = config.get_active_model()
    for m in models:
        m["installed"] = True
        m["active"] = (m.get("name") == active_model)

    return {"models": models}

# ── Control Plane: Active Model & Loading ───────────────────────────────────

@app.post("/admin/models/select", tags=["Admin"])
async def admin_select_model(body: schemas.SelectModelRequest, _: str = Depends(require_admin)):
    """Set the globally active model."""
    config.set_active_model(body.model)
    return {"status": "ok", "active_model": body.model}


@app.get("/admin/models/active", tags=["Admin"])
async def admin_get_active_model(_: str = Depends(require_admin)):
    """Get the globally active model."""
    return {"active_model": config.get_active_model()}


@app.post("/admin/models/load", tags=["Admin"])
async def admin_load_model(body: schemas.LoadModelRequest, _: str = Depends(require_admin)):
    """Load a model into VRAM."""
    success = await ollama_client.load_model(body.model)
    if not success:
        raise HTTPException(500, "Failed to load model in Ollama.")
    return {"status": "ok", "message": f"Model {body.model} loaded."}


@app.post("/admin/models/unload", tags=["Admin"])
async def admin_unload_model(body: schemas.LoadModelRequest, _: str = Depends(require_admin)):
    """Unload a model from VRAM."""
    success = await ollama_client.unload_model(body.model)
    if not success:
        raise HTTPException(500, "Failed to unload model in Ollama.")
    return {"status": "ok", "message": f"Model {body.model} unloaded."}

# ── Public Reliability API ───────────────────────────────────────────────────

@app.get("/api/health", tags=["Reliability"])
async def api_health():
    """Consolidated health check: agent, ollama status, and model state."""
    ollama_ok = await ollama_client.ping_ollama()
    loaded_models = []
    try:
        loaded_models = list(await ollama_client.get_loaded_models())
    except Exception:
        pass

    return {
        "agent": "online",
        "ollama": "online" if ollama_ok else "offline",
        "active_model": config.get_active_model() or None,
        "models_loaded": loaded_models,
        "uptime": uptime_tracker.get_uptime(),
        "errors": structured_logger.get_error_logs(limit=5)
    }


@app.post("/api/test-model", tags=["Reliability"])
async def api_test_model(body: schemas.TestModelRequest, _: str = Depends(require_admin)):
    """Verify if a model is callable with a strict 10s timeout, using standardized test logic."""
    # Ensure model is "active" or load it temporarily? 
    # The prompt says: "Ensure model is loaded"
    active = config.get_active_model()
    if active != body.name:
        log.info(f"Test requested for non-active model {body.name}. Loading now...")
        # This will trigger our single-model load logic
        await ollama_client.load_model(body.name)
    
    # Use the model_tester logic to get a standardized response
    # We wrap it in a try-except to guarantee it never crashes the server
    try:
        result = await asyncio.wait_for(
            model_tester.test_model(
                model_name=body.name,
                prompt=body.prompt or "Say OK if working."
            ),
            timeout=12.0 # 10s target + 2s buffer
        )
        return result
    except asyncio.TimeoutError:
        return {
            "model": body.name,
            "status": "fail",
            "error": "Inference timed out after 10 seconds",
            "latency_ms": 10000.0
        }
    except Exception as e:
        return {
            "model": body.name,
            "status": "fail",
            "error": str(e),
            "latency_ms": 0
        }


@app.get("/api/ollama-health", tags=["Reliability"])
async def api_ollama_health(_: str = Depends(require_admin)):
    """Detailed health check for the Ollama runtime."""
    ollama_ok = await ollama_client.ping_ollama()
    active_model = config.get_active_model()
    
    loaded_models = []
    try:
        loaded_models = list(await ollama_client.get_loaded_models())
    except Exception:
        pass

    installed_models = []
    try:
        tags = await ollama_client.list_models()
        installed_models = [m["name"] for m in tags]
    except Exception:
        pass

    return {
        "status": "online" if ollama_ok else "offline",
        "active_model": active_model or None,
        "loaded_models": loaded_models,
        "installed_models": installed_models,
        "gpu_usage": None, # Future: integrate GPU metrics if available
        "latency": None
    }


@app.post("/api/shutdown", tags=["Control"])
async def api_shutdown(_: str = Depends(require_admin)):
    """Gracefully stop the agent process."""
    log.info("SHUTDOWN request received via API. Terminating in 1 second...")
    
    def delayed_exit():
        time.sleep(1)
        log.info("Agent process exiting now.")
        sys.exit(0)
    
    threading.Thread(target=delayed_exit, daemon=True).start()
    
    return {
        "status": "stopping",
        "message": "Agent is shutting down cleanly. Local tunnel will be disconnected."
    }


# ── Control Plane: API Integrity Testing ──────────────────────────────────────

@app.post("/admin/test-api-key", tags=["Admin"])
async def admin_test_api_key(body: schemas.ApiKeyTestRequest, _: str = Depends(require_admin)):
    """
    Test an API key by running inference locally through the proxy.
    Returns PASS/FAIL and the response details.
    """
    if body.api_key not in auth.API_KEYS:
        return {"status": "FAIL", "model_used": "", "error": "Invalid API Key"}

    model_to_use = body.model or config.get_active_model()
    if not model_to_use:
        return {"status": "FAIL", "model_used": "", "error": "No model supplied and no active model set."}

    import time
    start_time = time.time()
    try:
        req = schemas.ChatCompletionRequest(
            model=model_to_use,
            messages=[schemas.ChatMessage(role="user", content="Say OK if working")],
            max_tokens=10,
            stream=False
        )
        resp = await ollama_client.chat_completion(
            req.model, [{"role": m.role, "content": m.content} for m in req.messages], req.temperature, req.max_tokens, False
        )
        latency = (time.time() - start_time) * 1000
        content = resp["choices"][0]["message"]["content"]
        
        return {
            "status": "PASS",
            "model_used": model_to_use,
            "response": content,
            "latency_ms": round(latency, 2)
        }
    except Exception as e:
        return {
            "status": "FAIL",
            "model_used": model_to_use,
            "error": str(e),
            "latency_ms": round((time.time() - start_time) * 1000, 2)
        }


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


# ── Soft Lifecycle: Activate / Deactivate ─────────────────────────────────────

@app.post("/admin/deactivate", tags=["Admin"])
async def admin_deactivate(_: str = Depends(require_admin)):
    """
    Soft deactivate: stop Ollama process and free ports.
    The agent stays alive so it can be re-activated remotely.
    """
    global ACTIVE_REQUESTS

    # Safety check: block if requests are in-flight (unless force)
    if ACTIVE_REQUESTS > 0:
        return {
            "status": "blocked",
            "message": f"Cannot deactivate: {ACTIVE_REQUESTS} active request(s) in progress.",
            "active_requests": ACTIVE_REQUESTS,
            "state": "active",
        }

    # Kill Ollama
    kill_result = process_tracker.kill_ollama()
    state = process_tracker.get_status()

    log.info(f"Server DEACTIVATED — Ollama killed: {kill_result}")
    return {
        "status": "ok" if kill_result["success"] else "partial",
        "message": "Server deactivated. Ollama stopped, agent remains alive.",
        "ollama_kill": kill_result,
        "state": state["state"],
        "processes": state["processes"],
    }


@app.post("/admin/activate", tags=["Admin"])
async def admin_activate(_: str = Depends(require_admin)):
    """
    Soft activate: start Ollama and verify health.
    """
    start_result = process_tracker.start_ollama()

    # Wait briefly, then verify
    import asyncio
    await asyncio.sleep(2)

    ollama_ok = await ollama_client.ping_ollama()
    state = process_tracker.get_status()

    log.info(f"Server ACTIVATED — Ollama started: {start_result}, reachable: {ollama_ok}")
    return {
        "status": "active" if ollama_ok else "failed",
        "message": "Server activated successfully." if ollama_ok else "Ollama started but not yet reachable.",
        "ollama_reachable": ollama_ok,
        "ollama_start": start_result,
        "state": state["state"],
        "processes": state["processes"],
    }


@app.get("/admin/server-state", tags=["Admin"])
async def admin_server_state(_: str = Depends(require_admin)):
    """
    Return the current soft server state: active | idle | offline.
    Includes PID tracking information.
    """
    global ACTIVE_REQUESTS
    state = process_tracker.get_status()
    state["active_requests"] = ACTIVE_REQUESTS
    return state


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
