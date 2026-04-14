"""
tunnel_manager.py — Manage local SSH tunnels for secure agent communication.
"""
import os
import signal
import subprocess
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

TUNNEL_DIR = Path.home() / ".shadowlab" / "tunnels"
TUNNEL_DIR.mkdir(parents=True, exist_ok=True)

def _pid_file(server_id: int) -> Path:
    return TUNNEL_DIR / f"{server_id}.pid"

def start_tunnel(server_id: int, host: str, user: str, remote_port: int, local_port: int, key_path: Optional[str] = None) -> bool:
    """
    Establish a local SSH tunnel: local_port -> remote:remote_port.
    Saves the PID for later termination.
    """
    if is_tunnel_alive(server_id):
        log.info(f"Tunnel for server {server_id} already alive.")
        return True

    # Construct SSH command
    # -N: Do not execute a remote command.
    # -L: Local port forwarding.
    # -f: Go to background just before command execution (using nohup/subprocess instead for better control)
    ssh_cmd = [
        "ssh", "-N",
        "-L", f"{local_port}:127.0.0.1:{remote_port}",
        f"{user}@{host}"
    ]
    
    if key_path:
        ssh_cmd.extend(["-i", key_path])
    
    # We use subprocess.Popen and don't wait for it.
    try:
        # Note: We need to handle host key verification. 
        # For a headless tool, BatchMode=yes and StrictHostKeyChecking=accept-new are safer defaults.
        ssh_cmd.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"])
        
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        
        _pid_file(server_id).write_text(str(proc.pid))
        log.info(f"Started tunnel for server {server_id} (PID {proc.pid}) at 127.0.0.1:{local_port}")
        return True
    except Exception as e:
        log.error(f"Failed to start tunnel for server {server_id}: {e}")
        return False

def stop_tunnel(server_id: int):
    """Kill the tunnel process for a specific server."""
    pf = _pid_file(server_id)
    if not pf.exists():
        return

    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        log.info(f"Killed tunnel PID {pid} for server {server_id}")
    except (OSError, ValueError, ProcessLookupError):
        pass
    finally:
        if pf.exists():
            pf.unlink()

def is_tunnel_alive(server_id: int) -> bool:
    """Check if the tunnel process is still running."""
    pf = _pid_file(server_id)
    if not pf.exists():
        return False
    
    try:
        pid = int(pf.read_text().strip())
        # Signal 0 checks for process existence
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, ProcessLookupError):
        # Clean up stale PID file
        if pf.exists():
            pf.unlink()
        return False
