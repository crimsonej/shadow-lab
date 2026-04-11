import logging

log = logging.getLogger(__name__)

def detect_os(ssh_manager) -> dict:
    """
    Detect the remote Operating System via SSH execution.
    Returns:
    {
      "os": "linux | macos | windows | unknown",
      "arch": "x86_64 | arm64",
      "package_manager": "apt | brew | choco | dnf | pacman | yum | unknown"
    }
    """
    if not ssh_manager or not ssh_manager.is_reachable():
        return {"os": "unknown", "arch": "unknown", "package_manager": "unknown"}

    # --- Try Linux / macOS via uname ---
    rc_uname, out_uname, _ = ssh_manager.execute("uname -sm 2>/dev/null")
    if rc_uname == 0 and out_uname:
        parts = out_uname.lower().split()
        sys_name = parts[0] if parts else "unknown"
        arch_raw = parts[1] if len(parts) > 1 else "unknown"

        arch = "x86_64" if arch_raw in ("x86_64", "amd64") else ("arm64" if arch_raw in ("arm64", "aarch64") else arch_raw)

        if sys_name == "darwin":
            return {"os": "macos", "arch": arch, "package_manager": "brew"}
        
        elif sys_name == "linux":
            # Identify specific package manager
            rc_osr, out_osr, _ = ssh_manager.execute("cat /etc/os-release 2>/dev/null")
            pkg = "unknown"
            
            if rc_osr == 0:
                distro_id = "unknown"
                id_like = ""
                for line in out_osr.splitlines():
                    if line.startswith("ID="):
                        distro_id = line.split("=")[1].strip('"').lower()
                    if line.startswith("ID_LIKE="):
                        id_like = line.split("=")[1].strip('"').lower()
                
                if distro_id in ("ubuntu", "debian", "linuxmint", "pop", "parrot", "kali", "raspbian") or "debian" in id_like:
                    pkg = "apt"
                elif distro_id in ("arch", "manjaro", "endeavouros", "garuda") or "arch" in id_like:
                    pkg = "pacman"
                elif distro_id in ("fedora",) or "fedora" in id_like:
                    pkg = "dnf"
                elif distro_id in ("centos", "rhel", "rocky", "almalinux", "ol") or "rhel" in id_like:
                    pkg = "yum"
            
            return {"os": "linux", "arch": arch, "package_manager": pkg}

    # --- Try Windows via CMD / PowerShell ---
    rc_win, out_win, _ = ssh_manager.execute("cmd.exe /c echo %OS% 2>NUL")
    if rc_win == 0 and "Windows_NT" in out_win:
        return {"os": "windows", "arch": "x86_64", "package_manager": "choco"}

    rc_ps, out_ps, _ = ssh_manager.execute('powershell -Command "[Environment]::OSVersion.Platform" 2>NUL')
    if rc_ps == 0 and "Win32NT" in out_ps:
        return {"os": "windows", "arch": "x86_64", "package_manager": "choco"}

    return {"os": "unknown", "arch": "unknown", "package_manager": "unknown"}
