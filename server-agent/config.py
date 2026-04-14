"""
config.py — Runtime configuration loaded from environment variables.
"""
import os
import secrets
from pathlib import Path
from typing import Optional

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


# ── Active Model & Lifecycle State Persistence ────────────────────────────────
import json
import time

STATE_FILE = DATA_DIR / "state.json"
_STATE_CACHE: Optional[dict] = None

def _load_state() -> dict:
    global _STATE_CACHE
    if _STATE_CACHE is not None:
        return _STATE_CACHE

    if STATE_FILE.exists():
        try:
            _STATE_CACHE = json.loads(STATE_FILE.read_text())
            return _STATE_CACHE
        except (IOError, json.JSONDecodeError):
            pass
    
    _STATE_CACHE = {
        "active_model": "",
        "loaded_models": [],
        "last_switch": 0
    }
    return _STATE_CACHE

def _save_state(state: dict):
    global _STATE_CACHE
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
        _STATE_CACHE = state
    except IOError:
        pass

def get_active_model() -> str:
    """Read the currently active model from persistent JSON state."""
    state = _load_state()
    return state.get("active_model", "")

def set_active_model(model: str):
    """Save the active model to JSON state and update tracking."""
    state = _load_state()
    state["active_model"] = model
    state["last_switch"] = time.time()
    _save_state(state)

def get_loaded_models() -> list:
    """Return the list of models we consider 'loaded' according to state.json."""
    state = _load_state()
    return state.get("loaded_models", [])

def update_loaded_models(models: list):
    """Update the set of models recorded as loaded in VRAM."""
    state = _load_state()
    state["loaded_models"] = models
    _save_state(state)

