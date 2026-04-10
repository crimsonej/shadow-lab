from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
import httpx
import json
from config import settings
from auth import verify_api_key, increment_usage

router = APIRouter(prefix="/v1", tags=["openai"])

# Provide OpenAI compat /v1/models list
@router.get("/models")
async def list_openai_models(api_key: str = Depends(verify_api_key)):
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{settings.OLLAMA_HOST}/api/tags")
            r.raise_for_status()
            models = r.json().get("models", [])
            data = []
            for m in models:
                data.append({
                    "id": m["name"],
                    "object": "model",
                    "created": 1686935002, # specific dummy date
                    "owned_by": "shadow-lab"
                })
            increment_usage(api_key)
            return {"object": "list", "data": data}
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Backend provider unavailable")

# Provide OpenAI compat /v1/chat/completions
@router.post("/chat/completions")
async def chat_completions(request: Request, api_key: str = Depends(verify_api_key)):
    body = await request.json()
    model = body.get("model")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "options": {}
    }

    # Extract some options
    if "temperature" in body: payload["options"]["temperature"] = body["temperature"]
    if "max_tokens" in body: payload["options"]["num_predict"] = body["max_tokens"]
    if "top_p" in body: payload["options"]["top_p"] = body["top_p"]

    increment_usage(api_key)

    if not stream:
        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                r = await client.post(f"{settings.OLLAMA_HOST}/api/chat", json=payload)
                r.raise_for_status()
                response_data = r.json()
                
                # Format exactly as OpenAI
                return {
                    "id": f"chatcmpl-{response_data.get('created_at', 'unknown')}",
                    "object": "chat.completion",
                    "created": 1677652288,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": response_data.get("message", {"role": "assistant", "content": ""}),
                        "finish_reason": "stop"
                    }],
                    "usage": {
                        "prompt_tokens": response_data.get("prompt_eval_count", 0),
                        "completion_tokens": response_data.get("eval_count", 0),
                        "total_tokens": response_data.get("prompt_eval_count", 0) + response_data.get("eval_count", 0)
                    }
                }
            except httpx.RequestError as e:
                raise HTTPException(status_code=503, detail=f"Backend provider Error: {e}")
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
    else:
        # Streaming response
        async def generate():
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("POST", f"{settings.OLLAMA_HOST}/api/chat", json=payload) as response:
                    async for chunk in response.aiter_lines():
                        if chunk:
                            try:
                                data = json.loads(chunk)
                                chunk_msg = {
                                    "id": "chatcmpl-stream",
                                    "object": "chat.completion.chunk",
                                    "created": 1677652288,
                                    "model": model,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": data.get("message", {}).get("content", "")},
                                        "finish_reason": "stop" if data.get("done") else None
                                    }]
                                }
                                yield f"data: {json.dumps(chunk_msg)}\n\n"
                            except json.JSONDecodeError:
                                pass
                    yield "data: [DONE]\n\n"
        
        return StreamingResponse(generate(), media_type="text/event-stream")
