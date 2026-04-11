"""
ssh_manager.py — SSH command execution abstraction for the dashboard.

Built on paramiko (already a dependency). Provides a reusable connection
wrapper with retry logic, used by server_lifecycle.py and compatibility.py.
"""
import logging
import threading
from typing import Optional, Tuple, Dict

import paramiko

log = logging.getLogger(__name__)

# ── Connection cache (one per server) ─────────────────────────────────────────
_connections: Dict[str, "SSHManager"] = {}
_cache_lock = threading.Lock()


def get_ssh(server: dict) -> Optional["SSHManager"]:
    """
    Get or create an SSHManager for a server record.
    Returns None if SSH info is missing.
    """
    ssh_host = server.get("ssh_host", "") or server.get("host", "")
    ssh_user = server.get("ssh_user", "")
    ssh_port = int(server.get("ssh_port", 22))
    ssh_key = server.get("ssh_key_path", "")

    if not ssh_host or not ssh_user:
        return None

    # Strip http(s):// from host if present (dashboard stores agent URL)
    if "://" in ssh_host:
        ssh_host = ssh_host.split("://", 1)[1].split(":")[0].split("/")[0]

    cache_key = f"{ssh_user}@{ssh_host}:{ssh_port}"

    with _cache_lock:
        mgr = _connections.get(cache_key)
        if mgr and mgr.is_connected():
            return mgr

        mgr = SSHManager(
            host=ssh_host,
            port=ssh_port,
            username=ssh_user,
            key_path=ssh_key or None,
        )
        _connections[cache_key] = mgr
        return mgr


class SSHManager:
    """
    Wraps paramiko SSH operations with auto-reconnect and retry logic.
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "root",
        password: Optional[str] = None,
        key_path: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_path = key_path
        self._client: Optional[paramiko.SSHClient] = None
        self._lock = threading.Lock()

    def _connect(self) -> None:
        """Establish SSH connection."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = dict(
            hostname=self.host,
            port=self.port,
            username=self.username,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
        )
        if self.key_path:
            kwargs["key_filename"] = self.key_path
        elif self.password:
            kwargs["password"] = self.password
            kwargs["look_for_keys"] = False
        # else: rely on SSH agent / default keys

        client.connect(**kwargs)
        self._client = client
        log.info(f"SSH connected to {self.username}@{self.host}:{self.port}")

    def is_connected(self) -> bool:
        """Check if the SSH connection is alive."""
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def close(self) -> None:
        """Close the SSH connection."""
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def execute(
        self, cmd: str, timeout: int = 30, retries: int = 1
    ) -> Tuple[int, str, str]:
        """
        Execute a remote command.

        Returns: (exit_code, stdout, stderr)
        Retries once on connection failure.
        """
        for attempt in range(retries + 1):
            try:
                with self._lock:
                    if not self.is_connected():
                        self._connect()

                    stdin, stdout, stderr = self._client.exec_command(
                        cmd, timeout=timeout
                    )
                    exit_code = stdout.channel.recv_exit_status()
                    out = stdout.read().decode("utf-8", errors="replace").strip()
                    err = stderr.read().decode("utf-8", errors="replace").strip()
                    return exit_code, out, err

            except Exception as e:
                log.warning(
                    f"SSH execute failed (attempt {attempt + 1}): {e}"
                )
                self.close()
                if attempt >= retries:
                    return -1, "", str(e)

        return -1, "", "All retries exhausted"

    def is_reachable(self) -> bool:
        """Quick test: can we connect via SSH?"""
        try:
            with self._lock:
                if not self.is_connected():
                    self._connect()
            return True
        except Exception:
            return False
