"""API schemas (Pydantic)."""
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    user_id: str
    tenant_id: str
    email: str
    name: str
    role: str


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
    tenant_name: str = ""


class TenantIn(BaseModel):
    name: str
    market: str = "IN"
    provider_mode_default: str = "managed"
    from_numbers: List[str] = []
    concurrency_limit: int = 20


class TenantOut(BaseModel):
    tenant_id: str
    name: str
    market: str
    provider_mode_default: str
    from_numbers: List[str] = []
    concurrency_limit: int = 20
    recording_retention_days: int = 90


class CapturedField(BaseModel):
    name: str
    type: str = "string"  # string | date | number | boolean
    description: str = ""


class AgentIn(BaseModel):
    name: str
    stage: str = ""
    purpose: str = ""
    is_reference: bool = False
    # THE single prompt — behaviour + dialogue + disposition logic, one text.
    prompt: str = ""
    # Structured config only:
    llm_model: str = "sarvam-105b"
    stt_provider: str = "saaras"
    stt_language: str = "hi-IN"
    tts_provider: str = "bulbul"
    tts_voice_id: str = ""
    language_rule: dict = Field(default_factory=dict)
    captured_fields: List[CapturedField] = []
    max_duration_s: int = 300
    silence_prompt_after_s: int = 8


class AgentOut(AgentIn):
    agent_id: str
    tenant_id: str
    prompt_version: int = 1  # current (latest) prompt version number


class PromptVersionOut(BaseModel):
    version: int
    prompt: str
    note: str = ""
    created_by: str = ""
    created_at: Optional[str] = None
