"""
compatibility.py — OS and Python compatibility detection layer.

Runs on the dashboard side via SSH. Does NOT modify remote systems —
only detects current state. Used during deployment pre-checks and
server management.
"""
import logging
import re
from typing import Dict, Any, Optional, List

import ssh_manager
from compat.os_detector import detect_os as _detect_os_module

log = logging.getLogger(__name__)

# Python versions we support
PYTHON_MIN = (3, 10)
PYTHON_MAX = (3, 12)


def detect_os(server: dict) -> Dict[str, Any]:
    """
    Detect the remote operating system using the new compat module.
    """
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"distro": "unknown", "version": "", "family": "unknown",
                "pkg_manager": "unknown", "pretty_name": "SSH not configured", "error": "SSH not configured", "os": "unknown", "arch": "unknown"}

    # Use our abstract detector
    result = _detect_os_module(ssh)
    
    # Try to augment it with some legacy distro details if it's linux
    distro_id = "unknown"
    pretty = f"{result['os']} {result['arch']}"
    
    if result["os"] == "linux":
        rc, out, _ = ssh.execute("cat /etc/os-release 2>/dev/null || echo 'ID=linux'")
        for line in out.splitlines():
            if line.startswith("ID="):
                distro_id = line.split("=")[1].strip('"').lower()
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=")[1].strip('"')

    return {
        "os": result["os"],
        "arch": result["arch"],
        "distro": distro_id,
        "version": "",
        "family": "debian" if result["package_manager"] == "apt" else ("rhel" if result["package_manager"] in ("yum", "dnf") else ("arch" if result["package_manager"] == "pacman" else "unknown")),
        "pkg_manager": result["package_manager"],
        "pretty_name": pretty,
        "error": None,
    }


def detect_python(server: dict) -> Dict[str, Any]:
    """
    Detect the remote Python installation.

    Returns:
        {
            "version": str,         # e.g. "3.11.4"
            "major": int,
            "minor": int,
            "path": str,            # Absolute path to python3 binary
            "is_compatible": bool,  # True if 3.10-3.12
            "has_venv": bool,
            "has_pip": bool,
            "warning": str | None,  # Warning message for 3.13+ or <3.9
            "error": str | None,
        }
    """
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"version": "", "major": 0, "minor": 0, "path": "",
                "is_compatible": False, "has_venv": False, "has_pip": False,
                "warning": None, "error": "SSH not configured"}

    # Get Python version
    rc, ver_out, _ = ssh.execute("python3 -c \"import sys; print(sys.version_info.major, sys.version_info.minor, sys.version_info.micro)\"")
    if rc != 0:
        return {"version": "not found", "major": 0, "minor": 0, "path": "",
                "is_compatible": False, "has_venv": False, "has_pip": False,
                "warning": "Python 3 not installed", "error": "python3 not found"}

    parts = ver_out.strip().split()
    major, minor, micro = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    version = f"{major}.{minor}.{micro}"

    # Get path
    _, path, _ = ssh.execute("which python3")

    # Check venv
    rc_venv, _, _ = ssh.execute("python3 -m venv --help >/dev/null 2>&1")
    has_venv = rc_venv == 0

    # Check pip
    rc_pip, _, _ = ssh.execute("python3 -m pip --version >/dev/null 2>&1")
    has_pip = rc_pip == 0

    # Compatibility check
    is_compat = PYTHON_MIN <= (major, minor) <= PYTHON_MAX
    warning = None
    if (major, minor) > PYTHON_MAX:
        warning = (
            f"Python {version} detected. Python 3.13+ has known compatibility issues "
            f"with pydantic-core/FastAPI. Recommended: Python 3.10-3.12. "
            f"Consider using a virtual environment with a compatible version."
        )
    elif (major, minor) < PYTHON_MIN:
        warning = (
            f"Python {version} is below the minimum supported version (3.10). "
            f"Please upgrade Python."
        )

    return {
        "version": version,
        "major": major,
        "minor": minor,
        "path": path.strip(),
        "is_compatible": is_compat,
        "has_venv": has_venv,
        "has_pip": has_pip,
        "warning": warning,
        "error": None,
    }


def check_dependencies(server: dict) -> Dict[str, Any]:
    """
    Check for required system-level dependencies on the remote server.

    Returns:
        {
            "missing": [str],       # Missing package names
            "present": [str],       # Found packages
            "install_command": str,  # Suggested install command
            "error": str | None,
        }
    """
    ssh = ssh_manager.get_ssh(server)
    if not ssh:
        return {"missing": [], "present": [], "install_command": "", "error": "SSH not configured"}

    # Required CLI tools
    required = ["curl", "tar", "python3"]
    present = []
    missing = []

    for tool in required:
        rc, _, _ = ssh.execute(f"command -v {tool} >/dev/null 2>&1")
        if rc == 0:
            present.append(tool)
        else:
            missing.append(tool)

    # Check for Ollama
    rc, _, _ = ssh.execute("command -v ollama >/dev/null 2>&1")
    if rc == 0:
        present.append("ollama")
    else:
        missing.append("ollama")

    # Generate the install command based on detected OS
    os_info = detect_os(server)
    install_cmd = get_install_command(os_info.get("pkg_manager", "unknown"), missing)

    return {
        "missing": missing,
        "present": present,
        "install_command": install_cmd,
        "error": None,
    }


def get_install_command(pkg_manager: str, packages: List[str]) -> str:
    """
    Generate the correct install command for the detected package manager.
    Filters out non-system packages (like 'ollama') which have their own installers.
    """
    # Ollama isn't a system package
    system_pkgs = [p for p in packages if p != "ollama"]
    if not system_pkgs:
        return ""

    # Map generic names to distro-specific package names
    pkg_map = {
        "apt": {"python3": "python3 python3-pip python3-venv"},
        "yum": {"python3": "python3 python3-pip"},
        "dnf": {"python3": "python3 python3-pip"},
        "pacman": {"python3": "python python-pip"},
    }

    if pkg_manager == "apt":
        expanded = []
        for p in system_pkgs:
            expanded.append(pkg_map.get("apt", {}).get(p, p))
        return f"sudo apt-get update && sudo apt-get install -y {' '.join(expanded)}"
    elif pkg_manager == "yum":
        expanded = []
        for p in system_pkgs:
            expanded.append(pkg_map.get("yum", {}).get(p, p))
        return f"sudo yum install -y {' '.join(expanded)}"
    elif pkg_manager == "dnf":
        expanded = []
        for p in system_pkgs:
            expanded.append(pkg_map.get("dnf", {}).get(p, p))
        return f"sudo dnf install -y {' '.join(expanded)}"
    elif pkg_manager == "pacman":
        expanded = []
        for p in system_pkgs:
            expanded.append(pkg_map.get("pacman", {}).get(p, p))
        return f"sudo pacman -Sy --noconfirm {' '.join(expanded)}"
    else:
        return f"# Unknown package manager — manually install: {' '.join(system_pkgs)}"


def full_compatibility_report(server: dict) -> Dict[str, Any]:
    """
    Run all compatibility checks and return a combined report.
    """
    return {
        "os": detect_os(server),
        "python": detect_python(server),
        "dependencies": check_dependencies(server),
    }
