#!/usr/bin/env bash
# examples/curl_examples.sh
# ─────────────────────────────────────────────────────────────────────────────
# Replace these with your actual values before running.
# ─────────────────────────────────────────────────────────────────────────────
AGENT_URL="http://YOUR_SERVER_IP:8080"
API_KEY="sk-your-api-key-here"
ADMIN_TOKEN="your-admin-token-here"

echo "=== 1. Health check (no auth required) ==="
curl -s "$AGENT_URL/v1/health" | python3 -m json.tool

echo ""
echo "=== 2. List available models ==="
curl -s "$AGENT_URL/v1/models" \
  -H "Authorization: Bearer $API_KEY" | python3 -m json.tool

echo ""
echo "=== 3. Chat completion (non-streaming) ==="
curl -s "$AGENT_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3:8b",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "temperature": 0.7
  }' | python3 -m json.tool

echo ""
echo "=== 4. Chat completion (streaming) ==="
curl -s "$AGENT_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3:8b",
    "messages": [{"role": "user", "content": "Write a haiku about AI."}],
    "stream": true
  }'

echo ""
echo "=== 5. Create an API key (admin) ==="
curl -s -X POST "$AGENT_URL/admin/keys" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label": "my-app", "limit_rpm": 0}' | python3 -m json.tool

echo ""
echo "=== 6. List API keys (admin) ==="
curl -s "$AGENT_URL/admin/keys" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -m json.tool

echo ""
echo "=== 7. System metrics (admin) ==="
curl -s "$AGENT_URL/admin/metrics" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -m json.tool

echo ""
echo "=== 8. Pull a model (admin) — streams progress ==="
curl -s -X POST "$AGENT_URL/admin/models/pull" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "llama3:8b"}'

echo ""
echo "=== 9. Revoke a key (admin) ==="
curl -s -X POST "$AGENT_URL/admin/keys/revoke" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"key\": \"$API_KEY\"}" | python3 -m json.tool
