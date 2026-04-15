import os
import multiprocessing
import logging
from typing import Dict, Any

log = logging.getLogger(__name__)

def get_cpu_info() -> Dict[str, Any]:
    """Gather internal hardware metrics."""
    try:
        cores = multiprocessing.cpu_count()
        # On VPS, physical cores might be same as logical, but let's be conservative
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0
        
        # Check for AVX support in cpuinfo
        has_avx = False
        if os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r") as f:
                if "avx" in f.read().lower():
                    has_avx = True
                    
        return {
            "cores": cores,
            "load": load,
            "has_avx": has_avx
        }
    except Exception as e:
        log.warning(f"Hardware detection failed: {e}")
        return {"cores": 4, "load": 0, "has_avx": True}

def get_optimal_options(user_options: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Calculate best settings for Ollama based on real hardware.
    Aims for 'Provider-Grade' performance over 'Safe' defaults.
    """
    cpu = get_cpu_info()
    cores = cpu["cores"]
    
    # Rule 1: num_thread should be physical cores (or cores-1 on busy VPS)
    # We use cores-1 if load is high (> cores) to preserve system stability
    num_thread = max(1, cores - 1) if cpu["load"] > cores else cores
    
    # Rule 2: num_ctx (Context Window)
    # 4096 is a solid default, but we reduce it if cores are low to speed up prefill
    num_ctx = 3072 if cores <= 4 else 4096
    
    # Rule 3: num_batch
    # Smaller batch size can reduce memory spike but hurts throughput.
    # We stay with 512 unless it's a very weak VPS.
    num_batch = 512
    
    options = {
        "num_thread": num_thread,
        "num_ctx": num_ctx,
        "num_batch": num_batch,
        "f16_kv": True,  # High speed KV
        "use_mmap": True, # Fast loading
        "use_mlock": False, # Usually causes issues on VPS without root/swap, but mmap is enough
    }
    
    # Merge with user-provided options (temperature, top_p, etc.)
    if user_options:
        options.update(user_options)
        
    log.info(f"Hardware Optimized: threads={num_thread}, ctx={num_ctx}, load={cpu['load']:.2f}")
    return options

def get_system_metadata() -> Dict[str, Any]:
    """Return a brief host summary for the agent's identity."""
    cpu = get_cpu_info()
    try:
        with open("/proc/meminfo", "r") as f:
            mem_total = 0
            for line in f:
                if "MemTotal" in line:
                    mem_total = int(line.split()[1]) // 1024 # MB
                    break
    except:
        mem_total = 0
        
    return {
        "cores": cpu["cores"],
        "ram_mb": mem_total,
        "os": os.uname().sysname if hasattr(os, "uname") else "Linux",
        "nodename": os.uname().nodename if hasattr(os, "uname") else "shadow-lab-node"
    }
