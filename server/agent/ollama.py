import httpx
from fastapi import APIRouter, Depends, HTTPException
from config import settings
from auth import verify_admin

router = APIRouter(prefix="/v1/admin/models", tags=["admin/models"])

async def get_ollama_models():
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{settings.OLLAMA_HOST}/api/tags")
            r.raise_for_status()
            return r.json().get("models", [])
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Ollama daemon unreachable")

@router.get("")
async def list_models(admin: str = Depends(verify_admin)):
    models = await get_ollama_models()
    return {"models": models}

@router.post("/pull")
async def pull_model(model: str, admin: str = Depends(verify_admin)):
    # Stream is mostly false for simpler proxying in API
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            r = await client.post(f"{settings.OLLAMA_HOST}/api/pull", json={"name": model, "stream": False})
            r.raise_for_status()
            return {"status": "success", "detail": f"Model {model} pulled successfully"}
        except httpx.TimeoutException:
            return {"status": "processing", "detail": f"Model {model} pull in progress"}
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Failed to pull model via Ollama")

@router.delete("/{model_name}")
async def delete_model(model_name: str, admin: str = Depends(verify_admin)):
    async with httpx.AsyncClient() as client:
        try:
            # Note: The Ollama API uses {"name": "model_name"} in the body for delete
            r = await client.request("DELETE", f"{settings.OLLAMA_HOST}/api/delete", json={"name": model_name})
            r.raise_for_status()
            return {"status": "success", "detail": f"Model {model_name} deleted successfully"}
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Failed to delete model")
