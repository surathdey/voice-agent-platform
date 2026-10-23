"""Core data model — Detailed Design V2, Section 7 (class diagram g6).

All six platform entities are defined here. Every entity carries tenant_id
and every query in the API layer is tenant-scoped (A1 acceptance: tenants
cannot see each other's data).

DESIGN CHANGE (owner, 2026-10-23) — SINGLE PROMPT: AgentConfig has exactly
ONE prompt text field holding the agent's complete behaviour — dialogue/flow
AND the full disposition logic. There is deliberately NO script_templates
table and NO disposition_rules structure; the post-call classifier reads its
disposition definitions from the same single prompt text. Structured config
is only: voice, model, language rule, captured-fields list.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ten"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    market: Mapped[str] = mapped_column(String(4), default="IN")  # NA | IN
    provider_mode_default: Mapped[str] = mapped_column(String(20), default="managed")
    # Secret REFERENCES only (e.g. vault paths) — never raw keys in this table.
    provider_credential_refs: Mapped[dict] = mapped_column(JSON, default=dict)
    from_numbers: Mapped[list] = mapped_column(JSON, default=list)
    concurrency_limit: Mapped[int] = mapped_column(Integer, default=20)
    recording_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(40), default="operator")  # admin | operator
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AgentConfig(Base):
    """The agent as data (design §9), with the owner's single-prompt change."""

    __tablename__ = "agent_configs"

    agent_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("agt"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    stage: Mapped[str] = mapped_column(String(120), default="")       # journey stage label
    purpose: Mapped[str] = mapped_column(String(300), default="")     # one-line summary (card subtitle)
    is_reference: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── THE single prompt: behaviour + dialogue/flow + FULL disposition logic.
    prompt: Mapped[str] = mapped_column(Text, default="")

    # ── Structured configuration (not prompt text) ──
    llm_model: Mapped[str] = mapped_column(String(120), default="sarvam-105b")
    stt_provider: Mapped[str] = mapped_column(String(60), default="saaras")
    stt_language: Mapped[str] = mapped_column(String(20), default="hi-IN")
    tts_provider: Mapped[str] = mapped_column(String(60), default="bulbul")
    tts_voice_id: Mapped[str] = mapped_column(String(120), default="")
    # e.g. {"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}}
    language_rule: Mapped[dict] = mapped_column(JSON, default=dict)
    # e.g. [{"name": "promise_date", "type": "date", "description": "..."}]
    captured_fields: Mapped[list] = mapped_column(JSON, default=list)
    max_duration_s: Mapped[int] = mapped_column(Integer, default=300)
    silence_prompt_after_s: Mapped[int] = mapped_column(Integer, default=8)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PromptVersion(Base):
    """Append-only prompt history (owner refinement, 2026-10-23).

    Every save that changes an agent's prompt creates a new version row.
    Rollback never edits history: it appends a NEW version whose text equals
    the chosen older version. The agent's live prompt == the latest version.
    """

    __tablename__ = "prompt_versions"

    version_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pv"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_configs.agent_id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, 3… per agent
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(String(300), default="")  # e.g. "rollback to v2"
    created_by: Mapped[str] = mapped_column(String(200), default="")  # user email
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_prompt_version"),)


class Campaign(Base):
    __tablename__ = "campaigns"

    campaign_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cmp"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_configs.agent_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    product: Mapped[str] = mapped_column(String(120), default="")   # credit card | vehicle loan | home loan …
    purpose: Mapped[str] = mapped_column(Text, default="")
    provider_mode: Mapped[str] = mapped_column(String(20), default="managed")  # managed | self_hosted
    status: Mapped[str] = mapped_column(String(30), default="draft")
    schedule: Mapped[dict] = mapped_column(JSON, default=dict)       # calling windows etc.
    retry_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    routing_rules: Mapped[list] = mapped_column(JSON, default=list)  # [{disposition, action, ...}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Lead(Base):
    __tablename__ = "leads"

    lead_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("led"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.campaign_id"), nullable=False, index=True)
    phone_number: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    bank_name: Mapped[str] = mapped_column(String(200), default="")
    state: Mapped[str] = mapped_column(String(120), default="")
    agent_fields: Mapped[dict] = mapped_column(JSON, default=dict)   # keys defined by the agent
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CallSession(Base):
    """One dispatch of one lead (design §6 state machine)."""

    __tablename__ = "call_sessions"

    session_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ses"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.campaign_id"), nullable=False)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.lead_id"), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(60), nullable=False)  # idempotency key from app layer
    provider_mode: Mapped[str] = mapped_column(String(20), default="managed")
    provider_call_id: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(30), default="QUEUED")
    from_number: Mapped[str] = mapped_column(String(30), default="")
    to_number: Mapped[str] = mapped_column(String(30), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recording_url: Mapped[str] = mapped_column(String(500), default="")
    end_reason: Mapped[str] = mapped_column(String(60), default="")
    transcript: Mapped[list] = mapped_column(JSON, default=list)  # [{speaker, text, offset_s}]

    __table_args__ = (UniqueConstraint("tenant_id", "attempt_id", name="uq_session_attempt"),)


class CallResult(Base):
    """Terminal record for a session — design §8.2, exactly one per session."""

    __tablename__ = "call_results"

    result_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("res"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("call_sessions.session_id"), unique=True, nullable=False)
    disposition: Mapped[str] = mapped_column(String(60), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[str] = mapped_column(Text, default="")
    captured_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    cost_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)  # {telephony, stt, llm, tts, total, currency}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
