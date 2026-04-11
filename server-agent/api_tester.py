"""
api_tester.py — End-to-end API integrity testing.

Simulates full OpenAI-style request cycles against the local Ollama backend
to verify the entire inference pipeline is working correctly.
"""
import asyncio
import json
import time
import logging
from typing import Dict, Any, List

import ollama_client

log = logging.getLogger(__name__)


async def _test_ollama_connectivity() -> Dict[str, Any]:
    """Test 1: Verify Ollama is reachable."""
    start = time.monotonic()
    try:
        ok = await ollama_client.ping_ollama()
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "ollama_connectivity",
            "passed": ok,
            "latency_ms": round(elapsed, 1),
            "error": None if ok else "Ollama did not respond to ping",
        }
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "ollama_connectivity",
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": str(e),
        }


async def _test_model_listing() -> Dict[str, Any]:
    """Test 2: Verify model listing works."""
    start = time.monotonic()
    try:
        models = await ollama_client.list_models()
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "model_listing",
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "model_count": len(models),
        }
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "model_listing",
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": str(e),
        }


async def _test_non_streaming(model: str) -> Dict[str, Any]:
    """Test 3: Non-streaming chat completion."""
    start = time.monotonic()
    try:
        result = await ollama_client.chat_completion(
            model=model,
            messages=[{"role": "user", "content": "Say hello in one word."}],
            temperature=0.1,
            max_tokens=16,
        )
        elapsed = (time.monotonic() - start) * 1000

        # Validate response structure
        choices = result.get("choices", [])
        if not choices:
            return {
                "name": "non_streaming_completion",
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": "Response missing 'choices' field",
            }

        content = choices[0].get("message", {}).get("content", "")
        if not content.strip():
            return {
                "name": "non_streaming_completion",
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": "Empty response content",
            }

        return {
            "name": "non_streaming_completion",
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "response_preview": content.strip()[:100],
        }

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "non_streaming_completion",
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": str(e),
        }


async def _test_streaming(model: str) -> Dict[str, Any]:
    """Test 4: Streaming chat completion."""
    start = time.monotonic()
    chunks_received = 0
    content_parts = []
    try:
        async for chunk in ollama_client.chat_completion_stream(
            model=model,
            messages=[{"role": "user", "content": "Say goodbye in one word."}],
            temperature=0.1,
            max_tokens=16,
        ):
            chunks_received += 1
            # Parse SSE chunk to extract content
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                try:
                    data = json.loads(chunk[6:])
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    c = delta.get("content", "")
                    if c:
                        content_parts.append(c)
                except (json.JSONDecodeError, IndexError):
                    pass

        elapsed = (time.monotonic() - start) * 1000

        if chunks_received == 0:
            return {
                "name": "streaming_completion",
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": "No chunks received from stream",
            }

        return {
            "name": "streaming_completion",
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "chunks_received": chunks_received,
            "response_preview": "".join(content_parts).strip()[:100],
        }

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "streaming_completion",
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": str(e),
        }


async def _test_timeout_behavior(model: str) -> Dict[str, Any]:
    """Test 5: Verify the system handles requests within timeout bounds."""
    start = time.monotonic()
    try:
        # This is a quick request; we're testing that it completes, not that it times out
        result = await asyncio.wait_for(
            ollama_client.chat_completion(
                model=model,
                messages=[{"role": "user", "content": "Reply OK."}],
                temperature=0.0,
                max_tokens=4,
            ),
            timeout=60.0,
        )
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "timeout_behavior",
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
        }
    except asyncio.TimeoutError:
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "timeout_behavior",
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "Request timed out after 60 seconds",
        }
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return {
            "name": "timeout_behavior",
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": str(e),
        }


async def test_api_integrity(model: str) -> Dict[str, Any]:
    """
    Run the full API integrity test suite for a given model.

    Returns:
        {
            "status": "PASS" | "FAIL",
            "model": str,
            "tests": [...],
            "passed_count": int,
            "failed_count": int,
            "total_latency_ms": float,
        }
    """
    overall_start = time.monotonic()

    tests: List[Dict[str, Any]] = []

    # Test 1 & 2: Infrastructure tests (no model needed)
    tests.append(await _test_ollama_connectivity())
    tests.append(await _test_model_listing())

    # If Ollama isn't even reachable, skip model-specific tests
    if not tests[0]["passed"]:
        overall_elapsed = (time.monotonic() - overall_start) * 1000
        return {
            "status": "FAIL",
            "model": model,
            "tests": tests,
            "passed_count": sum(1 for t in tests if t["passed"]),
            "failed_count": sum(1 for t in tests if not t["passed"]),
            "total_latency_ms": round(overall_elapsed, 1),
        }

    # Tests 3, 4, 5: Model-specific tests
    tests.append(await _test_non_streaming(model))
    tests.append(await _test_streaming(model))
    tests.append(await _test_timeout_behavior(model))

    overall_elapsed = (time.monotonic() - overall_start) * 1000
    all_passed = all(t["passed"] for t in tests)

    return {
        "status": "PASS" if all_passed else "FAIL",
        "model": model,
        "tests": tests,
        "passed_count": sum(1 for t in tests if t["passed"]),
        "failed_count": sum(1 for t in tests if not t["passed"]),
        "total_latency_ms": round(overall_elapsed, 1),
    }
