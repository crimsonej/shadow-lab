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
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
    return _client


# ── Model management ──────────────────────────────────────────────────────────

async def list_models() -> List[Dict[str, Any]]:
    """Return list of locally available models."""
    r = await get_client().get("/api/tags")
    r.raise_for_status()
    return r.json().get("models", [])


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

    r = await get_client().post("/api/chat", json=payload)
    r.raise_for_status()
    data = r.json()

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

    async with get_client().stream("POST", "/api/chat", json=payload) as resp:
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


# ── Health check ──────────────────────────────────────────────────────────────

async def ping_ollama() -> bool:
    """Return True if Ollama is reachable."""
    try:
        r = await get_client().get("/", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False
