"""
metrics.py — Lightweight system resource monitoring.

Collects CPU, RAM, disk, and GPU (NVIDIA via nvidia-smi / AMD via rocm-smi).
"""
import asyncio
import shutil
import subprocess
import time
from typing import Dict, Any, Optional

import psutil


# ── CPU / RAM / Disk ─────────────────────────────────────────────────────────

def cpu_percent() -> float:
    return psutil.cpu_percent(interval=0.1)


def ram_info() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    return {
        "total_gb": round(vm.total / 1e9, 2),
        "used_gb": round(vm.used / 1e9, 2),
        "free_gb": round(vm.available / 1e9, 2),
        "percent": vm.percent,
    }


def disk_info(path: str = "/") -> Dict[str, Any]:
    du = psutil.disk_usage(path)
    return {
        "total_gb": round(du.total / 1e9, 2),
        "used_gb": round(du.used / 1e9, 2),
        "free_gb": round(du.free / 1e9, 2),
        "percent": du.percent,
    }


# ── GPU monitoring ────────────────────────────────────────────────────────────

def _run(cmd: list) -> Optional[str]:
    """Run a subprocess, return stdout or None on error."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def gpu_info_nvidia() -> Optional[Dict[str, Any]]:
    """Query nvidia-smi for GPU stats."""
    if not shutil.which("nvidia-smi"):
        return None
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return None
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append({
                "name": parts[0],
                "utilization_percent": float(parts[1]),
                "memory_used_mb": float(parts[2]),
                "memory_total_mb": float(parts[3]),
                "temperature_c": float(parts[4]),
                "vendor": "nvidia",
            })
        except ValueError:
            pass
    return {"gpus": gpus} if gpus else None


def gpu_info_amd() -> Optional[Dict[str, Any]]:
    """Query rocm-smi for AMD GPU stats."""
    if not shutil.which("rocm-smi"):
        return None
    out = _run(["rocm-smi", "--showuse", "--showmeminfo", "vram", "--csv"])
    if not out:
        return None
    # Simplified: just report presence
    return {"gpus": [{"vendor": "amd", "raw": out[:200]}]}


def gpu_info() -> Optional[Dict[str, Any]]:
    return gpu_info_nvidia() or gpu_info_amd()


# ── Uptime ────────────────────────────────────────────────────────────────────

_start_time = time.time()


def uptime_seconds() -> float:
    return time.time() - _start_time


def system_uptime_seconds() -> float:
    return time.time() - psutil.boot_time()


# ── Aggregate snapshot ────────────────────────────────────────────────────────

async def snapshot() -> Dict[str, Any]:
    """Return a full metrics snapshot (async-friendly)."""
    loop = asyncio.get_event_loop()
    # offload blocking calls to thread pool
    cpu = await loop.run_in_executor(None, cpu_percent)
    ram = await loop.run_in_executor(None, ram_info)
    disk = await loop.run_in_executor(None, disk_info)
    gpu = await loop.run_in_executor(None, gpu_info)

    return {
        "cpu_percent": cpu,
        "ram": ram,
        "disk": disk,
        "gpu": gpu,
        "agent_uptime_seconds": uptime_seconds(),
        "system_uptime_seconds": system_uptime_seconds(),
    }
