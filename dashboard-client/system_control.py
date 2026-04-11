"""
system_control.py — Server lifecycle control with safety guardrails.
"""
import logging
from typing import Dict, Any

import ssh_manager
from compat import command_router
from compat import os_detector
import server_lifecycle

log = logging.getLogger(__name__)

async def _fetch_metrics(server: dict) -> Dict[str, Any]:
    """Fetch metrics from the agent to check active requests and loaded models."""
    host = server.get("host", "")
    token = server.get("admin_token", "")
    ok, data = await server_lifecycle._http_get(host, token, "/admin/metrics")
    if ok and data:
        return data
    return {}

async def _check_safety(server: dict, force: bool) -> tuple[bool, str]:
    """
    Check if it is safe to shutdown or reboot.
    Returns (is_safe: bool, reason: str).
    """
    if force:
        return True, "Force override applied."

    metrics = await _fetch_metrics(server)
    if not metrics:
        # If agent is unreachable, we can't be sure, but we might allow it if it's dead anyway?
        # A safer approach is to warn, but since it's unreachable, maybe it's completely down.
        # Let the UI decide or user force.
        return False, "Agent unreachable. Cannot verify active requests. Use force override."

    active_requests = metrics.get("active_requests", 0)
    if active_requests > 0:
        return False, f"Server is actively processing {active_requests} requests."

    models_loaded = metrics.get("models_loaded", [])
    if models_loaded:
        model_names = [m.get("name", "unknown") for m in models_loaded]
        return False, f"Models ({', '.join(model_names)}) are currently loaded in VRAM."

    return True, "Safe to proceed."

async def shutdown_machine(server: dict, force: bool = False) -> Dict[str, Any]:
    """Shutdown the entire server OS."""
    is_safe, reason = await _check_safety(server, force)
    if not is_safe:
        return {"success": False, "method": "none", "details": {"error": reason}}

    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "SSH not configured"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("shutdown_machine", os_info)

    # We expect this command to drop the connection
    ssh.execute(cmd, timeout=5)
    
    return {
        "success": True,
        "method": "ssh",
        "details": {"message": f"Shutdown issued to {os_info['os']} via SSH. Reason: {reason}"}
    }

async def reboot_machine(server: dict, force: bool = False) -> Dict[str, Any]:
    """Reboot the entire server OS."""
    is_safe, reason = await _check_safety(server, force)
    if not is_safe:
        return {"success": False, "method": "none", "details": {"error": reason}}

    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "SSH not configured"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("reboot_machine", os_info)

    ssh.execute(cmd, timeout=5)
    
    return {
        "success": True,
        "method": "ssh",
        "details": {"message": f"Reboot issued to {os_info['os']} via SSH. Reason: {reason}"}
    }

async def sleep_mode(server: dict) -> Dict[str, Any]:
    """
    Idle Mode: Stops Ollama and heavy processes, keeps agent alive.
    """
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "SSH not configured"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("stop_ollama", os_info)

    rc, _, err = ssh.execute(cmd, timeout=15)
    
    # Optionally we could also unload models using Ollama API directly,
    # but stopping the service fully frees VRAM/RAM.
    
    return {
        "success": True,
        "method": "ssh",
        "details": {"message": "Idle mode activated: Ollama stopped, Agent remains alive."}
    }

async def start_ai(server: dict) -> Dict[str, Any]:
    """Start both Ollama engine and AI agent."""
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "SSH not configured"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("start_ai", os_info)

    rc, _, err = ssh.execute(cmd, timeout=15)
    return {
        "success": rc == 0,
        "method": "ssh",
        "details": {"message": "Start AI issued" if rc == 0 else err}
    }

async def stop_ai(server: dict) -> Dict[str, Any]:
    """Stop both Ollama engine and AI agent."""
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "SSH not configured"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("stop_ai", os_info)

    rc, _, err = ssh.execute(cmd, timeout=15)
    return {
        "success": rc == 0,
        "method": "ssh",
        "details": {"message": "Stop AI issued" if rc == 0 else err}
    }

async def restart_ai(server: dict) -> Dict[str, Any]:
    """Restart both services."""
    await stop_ai(server)
    return await start_ai(server)


async def deactivate_server(server: dict, force: bool = False) -> Dict[str, Any]:
    """
    Soft deactivate: stop Ollama via agent HTTP API.
    Falls back to SSH command_router if agent is unreachable.
    """
    is_safe, reason = await _check_safety(server, force)
    if not is_safe:
        return {"success": False, "method": "none", "details": {"error": reason}}

    host = server.get("host", "")
    token = server.get("admin_token", "")

    # Try agent HTTP API first (preferred — uses process_tracker)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{host}/admin/deactivate",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                data = r.json()
                return {
                    "success": data.get("status") != "blocked",
                    "method": "http",
                    "details": data,
                }
    except Exception:
        pass

    # Fallback: SSH
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "No connection method available"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("deactivate_server", os_info)
    rc, _, err = ssh.execute(cmd, timeout=15)

    return {
        "success": rc == 0,
        "method": "ssh",
        "details": {"message": f"Deactivated via SSH ({os_info['os']})" if rc == 0 else err},
    }


async def activate_server(server: dict) -> Dict[str, Any]:
    """
    Soft activate: start Ollama via agent HTTP API.
    Falls back to SSH command_router if agent is unreachable.
    """
    host = server.get("host", "")
    token = server.get("admin_token", "")

    # Try agent HTTP API first
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{host}/admin/activate",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                data = r.json()
                return {
                    "success": data.get("status") == "active",
                    "method": "http",
                    "details": data,
                }
    except Exception:
        pass

    # Fallback: SSH
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"success": False, "method": "none", "details": {"error": "No connection method available"}}

    os_info = os_detector.detect_os(ssh)
    cmd = command_router.get_command("activate_server", os_info)
    rc, _, err = ssh.execute(cmd, timeout=15)

    return {
        "success": rc == 0,
        "method": "ssh",
        "details": {"message": f"Activated via SSH ({os_info['os']})" if rc == 0 else err},
    }

