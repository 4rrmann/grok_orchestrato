"""
Request and Response schemas for the /ask-ai endpoint.

These define the public-facing API contract that clients interact with.
The AI request schema intentionally mirrors what Grok expects so clients
don't need to transform their payloads — we forward compatible fields
and reject unknown ones.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict


class Message(BaseModel):
    """A single message in a conversation turn."""
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1)


class AIRequest(BaseModel):
    """
    What the client sends to our /ask-ai endpoint.

    We accept a subset of Grok's API parameters and layer our own
    orchestration concerns (like max_retries) on top.
    """
    messages: list[Message] = Field(
        ...,
        min_length=1,
        description="Conversation history; must contain at least one message",
    )
    model: Optional[str] = Field(
        default=None,
        description="Override the default model (e.g. 'grok-3-mini')",
    )
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="Sampling temperature — higher = more creative",
    )
    max_tokens: int = Field(
        default=1024,
        ge=1,
        le=32768,
        description="Maximum tokens to generate in the response",
    )
    stream: bool = Field(
        default=False,
        description="Whether to stream the response (currently not supported)",
    )
    # Orchestration hint from the caller — they can request fewer retries
    # for latency-sensitive use cases.
    max_retries: Optional[int] = Field(
        default=None,
        ge=1,
        le=10,
        description="Override the default MAX_RETRIES setting for this request",
    )


class UsageStats(BaseModel):
    """Token consumption reported by the Grok API."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AIResponse(BaseModel):
    """
    What our /ask-ai endpoint returns to the client.

    We enrich Grok's raw response with metadata about how the request
    was served — which key was used (alias only, never the raw key),
    how many retries were needed, and performance metrics.
    This metadata is invaluable for debugging and capacity planning.
    """
    model_config = ConfigDict(from_attributes=True)

    content: str = Field(description="The AI-generated text response")
    model: str = Field(description="The model that produced the response")
    usage: UsageStats = Field(default_factory=UsageStats)

    # Orchestration metadata
    key_alias: str = Field(description="Alias of the API key that served this request")
    attempts: int = Field(description="How many keys were tried before success")
    latency_ms: float = Field(description="Total end-to-end latency in milliseconds")

    # Optional Grok-native fields passed through
    finish_reason: Optional[str] = None
    raw_response_id: Optional[str] = None


class ErrorResponse(BaseModel):
    """Standard error envelope returned on failure."""
    error: str
    detail: Optional[str] = None
    request_id: Optional[str] = None
