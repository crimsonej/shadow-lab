"""
config.py — Runtime configuration loaded from environment variables.
"""
import os
import secrets
from pathlib import Path

# Where Ollama listens locally on the server
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

# Port this agent listens on
AGENT_PORT: int = int(os.getenv("AGENT_PORT", "8080"))

# Host binding (0.0.0.0 = accessible from outside)
AGENT_HOST: str = os.getenv("AGENT_HOST", "0.0.0.0")

# Path to the JSON file that persists API keys on disk
KEYS_FILE: Path = Path(os.getenv("KEYS_FILE", "/etc/ollama-agent/keys.json"))

# Master admin token used to generate/revoke API keys via the dashboard.
# Set this in .env or the environment before first run.
# If absent we generate one at startup and print it.
ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

# Maximum concurrent Ollama requests before we queue
MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "4"))

# Whether to enable GPU monitoring (requires nvidia-smi or rocm-smi)
GPU_MONITORING: bool = os.getenv("GPU_MONITORING", "true").lower() == "true"

# Rate-limit: max requests per minute per API key (0 = unlimited)
RATE_LIMIT_RPM: int = int(os.getenv("RATE_LIMIT_RPM", "0"))

# ── New: Control Plane Extensions ─────────────────────────────────────────────

# Persistent data directory for uptime tracking, logs, etc.
DATA_DIR: Path = Path(os.getenv("DATA_DIR", "/var/lib/ollama-agent"))

# Default test prompt used by model health checks
TEST_PROMPT: str = os.getenv(
    "TEST_PROMPT", "Reply with OK if you are functioning correctly"
)

# Maximum structured log entries kept in the in-memory ring buffer
LOG_BUFFER_SIZE: int = int(os.getenv("LOG_BUFFER_SIZE", "500"))

# Structured log file path
LOG_FILE: Path = DATA_DIR / "agent.log"

# Ensure data directory exists at import time
DATA_DIR.mkdir(parents=True, exist_ok=True)

def ensure_admin_token() -> str:
    """Return the admin token, generating+printing one if not set."""
    global ADMIN_TOKEN
    if not ADMIN_TOKEN:
        ADMIN_TOKEN = secrets.token_hex(32)
        print(f"\n{'='*60}")
        print(f"  ADMIN TOKEN (save this — shown only once):")
        print(f"  {ADMIN_TOKEN}")
        print(f"{'='*60}\n")
    return ADMIN_TOKEN


# ── Active Model State Persistence ────────────────────────────────────────────

ACTIVE_MODEL_FILE = DATA_DIR / "active_model.txt"

def get_active_model() -> str:
    """Read the currently active model from disk, or empty string if none."""
    if ACTIVE_MODEL_FILE.exists():
        try:
            return ACTIVE_MODEL_FILE.read_text().strip()
        except IOError:
            pass
    return ""

def set_active_model(model: str):
    """Save the active model to disk."""
    try:
        ACTIVE_MODEL_FILE.write_text(model)
    except IOError:
        pass
