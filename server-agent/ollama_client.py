"""
ollama_client.py — Thin async wrapper around the local Ollama HTTP API.

Ollama docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""
import asyncio
import json
from typing import AsyncIterator, List, Optional, Dict, Any

import httpx
import time

import config
import structured_logger
import hardware_optimizer

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
    """Wrapper to try an Ollama POST request with 1x immediate retry on timeout or disconnect."""
    import httpx
    client = get_client()
    try:
        return await client.post(path, json=json_data, timeout=timeout)
    except (httpx.TimeoutException, httpx.ConnectError):
        # 1 Retry
        return await client.post(path, json=json_data, timeout=timeout)

async def _verify_connection() -> None:
    """Check if Ollama is responsive before proceeding."""
    try:
        r = await get_client().get("/api/tags", timeout=3.0)
        r.raise_for_status()
    except Exception:
        raise RuntimeError("ollama_offline")

async def _prepare_model(name: str) -> None:
    """
    Ensure the requested model is prepped for inference.
    If it's already in VRAM, we just note it.
    If not, we issue a load request.
    Ollama handles VRAM eviction of other models automatically.
    """
    loaded_in_vram = await get_loaded_models()
    
    if name in loaded_in_vram:
        config.set_active_model(name)
        return

    # Force load requested model
    try:
        # Step 4: keep_alive=-1 to ensure residency for the controller's duration
        r = await _retry_post("/api/generate", json_data={
            "model": name,
            "keep_alive": -1
        }, timeout=15.0)
        if r.status_code == 200:
            config.set_active_model(name)
            # Update cache of what we know is loaded
            new_loaded = list(loaded_in_vram | {name})
            config.update_loaded_models(new_loaded)
        else:
            raise RuntimeError(f"Failed to load model {name}: {r.text}")
    except Exception as e:
        raise RuntimeError(f"Could not load model {name}: {str(e)}")


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
    """Public wrapper to preload a model."""
    try:
        await _prepare_model(name)
        return True
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

def format_messages(messages: List[Dict[str, str]]) -> str:
    """
    Convert OpenAI-style messages to a plain text prompt.
    User: Hello\nAssistant: Hi\nUser: How are you\nAssistant:
    """
    prompt_parts = []
    for msg in messages:
        role = msg.get("role", "user").capitalize()
        content = msg.get("content", "")
        prompt_parts.append(f"{role}: {content}")
    
    # Prepend 'Assistant: ' to trigger response if not already there
    prompt = "\n".join(prompt_parts)
    if not prompt.endswith("Assistant:"):
        prompt += "\nAssistant:"
    return prompt


async def generate_response(model: str, prompt: str, temperature: float = 0.7) -> Dict[str, Any]:
    """
    Step 4: Unified Generate Call.
    Uses /api/generate with keep_alive=-1 and 10s timeout + retry.
    """
    # Infrastructure Tuning: Get presets for this specific VPS
    presets = hardware_optimizer.get_optimal_options({
        "temperature": temperature
    })

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": presets,
        "keep_alive": -1
    }

    start_time = time.monotonic()
    slog = structured_logger.get_logger()
    
    try:
        # Step 4: 10s timeout + 1 retry
        r = await _retry_post("/api/generate", json_data=payload, timeout=10.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        latency = (time.monotonic() - start_time) * 1000
        slog.error("Ollama Generate Failed", model=model, latency_ms=latency, error=str(e))
        raise RuntimeError(f"Ollama backend failure: {str(e)}")

    latency = (time.monotonic() - start_time) * 1000
    slog.info("Ollama Generate Request", model=model, latency_ms=latency, success=True)
    return data


async def handle_chat_request(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
) -> Dict[str, Any]:
    """
    Step 3: Master Execution Function.
    Optimized for 'Resident Intelligence' identity and hardware tuning.
    """
    # Step 7: Fail-safe check
    await _verify_connection()

    # Step 2: Resolve model
    target_model = model or config.get_active_model()
    if not target_model:
        raise ValueError("no_model_specified")

    # Step 6: Switch model if needed
    await _prepare_model(target_model)

    # Step: Inject Host Identity (Resident Intelligence)
    # Only inject if messages present and first isn't already a system prompt
    if messages and messages[0].get("role") != "system":
        sys_info = hardware_optimizer.get_system_metadata()
        identity = (
            f"You are the Shadow-Lab Resident Intelligence. "
            f"Environment: {sys_info['os']} on {sys_info['nodename']}. "
            f"Hardware: {sys_info['cores']} cores, {sys_info['ram_mb']}MB RAM. "
            f"You are optimized for high-performance sequential inference on this specific VPS."
        )
        messages.insert(0, {"role": "system", "content": identity})

    # Step 2: Convert messages -> prompt
    prompt = format_messages(messages)

    # Step 4: Call Ollama
    try:
        data = await generate_response(target_model, prompt, temperature)
    except Exception as e:
        # Step 8: Never fail silently
        raise RuntimeError(f"inference_failed: {str(e)}")

    # Step 5: OpenAI Style Response
    return {
        "id": f"chatcmpl-{asyncio.get_event_loop().time():.0f}",
        "object": "chat.completion",
        "model": target_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": data.get("response", ""),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
            "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
        },
    }


async def chat_completion(
    model: str,
    messages: List[Dict],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    stream: bool = False,
) -> Dict[str, Any]:
    """
    Non-streaming chat completion (delegates to handle_chat_request).
    """
    # Note: handle_chat_request handles health, model swtiching, and formatting.
    return await handle_chat_request(messages, model=model, temperature=temperature)


async def handle_chat_stream_request(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
) -> AsyncIterator[str]:
    """
    Streaming version of Step 3.
    """
    # Step 7: Fail-safe check
    await _verify_connection()

    # Step 2: Resolve model
    target_model = model or config.get_active_model()
    if not target_model:
        raise ValueError("no_model_specified")

    # Step 6: Switch model if needed
    await _prepare_model(target_model)

    # Step 2: Convert messages -> prompt
    prompt = format_messages(messages)

    payload = {
        "model": target_model,
        "prompt": prompt,
        "stream": True,
        "options": {
            "temperature": temperature
        },
        "keep_alive": -1
    }

    chunk_id = f"chatcmpl-stream-{asyncio.get_event_loop().time():.0f}"
    slog = structured_logger.get_logger()
    start_time = time.monotonic()

    try:
        # Step 4: Call Ollama with simple retry logic for the stream setup
        client = get_client()
        try:
            req = client.build_request("POST", "/api/generate", json=payload)
            resp = await client.send(req, stream=True, timeout=httpx.Timeout(10.0, connect=5.0))
        except (httpx.TimeoutException, httpx.ConnectError):
            req = client.build_request("POST", "/api/generate", json=payload)
            resp = await client.send(req, stream=True, timeout=httpx.Timeout(10.0, connect=5.0))

        resp.raise_for_status()
        
        async for line in resp.aiter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            content = data.get("response", "")
            done = data.get("done", False)

            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "model": target_model,
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
                latency = (time.monotonic() - start_time) * 1000
                slog.info("Ollama Stream Generate Request", model=target_model, latency_ms=latency, success=True)
                break
    except Exception as e:
        latency = (time.monotonic() - start_time) * 1000
        slog.error("Ollama Stream Generate Failed", model=target_model, latency_ms=latency, error=str(e))
        err_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": target_model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"\n[Backend Error: {str(e)}]"}, "finish_reason": "error"}],
        }
        yield f"data: {json.dumps(err_chunk)}\n\n"
        yield "data: [DONE]\n\n"


async def chat_completion_stream(
    model: str,
    messages: List[Dict],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> AsyncIterator[str]:
    """
    Streaming chat completion (delegates to handle_chat_stream_request).
    """
    return handle_chat_stream_request(messages, model=model, temperature=temperature)


# ── Health check ──────────────────────────────────────────────────────────────

async def ping_ollama() -> bool:
    """Return True if Ollama is reachable."""
    try:
        r = await get_client().get("/", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False
