"""
model_tester.py — AI model verification and health-check logic.

Provides functions to:
  - Test a single model with a prompt and measure latency/token usage
  - Health-check all installed models in a single pass
"""
import asyncio
import time
import logging
from typing import Dict, Any, List, Optional

import config
import ollama_client

log = logging.getLogger(__name__)


async def test_model(
    model_name: str,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a test prompt to a specific model via Ollama and return diagnostics.

    Returns:
        {
            "model": str,
            "status": "pass" | "fail",
            "response_text": str | None,
            "latency_ms": float,
            "tokens": {"prompt": int, "completion": int, "total": int} | None,
            "error": str | None,
        }
    """
    prompt = prompt or config.TEST_PROMPT
    start = time.monotonic()

    try:
        result = await ollama_client.chat_completion(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # Low temperature for deterministic health checks
            max_tokens=64,
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        content = ""
        usage = result.get("usage", {})
        choices = result.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")

        return {
            "model": model_name,
            "status": "pass",
            "response_text": content.strip(),
            "latency_ms": round(elapsed_ms, 1),
            "tokens": {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
                "total": usage.get("total_tokens", 0),
            },
            "error": None,
        }

    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        log.warning(f"Model test failed for {model_name}: {e}")
        return {
            "model": model_name,
            "status": "fail",
            "response_text": None,
            "latency_ms": round(elapsed_ms, 1),
            "tokens": None,
            "error": str(e),
        }


async def health_check_all_models() -> List[Dict[str, Any]]:
    """
    Run a quick health check on every locally installed model.

    Returns a list of test results, one per model.
    Models that fail to respond get status="fail".
    """
    try:
        models = await ollama_client.list_models()
    except Exception as e:
        log.error(f"Cannot list models for health check: {e}")
        return [{"model": "unknown", "status": "fail", "error": f"Cannot list models: {e}"}]

    if not models:
        return []

    # Run tests concurrently with a concurrency cap to avoid overloading Ollama
    semaphore = asyncio.Semaphore(2)

    async def _guarded_test(model_name: str) -> Dict[str, Any]:
        async with semaphore:
            return await test_model(model_name)

    tasks = [_guarded_test(m["name"]) for m in models]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert any exceptions to fail records
    final = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            final.append({
                "model": models[i]["name"],
                "status": "fail",
                "response_text": None,
                "latency_ms": 0,
                "tokens": None,
                "error": str(r),
            })
        else:
            final.append(r)

    return final
