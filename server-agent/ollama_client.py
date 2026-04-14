"""
ollama_client.py — Thin async wrapper around the local Ollama HTTP API.

Ollama docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""
import asyncio
import json
from typing import AsyncIterator, List, Optional, Dict, Any

import httpx

import config

# Shared async client (connection-pooled)
_client: Optional[httpx.AsyncClient] = None

def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=config.OLLAMA_BASE_URL,
            timeout=httpx.Timeout(10.0), # Stricter timeout to prevent hanging
        )
    return _client

async def _retry_post(path: str, json_data: Dict[str, Any], timeout: float = 10.0) -> httpx.Response:
    """Wrapper to try an Ollama POST request with 1x immediate retry on timeout."""
    import httpx
    client = get_client()
    try:
        return await client.post(path, json=json_data, timeout=timeout)
    except (httpx.TimeoutException, httpx.ConnectError):
        # 1 Retry
        return await client.post(path, json=json_data, timeout=timeout)


# ── Model management ──────────────────────────────────────────────────────────

async def list_models() -> List[Dict[str, Any]]:
    """Return list of locally available models, enriched with active/loaded state."""
    # Note: caller will resolve 'active' label, we just supply 'loaded' here
    try:
        r = await get_client().get("/api/tags")
        r.raise_for_status()
        base_models = r.json().get("models", [])
    except Exception:
        return []

    # Get loaded models from /api/ps
    try:
        rps = await get_client().get("/api/ps")
        rps.raise_for_status()
        loaded_names = {m.get("name") for m in rps.json().get("models", [])}
    except Exception:
        loaded_names = set()

    for m in base_models:
        m["loaded"] = m.get("name") in loaded_names

    return base_models

async def get_loaded_models() -> set[str]:
    """Helper to return currently loaded model names."""
    try:
        rps = await get_client().get("/api/ps", timeout=5.0)
        rps.raise_for_status()
        return {m.get("name") for m in rps.json().get("models", [])}
    except Exception:
        return set()

async def load_model(name: str) -> bool:
    """Pre-load a model into VRAM while strictly enforcing single-model active state."""
    # Enforce unloading of any previously loaded models according to current state
    active = config.get_active_model()
    if active and active != name:
        await unload_model(active)
    
    # Also check if anything else is lingering in VRAM
    loaded = await get_loaded_models()
    for mod in loaded:
        if mod != name:
            await unload_model(mod)

    try:
        r = await _retry_post("/api/generate", json_data={
            "model": name,
            "keep_alive": -1  # Load and keep indefinitely until unloaded
        }, timeout=15.0)
        
        if r.status_code == 200:
            config.set_active_model(name)
            config.update_loaded_models([name])
            return True
        return False
    except Exception:
        return False


async def unload_model(name: str) -> bool:
    """Unload a model from VRAM."""
    try:
        r = await _retry_post("/api/generate", json_data={
            "model": name,
            "keep_alive": 0
        }, timeout=5.0)
        
        if r.status_code == 200:
            if config.get_active_model() == name:
                config.set_active_model("")
            
            loaded = [m for m in config.get_loaded_models() if m != name]
            config.update_loaded_models(loaded)
            return True
        return False
    except Exception:
        return False



async def pull_model(name: str) -> AsyncIterator[str]:
    """Stream pull progress lines (NDJSON) for a model."""
    async with get_client().stream("POST", "/api/pull", json={"name": name}) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if line:
                yield line


async def delete_model(name: str) -> bool:
    r = await get_client().delete("/api/delete", json={"name": name})
    return r.status_code == 200


async def show_model(name: str) -> Dict[str, Any]:
    r = await get_client().post("/api/show", json={"name": name})
    r.raise_for_status()
    return r.json()


# ── Chat / completions ────────────────────────────────────────────────────────

def _openai_messages_to_ollama(messages: List[Dict]) -> List[Dict]:
    """
    OpenAI message format → Ollama chat format.
    Ollama uses role/content identical to OpenAI for chat.
    """
    return messages  # already compatible


async def chat_completion(
    model: str,
    messages: List[Dict],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    stream: bool = False,
) -> Dict[str, Any]:
    """
    Non-streaming chat completion.
    Returns an OpenAI-compatible response dict.
    """
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _openai_messages_to_ollama(messages),
        "stream": False,
        "options": {"temperature": temperature},
    }
    if max_tokens:
        payload["options"]["num_predict"] = max_tokens

    try:
        r = await _retry_post("/api/chat", json_data=payload, timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"Ollama backend failure: {str(e)}")

    # Build OpenAI-compatible response
    return {
        "id": f"chatcmpl-{asyncio.get_event_loop().time():.0f}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": data["message"]["content"],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
            "total_tokens": (
                data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
            ),
        },
    }


async def chat_completion_stream(
    model: str,
    messages: List[Dict],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> AsyncIterator[str]:
    """
    Streaming chat completion.
    Yields Server-Sent Event strings in OpenAI format.
    """
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _openai_messages_to_ollama(messages),
        "stream": True,
        "options": {"temperature": temperature},
    }
    if max_tokens:
        payload["options"]["num_predict"] = max_tokens

    chunk_id = f"chatcmpl-stream-{asyncio.get_event_loop().time():.0f}"
    import httpx

    try:
        async with get_client().stream("POST", "/api/chat", json=payload, timeout=httpx.Timeout(15.0, connect=5.0)) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                content = data.get("message", {}).get("content", "")
                done = data.get("done", False)

                chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": content},
                            "finish_reason": "stop" if done else None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                if done:
                    yield "data: [DONE]\n\n"
                    break
    except Exception as e:
        # Gracefully handle the error by emitting an SSE encoded error string
        err_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"\n[Backend Error: {str(e)}]"}, "finish_reason": "error"}],
        }
        yield f"data: {json.dumps(err_chunk)}\n\n"
        yield "data: [DONE]\n\n"


# ── Health check ──────────────────────────────────────────────────────────────

async def ping_ollama() -> bool:
    """Return True if Ollama is reachable."""
    try:
        r = await get_client().get("/", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False
