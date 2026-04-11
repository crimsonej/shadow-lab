"""
process_tracker.py — Track PIDs of Ollama and agent processes for precise lifecycle control.

Provides OS-aware process discovery and targeted kill instead of blind pkill.
Works across Linux, macOS, and Windows.
"""
import json
import logging
import os
import platform
import signal
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List

log = logging.getLogger(__name__)

PID_FILE = Path(__file__).parent / ".shadow_pids.json"


def _load_pids() -> Dict[str, Any]:
    """Load stored PID data from disk."""
    if PID_FILE.exists():
        try:
            return json.loads(PID_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_pids(data: Dict[str, Any]):
    """Persist PID data to disk."""
    try:
        PID_FILE.write_text(json.dumps(data, indent=2))
    except IOError as e:
        log.warning(f"Could not write PID file: {e}")


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)  # Signal 0 = existence check, no actual signal sent
        return True
    except (OSError, ProcessLookupError):
        return False


def _find_ollama_pid() -> Optional[int]:
    """Discover the Ollama server process PID by scanning running processes."""
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq ollama.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                parts = line.replace('"', '').split(',')
                if len(parts) >= 2 and "ollama" in parts[0].lower():
                    return int(parts[1])
        else:
            # Linux / macOS
            result = subprocess.run(
                ["pgrep", "-f", "ollama serve"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().splitlines()[0])
    except Exception as e:
        log.debug(f"Ollama PID discovery failed: {e}")
    return None


def _find_agent_pid() -> int:
    """Return the current agent process PID (ourselves)."""
    return os.getpid()


def discover() -> Dict[str, Any]:
    """
    Discover current process state and return a snapshot.
    Returns:
        {
            "agent_pid": int | null,
            "agent_alive": bool,
            "ollama_pid": int | null,
            "ollama_alive": bool,
        }
    """
    agent_pid = _find_agent_pid()
    ollama_pid = _find_ollama_pid()

    state = {
        "agent_pid": agent_pid,
        "agent_alive": _is_pid_alive(agent_pid) if agent_pid else False,
        "ollama_pid": ollama_pid,
        "ollama_alive": _is_pid_alive(ollama_pid) if ollama_pid else False,
    }

    _save_pids(state)
    return state


def kill_ollama() -> Dict[str, Any]:
    """Kill the Ollama process. Uses precise PID if known, falls back to pkill."""
    system = platform.system()

    # Try precise PID kill first
    ollama_pid = _find_ollama_pid()
    if ollama_pid:
        try:
            if system == "Windows":
                subprocess.run(
                    ["taskkill", "/PID", str(ollama_pid), "/F"],
                    capture_output=True, timeout=10,
                )
            else:
                os.kill(ollama_pid, signal.SIGTERM)
            log.info(f"Killed Ollama (PID {ollama_pid})")
            return {"success": True, "pid": ollama_pid, "method": "pid"}
        except Exception as e:
            log.warning(f"PID kill failed for Ollama ({ollama_pid}): {e}")

    # Fallback: broad kill
    try:
        if system == "Windows":
            subprocess.run(
                ["taskkill", "/IM", "ollama.exe", "/F"],
                capture_output=True, timeout=10,
            )
        else:
            # Try stopping the service first, then pkill as fallback
            subprocess.run(["sudo", "systemctl", "stop", "ollama"],
                           capture_output=True, timeout=10)
            subprocess.run(["pkill", "-f", "ollama"],
                           capture_output=True, timeout=5)
        return {"success": True, "pid": None, "method": "fallback"}
    except Exception as e:
        log.error(f"Ollama kill failed completely: {e}")
        return {"success": False, "pid": None, "method": "failed", "error": str(e)}


def start_ollama() -> Dict[str, Any]:
    """Start the Ollama server process."""
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.DETACHED_PROCESS,
            )
        elif system == "Darwin":
            # macOS: try brew services, fallback to direct launch
            result = subprocess.run(
                ["brew", "services", "start", "ollama"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        else:
            # Linux: try systemd, fallback to direct launch
            result = subprocess.run(
                ["sudo", "systemctl", "start", "ollama"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

        log.info("Ollama start issued")
        return {"success": True, "message": "Ollama start issued"}
    except Exception as e:
        log.error(f"Failed to start Ollama: {e}")
        return {"success": False, "error": str(e)}


def get_status() -> Dict[str, Any]:
    """Return full process status without modifying anything."""
    state = discover()
    stored = _load_pids()

    return {
        "state": "active" if (state["ollama_alive"] and state["agent_alive"]) else
                 ("idle" if state["agent_alive"] and not state["ollama_alive"] else "offline"),
        "processes": state,
        "last_known": stored,
    }
