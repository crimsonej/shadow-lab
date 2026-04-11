"""
structured_logger.py — JSON-structured logging with request tracking.

Provides:
  - StructuredLogger: writes JSON log lines to a persistent file
  - RequestIdMiddleware: FastAPI middleware that assigns X-Request-Id per request
  - In-memory ring buffer for the /logs/recent endpoint
"""
import json
import logging
import time
import uuid
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

import config

log = logging.getLogger(__name__)

# ── Thread-safe ring buffer for recent logs ──────────────────────────────────

_buffer_lock = threading.Lock()
_log_buffer: deque = deque(maxlen=config.LOG_BUFFER_SIZE)


def _append_entry(entry: Dict[str, Any]) -> None:
    with _buffer_lock:
        _log_buffer.append(entry)


def get_recent_logs(limit: int = 100) -> List[Dict[str, Any]]:
    """Return the most recent N log entries from the ring buffer."""
    with _buffer_lock:
        entries = list(_log_buffer)
    # Return newest first
    return list(reversed(entries[-limit:]))


def get_error_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent ERROR-level entries."""
    with _buffer_lock:
        entries = list(_log_buffer)
    errors = [e for e in entries if e.get("level") in ("ERROR", "CRITICAL")]
    return list(reversed(errors[-limit:]))


# ── Structured Logger ─────────────────────────────────────────────────────────

class StructuredLogger:
    """
    Writes JSON-formatted log entries to a persistent log file
    and the in-memory ring buffer simultaneously.
    """

    def __init__(self, log_path: Optional[Path] = None):
        self._path = log_path or config.LOG_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file_lock = threading.Lock()

    def _write(self, entry: Dict[str, Any]) -> None:
        """Append entry to both file and ring buffer."""
        _append_entry(entry)
        try:
            with self._file_lock:
                with open(self._path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # Never let logging crash the application

    def log(
        self,
        level: str,
        message: str,
        request_id: Optional[str] = None,
        server_id: Optional[str] = None,
        **extra,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level.upper(),
            "message": message,
            "request_id": request_id,
            "server_id": server_id,
        }
        if extra:
            entry["extra"] = extra
        self._write(entry)

    def info(self, message: str, **kwargs) -> None:
        self.log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs) -> None:
        self.log("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs) -> None:
        self.log("ERROR", message, **kwargs)

    def debug(self, message: str, **kwargs) -> None:
        self.log("DEBUG", message, **kwargs)


# ── Singleton instance ────────────────────────────────────────────────────────

_logger_instance: Optional[StructuredLogger] = None


def get_logger() -> StructuredLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = StructuredLogger()
    return _logger_instance


# ── FastAPI Middleware ─────────────────────────────────────────────────────────

class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Assigns a unique X-Request-Id to every request and logs request lifecycle.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id", str(uuid.uuid4())[:12])
        request.state.request_id = request_id

        slog = get_logger()
        method = request.method
        path = request.url.path

        start = time.monotonic()
        slog.info(
            f"{method} {path}",
            request_id=request_id,
            method=method,
            path=path,
        )

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            slog.error(
                f"{method} {path} → 500 ({elapsed:.0f}ms)",
                request_id=request_id,
                method=method,
                path=path,
                latency_ms=round(elapsed, 1),
                error=str(exc),
            )
            raise

        elapsed = (time.monotonic() - start) * 1000
        slog.info(
            f"{method} {path} → {response.status_code} ({elapsed:.0f}ms)",
            request_id=request_id,
            method=method,
            path=path,
            status_code=response.status_code,
            latency_ms=round(elapsed, 1),
        )

        response.headers["X-Request-Id"] = request_id
        return response
