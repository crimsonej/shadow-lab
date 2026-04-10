from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import socket
import uvicorn
import logging
from config import settings
from auth import router as auth_router, verify_admin
from ollama import router as ollama_router
from openai_api import router as openai_router
from system import self_healing_worker, get_system_metrics, detect_gpu

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("agent")

# Project: Shadow-Lab
# Created by: crimsonej (https://github.com/crimsonej/crimsonej)

app = FastAPI(
    title="Shadow-Lab AI Provider Agent",
    description="A self-healing, zero-hardcode OpenAI-compatible API Provider by crimsonej",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(ollama_router)
app.include_router(openai_router)

@app.on_event("startup")
async def startup_event():
    # Start the self-healing and monitoring worker
    asyncio.create_task(self_healing_worker())
    logger.info("Self-healing worker initialized.")

@app.get("/system/status", tags=["system"])
async def get_system_status(admin: str = Depends(verify_admin)):
    metrics = get_system_metrics()
    gpu = detect_gpu()
    return {
        "status": "online",
        "cpu": metrics["cpu_percent"],
        "ram": metrics["ram_percent"],
        "ram_used_mb": metrics["ram_used_mb"],
        "ram_total_mb": metrics["ram_total_mb"],
        "gpu": gpu
    }

def find_open_port(start_port: int, max_port: int = 65535) -> int:
    for port in range(start_port, max_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
    raise RuntimeError("No open ports found")

if __name__ == "__main__":
    preferred_port = settings.AGENT_PORT
    try:
        # Try binding to see if we can use the preferred port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((settings.AGENT_HOST, preferred_port))
        s.close()
        port_to_use = preferred_port
    except OSError:
        logger.warning(f"Port {preferred_port} is busy. Hunting for an open port...")
        port_to_use = find_open_port(preferred_port + 1)
        logger.info(f"Dynamically switched to port {port_to_use}")

    # Start the server
    uvicorn.run("main:app", host=settings.AGENT_HOST, port=port_to_use, reload=False)
