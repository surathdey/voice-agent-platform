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
    # Market timezone (IANA name) — the clock every schedule question is
    # answered in (A10): calling windows/days, start dates, daily caps,
    # retry due times. Set from the market at provisioning
    # (app.market_time.MARKET_TIMEZONES), stored resolved so it can be
    # corrected per tenant without a code change.
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
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
    # Voice gender of the agent persona (female | male). In managed mode
    # the Vapi transport uses it to pick the per-language voice profile's
    # female / male voice ID from the tenant's Vapi settings (A6); in
    # self-hosted mode the Bulbul voice id conventionally encodes it.
    voice_gender: Mapped[str] = mapped_column(String(10), default="female")
    # e.g. {"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}}
    language_rule: Mapped[dict] = mapped_column(JSON, default=dict)
    # e.g. [{"name": "promise_date", "type": "date", "description": "..."}]
    captured_fields: Mapped[list] = mapped_column(JSON, default=list)
    max_duration_s: Mapped[int] = mapped_column(Integer, default=300)
    # Conversation-loop guard (A8): the self-hosted engine ends the call
    # honestly (end_reason max_turns) once this many agent turns have run.
    max_turns: Mapped[int] = mapped_column(Integer, default=50)
    silence_prompt_after_s: Mapped[int] = mapped_column(Integer, default=8)
    # Silence prompts before the polite close (A9, design §5.2): after
    # this many unanswered silence prompts the engine speaks the
    # prompt's closing and ends the call (end_reason no_input).
    # Default 1 = the design's behaviour: one prompt, a second silence
    # closes the call.
    silence_max_prompts: Mapped[int] = mapped_column(Integer, default=1)

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
    """One campaign is bound to exactly ONE agent (design §11.2, decision 14.1).

    Campaign screens set BUSINESS fields only (design §15 rules): name,
    product, purpose, audience, a named CHANNEL (never a phone number — the
    platform manages the number pool underneath), provider mode, schedule and
    retry rules. Nothing agent-level lives here: no model, voice, language
    rule or prompt — those belong to the AgentConfig the campaign references.
    """

    __tablename__ = "campaigns"

    campaign_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cmp"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_configs.agent_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Canonical product kind (see schemas.PRODUCTS): vehicle_loan | credit_card
    # | home_loan | personal_loan | other. The kind decides which identifier
    # the agent uses on the call (registration number / card last 4 / account no.).
    product: Mapped[str] = mapped_column(String(40), default="vehicle_loan")
    product_detail: Mapped[str] = mapped_column(String(300), default="")  # e.g. "two-wheeler (stale records)"
    purpose: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(Text, default="")  # who the campaign is for
    # Named channel — "Service Channel 1", "Service Channel 2", … NEVER a number.
    channel: Mapped[str] = mapped_column(String(120), default="Service Channel 1")
    provider_mode: Mapped[str] = mapped_column(String(20), default="managed")  # managed | self_hosted
    status: Mapped[str] = mapped_column(String(30), default="draft")  # draft → active → paused → completed
    schedule: Mapped[dict] = mapped_column(JSON, default=dict)       # start date, calling days/window, daily cap
    retry_rules: Mapped[dict] = mapped_column(JSON, default=dict)    # max attempts, retry gap
    routing_rules: Mapped[list] = mapped_column(JSON, default=list)  # [{disposition, action, ...}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Lead(Base):
    __tablename__ = "leads"

    lead_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("led"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.campaign_id"), nullable=False, index=True)
    phone_number: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    bank_name: Mapped[str] = mapped_column(String(200), default="")
    state: Mapped[str] = mapped_column(String(120), default="")
    # agent_fields holds every mapped non-core column, PLUS the product
    # identifier under canonical keys (A3): "product_identifier" (value) and
    # "product_identifier_type" (registration_number | card_last4 | account_number).
    agent_fields: Mapped[dict] = mapped_column(JSON, default=dict)   # keys defined by the agent
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    # Retry scheduling (A10): when an attempt ends no-answer / busy /
    # failed with attempts remaining, finalize_attempt re-queues the lead
    # (status back to "pending" — the only status dispatch admits) and
    # stamps this with when the next attempt becomes due (naive UTC):
    # ended_at + the campaign's retry gap, with the different-time-of-day
    # shift applied in market-local time when configured. NULL = due now
    # (fresh lead) or not awaiting a retry (final state reached).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CallSession(Base):
    """One dispatch of one lead (design §6 state machine)."""

    __tablename__ = "call_sessions"

    session_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ses"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    # Nullable from A11: a browser-test session is not a campaign attempt
    # — it carries no campaign and no lead (the test context lives in
    # test_lead below), so campaign/lead are NULL for harness calls.
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.campaign_id"), nullable=True)
    lead_id: Mapped[str | None] = mapped_column(ForeignKey("leads.lead_id"), nullable=True)
    attempt_id: Mapped[str] = mapped_column(String(60), nullable=False)  # idempotency key from app layer
    provider_mode: Mapped[str] = mapped_column(String(20), default="managed")
    # How this session was originated (A11): "" = a dispatched campaign
    # attempt; "browser-test" = the in-console test harness. The marker
    # rides the §8.2 surface (CallOut.channel) so a test call is never
    # mistaken for a campaign attempt anywhere it appears.
    channel: Mapped[str] = mapped_column(String(30), default="")
    # Browser-test lead snapshot (A11): the fictitious / picked lead the
    # operator tested against — {agent_id, name, phone_number, bank_name,
    # state, agent_fields, product, source_campaign_id, source_lead_id,
    # source_campaign_name}. A snapshot (not a lead row) by design: a test
    # never creates, consumes or re-queues a campaign lead. Empty for
    # dispatched attempts.
    test_lead: Mapped[dict] = mapped_column(JSON, default=dict)
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
    # Self-hosted STT leg (A7): the Sarvam Saaras session opened for this
    # call. stt_status: "" (no STT leg — stub / managed / never streamed)
    # | active | failed | not_configured. stt_detail carries the reason
    # when failed (exception class / short label — never provider bodies).
    stt_status: Mapped[str] = mapped_column(String(20), default="")
    stt_language: Mapped[str] = mapped_column(String(20), default="")
    stt_detail: Mapped[str] = mapped_column(String(500), default="")
    # Self-hosted LLM + TTS legs (A8): the conversation brain that ran
    # beside the STT leg. Same vocabulary as stt_status ("" = the call
    # never streamed; active | failed | not_configured) with the same
    # detail discipline — exception class / short label only.
    llm_status: Mapped[str] = mapped_column(String(20), default="")
    llm_detail: Mapped[str] = mapped_column(String(500), default="")
    tts_status: Mapped[str] = mapped_column(String(20), default="")
    tts_detail: Mapped[str] = mapped_column(String(500), default="")

    __table_args__ = (UniqueConstraint("tenant_id", "attempt_id", name="uq_session_attempt"),)


class ProviderSetting(Base):
    """Per-tenant provider credentials/config (A4 — design §7 “Provider
    settings”, §8.5). One row per (tenant, provider).

    `fields` holds ALL configured values for the provider — identifiers
    (auth id, account sid, subdomain) AND secrets (tokens, API keys). This
    table is the secret store: values are server-side only. No API response
    ever serialises a secret value — reads return masked forms only
    (app.providers.mask), and secrets are never written to logs.

    Resolution (app.providers.get_provider_config): a tenant value wins; a
    missing tenant value falls back to the platform .env; TELEPHONY_MODE=stub
    remains the global fallback when nothing is configured anywhere.
    """

    __tablename__ = "provider_settings"

    setting_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pset"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)  # vapi | twilio | plivo | exotel | sarvam
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    # Last Test-connection outcome (sanitised message — never contains a secret).
    last_test_status: Mapped[str] = mapped_column(String(20), default="")  # connected | failed | stub
    last_test_message: Mapped[str] = mapped_column(String(500), default="")
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "provider", name="uq_tenant_provider"),)


class CallResult(Base):
    """Terminal record for a session — design §8.2, exactly one per session."""

    __tablename__ = "call_results"

    result_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("res"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("call_sessions.session_id"), unique=True, nullable=False)
    disposition: Mapped[str] = mapped_column(String(60), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Review flag (A8): set when the post-call classification could not
    # produce a trustworthy disposition (classifier down, unparseable
    # output, or a bucket outside the agent prompt's own set — an
    # unknown bucket is NEVER stored as the disposition). The reason
    # rides in summary; the Human Queue (Phase B) consumes this flag.
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    captured_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    cost_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)  # {telephony, stt, llm, tts, total, currency}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
