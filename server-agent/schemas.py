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
    model: str
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
