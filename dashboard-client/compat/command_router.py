"""
command_router.py — Maps logical actions to OS-specific execution commands.
"""

def get_command(action: str, os_info: dict) -> str:
    """
    Returns the corresponding shell command for the detected OS.
    Supported actions:
        - start_ollama
        - stop_ollama
        - restart_ollama
        - check_status_ollama
        - restart_agent
        - stop_agent
        - start_agent
    """
    os_family = os_info.get("os", "unknown")
    
    if os_family == "macos":
        # macOS uses brew services
        if action == "start_ollama":
            return "brew services start ollama"
        elif action == "stop_ollama":
            return "brew services stop ollama"
        elif action == "restart_ollama":
            return "brew services restart ollama"
        elif action == "check_status_ollama":
            return "brew services info ollama"
        elif action == "restart_agent":
            return "pkill -f 'shadow-agent.*main.py' 2>/dev/null; cd ~/shadow-agent && nohup .venv/bin/python3 main.py > agent.log 2>&1 &"
        elif action == "stop_agent":
            return "pkill -f 'shadow-agent.*main.py' 2>/dev/null || true"
        elif action == "start_agent":
            return "cd ~/shadow-agent && nohup .venv/bin/python3 main.py > agent.log 2>&1 &"

    elif os_family == "windows":
        # Windows uses services / powershell
        if action == "start_ollama":
            return "Start-Service ollama"
        elif action == "stop_ollama":
            return "Stop-Service ollama"
        elif action == "restart_ollama":
            return "Restart-Service ollama"
        elif action == "check_status_ollama":
            return "Get-Service ollama"
        elif action == "restart_agent":
            return "Stop-Process -Name python -ErrorAction SilentlyContinue; Start-Process python -ArgumentList 'main.py' -WorkingDirectory 'C:\\shadow-agent' -WindowStyle Hidden"
        elif action == "stop_agent":
            return "Stop-Process -Name python -ErrorAction SilentlyContinue"
        elif action == "start_agent":
            return "Start-Process python -ArgumentList 'main.py' -WorkingDirectory 'C:\\shadow-agent' -WindowStyle Hidden"

    else:
        # Default Linux / systemd
        if action == "start_ollama":
            return "sudo systemctl start ollama"
        elif action == "stop_ollama":
            return "sudo systemctl stop ollama"
        elif action == "restart_ollama":
            return "sudo systemctl restart ollama"
        elif action == "check_status_ollama":
            return "sudo systemctl is-active ollama"
        elif action == "restart_agent":
            return "sudo systemctl restart ollama-agent 2>/dev/null || (pkill -f 'shadow-agent.*main.py' 2>/dev/null; cd ~/shadow-agent && nohup .venv/bin/python3 main.py > agent.log 2>&1 &)"
        elif action == "stop_agent":
            return "sudo systemctl stop ollama-agent 2>/dev/null || pkill -f 'shadow-agent.*main.py' 2>/dev/null || true"
        elif action == "start_agent":
            return "sudo systemctl start ollama-agent 2>/dev/null || (cd ~/shadow-agent && nohup .venv/bin/python3 main.py > agent.log 2>&1 &)"

    return f"echo 'Unsupported action {action} on {os_family}'"
