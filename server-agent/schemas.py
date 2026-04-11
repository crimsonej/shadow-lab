"""
schemas.py — Pydantic models for all request and response bodies.
"""
from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field


# ── OpenAI-compatible ─────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    stream: bool = False


# ── Key management ────────────────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    label: str = ""
    limit_rpm: int = Field(default=0, ge=0)  # 0 = unlimited


class RevokeKeyRequest(BaseModel):
    key: str


class DeleteKeyRequest(BaseModel):
    key: str


# ── Model management ──────────────────────────────────────────────────────────

class PullModelRequest(BaseModel):
    name: str  # e.g. "llama3:8b"


class DeleteModelRequest(BaseModel):
    name: str


# ── Generic responses ─────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str               # "ok" | "degraded"
    ollama_running: bool
    agent_version: str
    uptime_seconds: float


class ErrorResponse(BaseModel):
    error: Dict[str, Any]     # OpenAI-style: {"message": "...", "type": "..."}


# ── Control Plane Extensions ──────────────────────────────────────────────────

class TestModelRequest(BaseModel):
    name: str                 # Model name, e.g. "llama3:8b"
    prompt: str = ""          # Custom prompt (empty = use config default)


class TestModelResponse(BaseModel):
    model: str
    status: str               # "pass" | "fail"
    response_text: Optional[str] = None
    latency_ms: float = 0
    tokens: Optional[Dict[str, int]] = None
    error: Optional[str] = None


class TestApiRequest(BaseModel):
    model: str                # Which model to use for the API test


class TestApiResponse(BaseModel):
    status: str               # "PASS" | "FAIL"
    model: str
    tests: List[Dict[str, Any]]
    passed_count: int = 0
    failed_count: int = 0
    total_latency_ms: float = 0


class LifecycleActionRequest(BaseModel):
    action: str = "restart"   # "restart" | "status"


class LogQueryParams(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


# ── Active Model Management ───────────────────────────────────────────────────

class SelectModelRequest(BaseModel):
    model: str


class LoadModelRequest(BaseModel):
    model: str


class ApiKeyTestRequest(BaseModel):
    api_key: str
    model: Optional[str] = None


class ApiKeyTestResponse(BaseModel):
    status: str
    model_used: str
    response: Optional[str] = None
    latency_ms: float = 0
    error: Optional[str] = None
