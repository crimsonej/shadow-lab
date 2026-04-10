"""
examples/python_client.py
─────────────────────────────────────────────────────────────────────────────
Demonstrates how to use the Ollama API Provider from Python.

Compatible with the openai Python SDK (pip install openai).
Also shows raw httpx usage without any SDK.

Usage:
    pip install openai httpx
    python python_client.py
"""

import asyncio
import json
import os

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
AGENT_URL  = os.getenv("AGENT_URL",  "http://YOUR_SERVER_IP:8080")
API_KEY    = os.getenv("API_KEY",    "sk-your-api-key-here")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "your-admin-token-here")
MODEL      = "llama3:8b"   # Change to any installed model


# ─────────────────────────────────────────────────────────────────────────────
# Example 1: Use with the official OpenAI Python SDK (drop-in replacement)
# ─────────────────────────────────────────────────────────────────────────────
def example_openai_sdk():
    """
    The openai SDK works because the agent exposes OpenAI-compatible endpoints.
    Just point base_url at your agent.
    """
    try:
        from openai import OpenAI
    except ImportError:
        print("Install openai SDK: pip install openai")
        return

    client = OpenAI(
        base_url=f"{AGENT_URL}/v1",
        api_key=API_KEY,
    )

    print("\n=== OpenAI SDK — Non-streaming ===")
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user",   "content": "Explain quantum computing in one sentence."},
        ],
        temperature=0.7,
    )
    print(response.choices[0].message.content)
    print(f"Tokens: {response.usage.total_tokens}")

    print("\n=== OpenAI SDK — Streaming ===")
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Count to 5, one word per line."}],
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        print(delta, end="", flush=True)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Example 2: Raw usage with httpx (no SDK dependency)
# ─────────────────────────────────────────────────────────────────────────────
def example_raw_httpx():
    import httpx

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    print("\n=== Raw httpx — List Models ===")
    r = httpx.get(f"{AGENT_URL}/v1/models", headers=headers)
    models = r.json().get("data", [])
    for m in models:
        print(f"  • {m['id']}")

    print("\n=== Raw httpx — Chat Completion ===")
    r = httpx.post(
        f"{AGENT_URL}/v1/chat/completions",
        headers=headers,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "What is 2+2? Answer with just the number."}],
        },
        timeout=60,
    )
    data = r.json()
    print(data["choices"][0]["message"]["content"])


# ─────────────────────────────────────────────────────────────────────────────
# Example 3: Async streaming with httpx
# ─────────────────────────────────────────────────────────────────────────────
async def example_async_streaming():
    import httpx

    print("\n=== Async Streaming ===")
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{AGENT_URL}/v1/chat/completions",
            headers=headers,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "Write a short poem about the ocean."}],
                "stream": True,
            },
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        content = chunk["choices"][0]["delta"].get("content", "")
                        print(content, end="", flush=True)
                    except json.JSONDecodeError:
                        pass
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Example 4: Admin operations
# ─────────────────────────────────────────────────────────────────────────────
def example_admin():
    import httpx

    admin_headers = {
        "Authorization": f"Bearer {ADMIN_TOKEN}",
        "Content-Type": "application/json",
    }

    print("\n=== Admin — System Metrics ===")
    r = httpx.get(f"{AGENT_URL}/admin/metrics", headers=admin_headers, timeout=10)
    m = r.json()
    print(f"  CPU:  {m.get('cpu_percent', 0):.1f}%")
    print(f"  RAM:  {m.get('ram', {}).get('percent', 0):.1f}%  ({m.get('ram', {}).get('used_gb', 0):.1f}GB used)")
    gpu = m.get("gpu")
    if gpu and gpu.get("gpus"):
        g = gpu["gpus"][0]
        print(f"  GPU:  {g.get('utilization_percent', 0):.0f}%  [{g.get('name', 'unknown')}]")

    print("\n=== Admin — Create API Key ===")
    r = httpx.post(
        f"{AGENT_URL}/admin/keys",
        headers=admin_headers,
        json={"label": "python-example", "limit_rpm": 0},
    )
    key_data = r.json()
    print(f"  Key: {key_data.get('key', 'error')}")
    print(f"  Label: {key_data.get('label')}")

    print("\n=== Admin — List Keys ===")
    r = httpx.get(f"{AGENT_URL}/admin/keys", headers=admin_headers)
    keys = r.json()
    for k in keys:
        status = "✓" if k.get("enabled") else "✗"
        print(f"  [{status}] {k['key'][:16]}… — {k.get('label','—')} — {k.get('requests',0)} requests")


# ─────────────────────────────────────────────────────────────────────────────
# Example 5: Simple multi-turn conversation class
# ─────────────────────────────────────────────────────────────────────────────
class OllamaAPIClient:
    """
    A simple synchronous client that maintains conversation history.
    Works with any OpenAI-compatible endpoint.
    """
    def __init__(self, base_url: str, api_key: str, model: str):
        import httpx
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.Client(timeout=120)
        self.history = []

    def chat(self, message: str, system: str = None) -> str:
        if system and not self.history:
            self.history.append({"role": "system", "content": system})
        self.history.append({"role": "user", "content": message})
        r = self._client.post(
            f"{self.base_url}/v1/chat/completions",
            headers=self.headers,
            json={"model": self.model, "messages": self.history},
        )
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"]
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def reset(self):
        self.history = []


def example_conversation():
    print("\n=== Multi-turn Conversation ===")
    bot = OllamaAPIClient(
        base_url=f"{AGENT_URL}",
        api_key=API_KEY,
        model=MODEL,
    )
    print("User: My name is Alex.")
    reply = bot.chat("My name is Alex.", system="You are a friendly assistant. Keep replies short.")
    print(f"Bot:  {reply}")

    print("User: What's my name?")
    reply = bot.chat("What's my name?")
    print(f"Bot:  {reply}")


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Connecting to: {AGENT_URL}")
    print(f"Model: {MODEL}")

    # Run examples (comment out any you don't need)
    example_openai_sdk()
    example_raw_httpx()
    asyncio.run(example_async_streaming())
    example_admin()
    example_conversation()
