import psutil
import subprocess
import httpx
import asyncio
import logging
from config import settings

logger = logging.getLogger("system")

# Diagnostics
def get_system_metrics():
    sys_mem = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_total_mb": sys_mem.total / (1024 * 1024),
        "ram_used_mb": sys_mem.used / (1024 * 1024),
        "ram_percent": sys_mem.percent,
    }

def detect_gpu():
    # Attempt to detect NVIDIA
    try:
        subprocess.check_output(["nvidia-smi"], stderr=subprocess.STDOUT)
        return "NVIDIA"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Attempt to detect AMD
    try:
        subprocess.check_output(["rocm-smi"], stderr=subprocess.STDOUT)
        return "AMD"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return "CPU"

async def check_ollama_status():
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.get(f"{settings.OLLAMA_HOST}/api/version")
            return r.status_code == 200, r.json().get("version") if r.status_code == 200 else None
        except httpx.RequestError:
            return False, None

def try_auto_fix_ollama():
    logger.info("Attempting to restart Ollama service...")
    try:
        subprocess.run(["systemctl", "restart", "ollama"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to auto-fix Ollama via systemctl: {e}")
        return False
    except FileNotFoundError:
        logger.error("systemctl not found, cannot auto-fix")
        return False

async def self_healing_worker():
    while True:
        try:
            is_up, version = await check_ollama_status()
            if not is_up:
                logger.warning("Ollama daemon is offline. Triggering auto-fix...")
                success = try_auto_fix_ollama()
                if success:
                    logger.info("Ollama auto-fix executed. Waiting to verify.")
                    await asyncio.sleep(5)
                    is_up, _ = await check_ollama_status()
                    if is_up:
                        logger.info("Auto-fix successful.")
                    else:
                        logger.error("Auto-fix failed. Ollama is still down.")
        except Exception as e:
            logger.error(f"Error in self-healing worker: {e}")
        
        await asyncio.sleep(60) # Run every minute
