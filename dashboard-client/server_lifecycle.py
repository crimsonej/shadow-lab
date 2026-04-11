"""
server_lifecycle.py — Hybrid server lifecycle control from the dashboard.

Uses HTTP agent API when reachable, SSH as fallback when agent is down.
All functions take a `server` dict (from db.py) as input.
"""
import logging
from typing import Dict, Any, Optional

import httpx

import ssh_manager
from compat import command_router
from compat import os_detector

log = logging.getLogger(__name__)

# Shared async HTTP client
_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))


def _agent_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _http_get(host: str, token: str, path: str):
    """Try an HTTP GET to the agent. Returns (success, data)."""
    try:
        r = await _http.get(f"{host}{path}", headers=_agent_headers(token))
        return r.status_code == 200, r.json()
    except Exception:
        return False, None


async def _http_post(host: str, token: str, path: str, body: dict = None):
    """Try an HTTP POST to the agent. Returns (success, data)."""
    try:
        r = await _http.post(
            f"{host}{path}", json=body or {}, headers=_agent_headers(token)
        )
        return r.status_code == 200, r.json()
    except Exception:
        return False, None


# ── Health Check (hybrid) ─────────────────────────────────────────────────────

async def check_server_health(server: dict) -> Dict[str, Any]:
    """
    Check server health. Tries HTTP first, then SSH fallback.

    Returns:
        {
            "online": bool,
            "method": "http" | "ssh" | "unreachable",
            "ollama_running": bool | None,
            "details": dict | None,
        }
    """
    host = server.get("host", "")
    token = server.get("admin_token", "")

    # Try HTTP first
    ok, data = await _http_get(host, token, "/v1/health")
    if ok:
        return {
            "online": True,
            "method": "http",
            "ollama_running": data.get("ollama_running"),
            "details": data,
        }

    # Fallback to SSH
    ssh = ssh_manager.get_ssh(server)
    if ssh and ssh.is_reachable():
        # Try curling the agent from localhost on the remote machine
        agent_port = _extract_port(host)
        rc, out, _ = ssh.execute(
            f"curl -sf http://127.0.0.1:{agent_port}/v1/health", timeout=5
        )
        if rc == 0:
            import json
            try:
                data = json.loads(out)
                return {
                    "online": True,
                    "method": "ssh",
                    "ollama_running": data.get("ollama_running"),
                    "details": data,
                }
            except Exception:
                pass

        # Agent might be down, but SSH works — check Ollama directly
        rc2, _, _ = ssh.execute("curl -sf http://127.0.0.1:11434/", timeout=5)
        return {
            "online": False,
            "method": "ssh",
            "ollama_running": rc2 == 0,
            "details": {"note": "Agent unreachable but SSH connected"},
        }

    return {
        "online": False,
        "method": "unreachable",
        "ollama_running": None,
        "details": None,
    }


# ── Restart Ollama (hybrid) ──────────────────────────────────────────────────

async def restart_ollama(server: dict) -> Dict[str, Any]:
    """Restart Ollama. HTTP first, SSH fallback."""
    host = server.get("host", "")
    token = server.get("admin_token", "")

    # Try HTTP
    ok, data = await _http_post(host, token, "/admin/lifecycle/restart-ollama")
    if ok:
        return {"success": True, "method": "http", "details": data}

    # SSH fallback
    ssh = ssh_manager.get_ssh(server)
    if ssh:
        os_info = os_detector.detect_os(ssh)
        cmd = command_router.get_command("restart_ollama", os_info)
        
        rc, out, err = ssh.execute(cmd, timeout=30)
        if rc == 0:
            return {"success": True, "method": "ssh", "details": {"message": f"Ollama restarted via SSH ({os_info['os']})" }}
        return {"success": False, "method": "ssh", "details": {"error": err or out}}

    return {"success": False, "method": "unreachable", "details": {"error": "No connection method available"}}


# ── Restart Agent (SSH only — agent can't restart itself) ─────────────────────

async def restart_agent(server: dict) -> Dict[str, Any]:
    """Restart the agent process. SSH only."""
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "SSH not configured"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("restart_agent", os_info)

    # Try systemd first, then nohup fallback (or equivalent from router)
    rc, _, err = ssh.execute(cmd, timeout=15)

    return {
        "success": rc == 0,
        "method": "ssh",
        "details": {"message": "Agent restart issued" if rc == 0 else err},
    }


# ── Stop Agent (SSH only) ────────────────────────────────────────────────────

async def stop_agent(server: dict) -> Dict[str, Any]:
    """Stop the agent process. SSH only."""
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "SSH not configured"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("stop_agent", os_info)

    rc, _, err = ssh.execute(cmd, timeout=10)
    return {
        "success": True,
        "method": "ssh",
        "details": {"message": "Agent stop issued"},
    }


# ── Start Agent (SSH only) ───────────────────────────────────────────────────

async def start_agent(server: dict) -> Dict[str, Any]:
    """Start the agent process. SSH only."""
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "SSH not configured"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("start_agent", os_info)

    rc, _, err = ssh.execute(cmd, timeout=10)
    return {
        "success": rc == 0,
        "method": "ssh",
        "details": {"message": "Agent start issued" if rc == 0 else err},
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_port(host_url: str) -> int:
    """Extract port from a URL like http://1.2.3.4:8080."""
    try:
        if ":" in host_url.split("//")[-1]:
            return int(host_url.rsplit(":", 1)[-1].split("/")[0])
    except Exception:
        pass
    return 8080
