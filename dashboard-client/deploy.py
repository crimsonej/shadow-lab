"""
deploy.py — SSH-based automated remote agent deployment.
=========================================================
Connects to a remote Linux server via SSH, installs all prerequisites
(Python, pip, Ollama), uploads the server-agent bundle, configures it,
and starts the agent as a background daemon.

All steps are logged in real-time so the dashboard UI can stream progress.
"""
import os
import time
import uuid
import threading
import logging
import tarfile
import io
import secrets
import traceback

import paramiko
from pathlib import Path

log = logging.getLogger(__name__)

# ── In-memory deployment tracker ─────────────────────────────────────────────
# { deploy_id: { "status", "logs", "token", "agent_url", "server_name" } }
active_deployments = {}


def _log(deploy_id, message):
    """Append a timestamped line to the deployment log."""
    if deploy_id in active_deployments:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        active_deployments[deploy_id]["logs"].append(line)
        log.info(f"Deploy {deploy_id[:8]}: {message}")


def _exec(ssh, cmd, deploy_id, label="command", timeout=120):
    """
    Execute a remote command, wait for it to finish, and return
    (exit_status, stdout_text, stderr_text).  Logs failures automatically.
    """
    _log(deploy_id, f"  ▸ Running: {label}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if exit_status != 0:
        _log(deploy_id, f"  ✗ {label} exited with code {exit_status}")
        if err:
            # Only log first 3 lines to keep terminal clean
            for line in err.splitlines()[:3]:
                _log(deploy_id, f"    stderr: {line}")
    return exit_status, out, err


# ── Main deployment routine ──────────────────────────────────────────────────

def run_deployment(deploy_id, host, port, username,
                   password=None, key_path=None, server_name=None):
    """Full deployment pipeline executed in a background thread."""
    try:
        active_deployments[deploy_id]["status"] = "running"
        active_deployments[deploy_id]["server_name"] = server_name or host
        _log(deploy_id, f"Starting SSH deployment to {username}@{host}:{port}")

        # ── 1. SSH Connect ────────────────────────────────────────────────
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            connect_kwargs = dict(
                hostname=host, port=port, username=username, timeout=15,
                banner_timeout=15, auth_timeout=15,
            )
            if key_path:
                connect_kwargs["key_filename"] = key_path
            else:
                connect_kwargs["password"] = password
                connect_kwargs["look_for_keys"] = False

            ssh.connect(**connect_kwargs)
        except Exception as e:
            active_deployments[deploy_id]["status"] = "failed"
            _log(deploy_id, f"SSH Connection FAILED: {e}")
            return

        _log(deploy_id, "✓ SSH connection established.")

        # ── 2. Detect OS ──────────────────────────────────────────────────
        _log(deploy_id, "Detecting operating system...")
        _, os_info, _ = _exec(ssh, "cat /etc/os-release 2>/dev/null || echo unknown",
                              deploy_id, "OS detection")

        os_lower = os_info.lower()
        if "ubuntu" in os_lower or "debian" in os_lower:
            pkg_mgr = "apt"
            install_cmd = (
                "export DEBIAN_FRONTEND=noninteractive && "
                "sudo -n apt-get update -qq && "
                "sudo -n apt-get install -y -qq python3 python3-pip python3-venv curl tar"
            )
        elif "centos" in os_lower or "fedora" in os_lower or "rhel" in os_lower or "rocky" in os_lower or "alma" in os_lower:
            pkg_mgr = "yum"
            install_cmd = "sudo -n yum install -y python3 python3-pip curl tar"
        elif "arch" in os_lower:
            pkg_mgr = "pacman"
            install_cmd = "sudo -n pacman -Sy --noconfirm python python-pip curl tar"
        else:
            pkg_mgr = "unknown"
            _log(deploy_id, "⚠ Unknown OS — trying apt then yum as fallback")
            install_cmd = (
                "(sudo -n apt-get update -qq && "
                "sudo -n apt-get install -y -qq python3 python3-pip python3-venv curl tar) || "
                "sudo -n yum install -y python3 python3-pip curl tar"
            )

        # Extract distro name for display
        for line in os_info.splitlines():
            if line.startswith("PRETTY_NAME="):
                distro = line.split("=", 1)[1].strip('"')
                _log(deploy_id, f"✓ OS: {distro} (package manager: {pkg_mgr})")
                break
        else:
            _log(deploy_id, f"✓ OS detected (package manager: {pkg_mgr})")

        # ── 3. Install system prerequisites ───────────────────────────────
        _log(deploy_id, "Installing system prerequisites...")
        rc, _, _ = _exec(ssh, install_cmd, deploy_id,
                         "System packages", timeout=180)
        if rc != 0:
            _log(deploy_id, "⚠ Some packages may have failed — continuing anyway")
        else:
            _log(deploy_id, "✓ Prerequisites installed.")

        # Verify Python is available
        rc, pyver, _ = _exec(ssh, "python3 --version", deploy_id, "Python check")
        if rc != 0:
            active_deployments[deploy_id]["status"] = "failed"
            _log(deploy_id, "✗ FATAL: Python3 is not available on the remote server.")
            ssh.close()
            return
        _log(deploy_id, f"✓ {pyver}")

        # ── 4. Install Ollama ─────────────────────────────────────────────
        _log(deploy_id, "Checking for Ollama...")
        rc, ollama_ver, _ = _exec(ssh, "ollama --version 2>/dev/null",
                                  deploy_id, "Ollama check")
        if rc != 0:
            _log(deploy_id, "Ollama not found — installing (this may take a minute)...")
            rc, _, _ = _exec(ssh,
                             "curl -fsSL https://ollama.com/install.sh | sh",
                             deploy_id, "Ollama install", timeout=300)
            if rc != 0:
                _log(deploy_id, "⚠ Ollama install returned non-zero — agent may still work if Ollama is already present")
            else:
                _log(deploy_id, "✓ Ollama installed successfully.")
        else:
            _log(deploy_id, f"✓ Ollama already installed: {ollama_ver}")

        # Ensure ollama service is running
        _exec(ssh, "sudo -n systemctl start ollama 2>/dev/null || true",
              deploy_id, "Start Ollama service")

        # ── 5. Prepare and upload agent bundle ────────────────────────────
        _log(deploy_id, "Preparing agent bundle...")
        agent_dir = Path(__file__).parent.parent / "server-agent"
        if not agent_dir.exists():
            active_deployments[deploy_id]["status"] = "failed"
            _log(deploy_id, f"✗ FATAL: server-agent directory not found at {agent_dir}")
            ssh.close()
            return

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            for f in agent_dir.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    tar.add(str(f), arcname=f.name)
        tar_buf.seek(0)
        bundle_kb = len(tar_buf.getvalue()) // 1024
        _log(deploy_id, f"✓ Bundle created ({bundle_kb} KB, {sum(1 for _ in agent_dir.iterdir() if _.is_file())} files)")

        _log(deploy_id, "Uploading agent bundle via SFTP...")
        remote_tar = "/tmp/shadow-agent-bundle.tar.gz"
        try:
            sftp = ssh.open_sftp()
            sftp.putfo(tar_buf, remote_tar)
            sftp.close()
        except Exception as e:
            active_deployments[deploy_id]["status"] = "failed"
            _log(deploy_id, f"✗ SFTP upload failed: {e}")
            ssh.close()
            return
        _log(deploy_id, "✓ Bundle uploaded.")

        # ── 6. Extract and set up virtual environment ─────────────────────
        _log(deploy_id, "Setting up agent environment on remote server...")
        setup_cmds = [
            "mkdir -p ~/shadow-agent",
            f"tar -xzf {remote_tar} -C ~/shadow-agent",
            f"rm -f {remote_tar}",
            "cd ~/shadow-agent && python3 -m venv .venv 2>/dev/null || python3 -m venv --without-pip .venv",
        ]
        for cmd in setup_cmds:
            _exec(ssh, cmd, deploy_id, cmd.split("&&")[-1].strip()[:60])

        _log(deploy_id, "Installing Python dependencies...")
        rc, _, _ = _exec(
            ssh,
            "cd ~/shadow-agent && source .venv/bin/activate && "
            "pip install -q -r requirements.txt 2>&1 | tail -5",
            deploy_id, "pip install", timeout=180,
        )
        if rc != 0:
            # Fallback: try without venv
            _log(deploy_id, "⚠ venv pip failed — trying system pip...")
            _exec(
                ssh,
                "cd ~/shadow-agent && pip3 install --break-system-packages -r requirements.txt 2>&1 | tail -5",
                deploy_id, "pip install (system)", timeout=180,
            )
        _log(deploy_id, "✓ Dependencies installed.")

        # ── 7. Detect free port ───────────────────────────────────────────
        agent_port = 8080
        _log(deploy_id, f"Checking if port {agent_port} is available...")
        rc, _, _ = _exec(ssh, f"ss -tlnp | grep -q ':{agent_port} '",
                         deploy_id, "Port check")
        if rc == 0:
            # Port is in use — try alternatives
            for alt_port in [8081, 8082, 8090, 9090]:
                rc2, _, _ = _exec(ssh, f"ss -tlnp | grep -q ':{alt_port} '",
                                  deploy_id, f"Port {alt_port} check")
                if rc2 != 0:
                    agent_port = alt_port
                    _log(deploy_id, f"⚠ Port 8080 in use — using {agent_port} instead")
                    break
            else:
                _log(deploy_id, "⚠ All common ports occupied — using 8080 anyway")
        else:
            _log(deploy_id, f"✓ Port {agent_port} is available.")

        # ── 8. Generate credentials and write .env ────────────────────────
        admin_token = secrets.token_hex(16)
        active_deployments[deploy_id]["token"] = admin_token

        env_content = f"ADMIN_TOKEN={admin_token}\\nAGENT_PORT={agent_port}\\nOLLAMA_URL=http://127.0.0.1:11434"
        _exec(ssh, f'printf "{env_content}\\n" > ~/shadow-agent/.env',
              deploy_id, "Write .env")
        _log(deploy_id, f"✓ Admin token generated and .env written (port {agent_port})")

        # ── 9. Kill any existing agent and start fresh ────────────────────
        _log(deploy_id, "Starting agent daemon...")
        _exec(ssh, "pkill -f 'shadow-agent.*main.py' 2>/dev/null || true",
              deploy_id, "Kill old agent")

        # Check for systemd
        rc_sys, _, _ = _exec(ssh, "command -v systemctl >/dev/null 2>&1 && echo yes || echo no",
                             deploy_id, "systemd check")

        # Use nohup (reliable across all systems)
        start_cmd = (
            "cd ~/shadow-agent && "
            "nohup .venv/bin/python3 main.py > agent.log 2>&1 &"
        )
        _exec(ssh, start_cmd, deploy_id, "nohup start")
        _log(deploy_id, "✓ Agent process started in background.")

        # ── 10. Health check loop ─────────────────────────────────────────
        agent_url = f"http://{host}:{agent_port}"
        active_deployments[deploy_id]["agent_url"] = agent_url
        _log(deploy_id, f"Running health checks on {agent_url}...")

        success = False
        for attempt in range(12):
            time.sleep(3)
            rc, body, _ = _exec(
                ssh, f"curl -sf http://127.0.0.1:{agent_port}/v1/health",
                deploy_id, f"Health check {attempt + 1}/12",
            )
            if rc == 0:
                success = True
                break
            _log(deploy_id, f"  … attempt {attempt + 1}/12 — not ready yet")

        if success:
            active_deployments[deploy_id]["status"] = "success"
            _log(deploy_id, "")
            _log(deploy_id, "═══════════════════════════════════════════")
            _log(deploy_id, f"  ✓ DEPLOYMENT SUCCESSFUL")
            _log(deploy_id, f"  Agent URL:    {agent_url}")
            _log(deploy_id, f"  Admin Token:  {admin_token[:8]}…{admin_token[-4:]}")
            _log(deploy_id, "═══════════════════════════════════════════")
        else:
            # Grab tail of agent.log for diagnostics
            _, agent_log, _ = _exec(ssh, "tail -20 ~/shadow-agent/agent.log 2>/dev/null",
                                    deploy_id, "Fetch agent.log")
            active_deployments[deploy_id]["status"] = "failed"
            _log(deploy_id, "")
            _log(deploy_id, "✗ DEPLOYMENT FAILED — agent did not respond to health checks.")
            if agent_log:
                _log(deploy_id, "── Last lines of agent.log ──")
                for line in agent_log.splitlines()[-10:]:
                    _log(deploy_id, f"  {line}")

        ssh.close()

    except Exception as e:
        active_deployments[deploy_id]["status"] = "failed"
        _log(deploy_id, f"✗ Deployment error: {e}")
        log.error(traceback.format_exc())


# ── Public API ────────────────────────────────────────────────────────────────

def start_deployment(host, port, username,
                     password=None, key_path=None, server_name=None):
    """Kick off a deployment in a background thread. Returns the deploy_id."""
    deploy_id = str(uuid.uuid4())
    active_deployments[deploy_id] = {
        "status": "pending",
        "logs": [],
        "token": None,
        "agent_url": None,
        "server_name": server_name or host,
    }
    t = threading.Thread(
        target=run_deployment,
        args=(deploy_id, host, port, username),
        kwargs={"password": password, "key_path": key_path, "server_name": server_name},
        daemon=True,
    )
    t.start()
    return deploy_id
