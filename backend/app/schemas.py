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
    team_id: Optional[str] = None
    phone: str = ""


# ── Epic C, C5: roles, invites, allocation, queue approval ──────────

class TeamIn(BaseModel):
    name: str
    leader_user_id: Optional[str] = None
    tenant_id: Optional[str] = None      # Super Admin only; ignored otherwise


class TeamOut(BaseModel):
    team_id: str
    tenant_id: str
    name: str
    leader_user_id: Optional[str] = None
    member_count: int = 0


class InviteIn(BaseModel):
    email: str
    name: str = ""
    # tenant_admin | team_leader | caller. Super Admin may invite any
    # of the three (with an explicit tenant_id); Tenant Admin may
    # invite Callers into their own tenant only. Super Admin accounts
    # themselves are never created through invites — they are
    # bootstrapped from the platform environment (see seed.py).
    role: str
    team_id: Optional[str] = None
    phone: str = ""
    channel: str = "email"               # email | sms (SMS = fallback)
    tenant_id: Optional[str] = None      # Super Admin only; ignored otherwise


class InviteOut(BaseModel):
    invite_id: str
    tenant_id: str
    email: str
    name: str
    role: str
    team_id: Optional[str] = None
    channel: str
    setup_url: str
    expires_at: Optional[str] = None
    # Returned ONLY when INVITE_DEV_OTP=true (tests). In production
    # the OTP travels only over its channel (log-mode until wired).
    dev_otp: Optional[str] = None


class SetupIn(BaseModel):
    invite_id: str                       # from the invite link (hashes are
                                         # salted, so the token itself is never
                                         # a lookup key — it is verified, not matched)
    token: str                           # single-use setup token from the invite link
    otp: str                             # the Email/SMS OTP from the invite channel
    password: str


class UserPatchIn(BaseModel):
    name: Optional[str] = None
    team_id: Optional[str] = None
    is_active: Optional[bool] = None


class AllocateIn(BaseModel):
    caller_id: Optional[str] = None      # None = unallocate


class DispositionPatchIn(BaseModel):
    disposition: str


class ApproveIn(BaseModel):
    decision: str                        # approved | rejected | escalated
    note: str = ""


class TenantPatchIn(BaseModel):
    timezone: Optional[str] = None
    from_numbers: Optional[List[str]] = None


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
    tenant_name: str = ""


class TenantIn(BaseModel):
    name: str
    market: str = "IN"
    timezone: str = ""                   # IANA name; "" = derive from market (A10)
    provider_mode_default: str = "managed"
    from_numbers: List[str] = []
    concurrency_limit: int = 20


class TenantOut(BaseModel):
    tenant_id: str
    name: str
    market: str
    timezone: str = "Asia/Kolkata"       # market timezone — the schedule clock (A10)
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
    # C5, create-only: the tenant the agent lands in. Required when a
    # Super Admin creates an agent (their own tenant_id is the
    # platform tenant, never the answer); ignored on update.
    tenant_id: Optional[str] = None
    # THE single prompt — behaviour + dialogue + disposition logic, one text.
    prompt: str = ""
    # Structured config only:
    llm_model: str = "sarvam-105b"
    stt_provider: str = "saaras"
    stt_language: str = "hi-IN"
    tts_provider: str = "bulbul"
    tts_voice_id: str = ""
    voice_gender: str = "female"  # female | male — picks the Vapi voice-profile voice ID (A6)
    language_rule: dict = Field(default_factory=dict)
    captured_fields: List[CapturedField] = []
    max_duration_s: int = 300
    max_turns: int = 50  # conversation-loop guard (A8)
    silence_prompt_after_s: int = 8
    silence_max_prompts: int = 1  # unanswered silence prompts before the polite close (A9)


class AgentOut(AgentIn):
    agent_id: str
    tenant_id: str
    prompt_version: int = 1  # current (latest) prompt version number
    # C2: which catalog version this agent currently equals (None when
    # the agent was never adopted, or diverged by a manual prompt edit).
    adopted_catalog_agent: Optional[str] = None
    adopted_catalog_version: Optional[int] = None


class PromptVersionOut(BaseModel):
    version: int
    prompt: str
    note: str = ""
    created_by: str = ""
    created_at: Optional[str] = None


# ── Catalog & adoption (Epic C, C2 — design §11) ─────────────────────────
# A catalog version is IMMUTABLE: there is no update/delete schema for
# versions by design — a fix becomes a new version (publish endpoint).

class CatalogVersionIn(BaseModel):
    prompt: str
    note: str = ""
    # Mismatch guard: "managed" | "self_hosted" | "both".
    suitable_for: str = "both"
    model: str = ""
    # Stack(s) live-call proven on, e.g. "Vapi managed". "Best for X"
    # is claimed ONLY for stacks named here. HISTORY: flipping
    # suitable_for never rewrites this field.
    validated_on: str = ""
    # Full content carried on the version (see models.CatalogVersion).
    # Every field is OPTIONAL here: None means "omitted" and the
    # version-creation resolver fills it from the parent agent row
    # (non-stack config) or by suitable_for (stack providers/model),
    # then validates the final stack. The detail form always sends
    # every field explicitly; the seed immediate-publish path sends
    # only prompt/note/suitable_for/model/validated_on.
    stt_provider: Optional[str] = None
    stt_language: Optional[str] = None
    tts_provider: Optional[str] = None
    tts_voice_id: Optional[str] = None
    voice_gender: Optional[str] = None
    language_rule: Optional[dict] = None
    captured_fields: Optional[List[CapturedField]] = None
    max_duration_s: Optional[int] = None
    max_turns: Optional[int] = None
    silence_prompt_after_s: Optional[int] = None
    silence_max_prompts: Optional[int] = None


class CatalogVersionOut(CatalogVersionIn):
    version_id: str
    catalog_agent_id: str
    version: int
    published_by: str = ""
    published_at: Optional[str] = None
    # Draft/publish split: "draft" iff published_at is NULL, else "published".
    status: str = "published"
    # Computed for the requesting tenant's stack (not stored):
    unproven_on_stack: bool = False      # suitable_for fits neither "both" nor this stack
    fit_label: str = ""                  # "best for managed" | "intended for managed" | ""


class CatalogAgentIn(BaseModel):
    slug: str                            # e.g. "rpi" — lowercase, stable
    name: str
    stage: str = ""
    purpose: str = ""
    # Creation defaults (same shape as AgentIn). A version row carries
    # its own full copy, so adoption reads the version, never this
    # row — these only fill in fields a version creation omits.
    llm_model: str = "sarvam-105b"
    stt_provider: str = "saaras"
    stt_language: str = "hi-IN"
    tts_provider: str = "bulbul"
    tts_voice_id: str = ""
    voice_gender: str = "female"
    language_rule: dict = Field(default_factory=dict)
    captured_fields: List[CapturedField] = []
    max_duration_s: int = 300
    max_turns: int = 50
    silence_prompt_after_s: int = 8
    silence_max_prompts: int = 1
    # First version, created WITH the agent as v1 DRAFT (create-agent
    # form). When present and carrying a non-blank prompt, the agent
    # is never created version-less: v1 is written in the same
    # transaction. When absent (seed flow: agent first, then a
    # separate immediate-publish call), the agent is created alone.
    initial_version: Optional[CatalogVersionIn] = None


class CatalogAgentOut(CatalogAgentIn):
    catalog_agent_id: str
    versions: List[CatalogVersionOut] = []
    # This tenant's view: current adoption + availability flags.
    adopted_version: Optional[int] = None
    latest_version: Optional[int] = None
    new_version_available: bool = False
    # True when no published version suits this tenant's stack.
    unproven_on_stack: bool = False
    tenant_stack: str = ""               # managed | self_hosted | stub


class AdoptionIn(BaseModel):
    catalog_agent_id: str                # slug or catalog_agent_id
    version: Optional[int] = None        # None = latest published
    # Cross-box import mode: a full version payload (from the export
    # endpoint) for tenant boxes that cannot read the platform box's
    # database. When present it wins over the same-DB lookup.
    payload: Optional[dict] = None


class AdoptionOut(BaseModel):
    adoption_id: str
    tenant_id: str
    catalog_agent_id: str                # slug
    catalog_version: int
    agent_id: str
    agent_name: str
    adopted_by: str
    adopted_at: Optional[str] = None


class AdoptionLogOut(BaseModel):
    tenant_id: str
    tenant_slug: str
    catalog_agent_id: str
    catalog_version: int
    adopted_by: str
    adopted_at: Optional[str] = None


# ── Campaigns & leads (A3) ────────────────────────────────────────────────
# Canonical product kinds. The kind decides which identifier the agent uses
# on the call (design §15): vehicle → registration number, credit card →
# card last 4, home/personal loan → loan account number.
PRODUCTS = {
    "vehicle_loan": {"label": "Vehicle loan", "identifier_type": "registration_number",
                     "identifier_label": "Vehicle registration number"},
    "credit_card": {"label": "Credit card", "identifier_type": "card_last4",
                    "identifier_label": "Card last 4 digits"},
    "home_loan": {"label": "Home loan", "identifier_type": "account_number",
                  "identifier_label": "Loan account number"},
    "personal_loan": {"label": "Personal loan", "identifier_type": "account_number",
                      "identifier_label": "Loan account number"},
    "other": {"label": "Other", "identifier_type": "account_number",
              "identifier_label": "Account number"},
}


class ScheduleIn(BaseModel):
    """Calling schedule — configuration only in A3 (dispatch engine is A5/A10)."""
    start_date: str = ""                 # ISO date, e.g. "2026-10-24"
    calling_days: str = "Mon – Sat"
    window_start: str = "09:00"          # local time at the tenant's market
    window_end: str = "19:00"
    daily_cap: int = 2000                # max calls per day across the campaign


class RetryIn(BaseModel):
    max_attempts: int = 3                # per lead
    retry_gap_days: int = 1
    retry_different_time: bool = True    # retry at a different time of day


class RoutingRule(BaseModel):
    disposition: str
    action: str                          # e.g. route_to_campaign | human_queue | none
    target: str = ""                     # e.g. target campaign id / queue name


class CampaignIn(BaseModel):
    """Business fields ONLY (§15): no model/voice/language/prompt here, and
    NEVER a from-number — the campaign names a channel, the platform owns
    the number pool."""
    agent_id: str                        # bound to exactly one agent
    name: str
    product: str = "vehicle_loan"        # a key of PRODUCTS
    product_detail: str = ""
    purpose: str = ""
    audience: str = ""
    channel: str = "Service Channel 1"
    provider_mode: str = "managed"       # managed | self_hosted
    # B4: max discount % the Settlement agent may agree on its own (None = no authority set).
    settlement_authority_pct: Optional[float] = None
    schedule: ScheduleIn = Field(default_factory=ScheduleIn)
    retry_rules: RetryIn = Field(default_factory=RetryIn)
    routing_rules: List[RoutingRule] = []


class CampaignOut(CampaignIn):
    campaign_id: str
    tenant_id: str
    status: str = "draft"                # draft → active → paused → completed
    agent_name: str = ""
    # C2 pinning (display only — the text itself stays server-side):
    pinned_prompt_version: Optional[int] = None
    pinned_catalog_agent: Optional[str] = None
    pinned_catalog_version: Optional[int] = None
    product_label: str = ""
    identifier_type: str = ""            # derived from product
    identifier_label: str = ""
    lead_count: int = 0
    # A4: is a real provider configured for this campaign's mode? A campaign
    # with no provider is ALLOWED (it runs in stub mode) but flagged.
    provider_configured: bool = False
    provider_flag: Optional[str] = None
    created_at: Optional[str] = None


class CampaignStatusIn(BaseModel):
    status: str                          # target status; transitions validated


class LeadOut(BaseModel):
    lead_id: str
    campaign_id: str
    phone_number: str
    name: str = ""
    bank_name: str = ""
    state: str = ""
    agent_fields: dict = Field(default_factory=dict)
    attempt_count: int = 0
    status: str = "pending"
    # A10: when a re-queued lead's next attempt becomes due (ISO, UTC);
    # None when the lead is due now or has reached a final state.
    next_attempt_at: Optional[str] = None
    # C5: case allocation (None = unallocated pool).
    assigned_caller_id: Optional[str] = None
    allocated_by: Optional[str] = None
    allocated_at: Optional[str] = None
    created_at: Optional[str] = None


class LeadListOut(BaseModel):
    total: int
    leads: List[LeadOut]


class UploadResultOut(BaseModel):
    total_rows: int
    created: int
    duplicates_skipped: int
    # C5: caller values from a caller-mapped column that matched no
    # Caller (or matched ambiguously) — those leads stay unallocated.
    unresolved_callers: List[str] = []
    invalid_skipped: int


# ── Provider settings (A4) ────────────────────────────────────────────────
# Secrets are WRITE-ONLY in this API: ProviderSettingIn accepts values;
# ProviderOut carries masked forms only (•••• + last 4) — a full secret is
# never present in any response (enforced by smoke tests with sentinels).

class ProviderSettingIn(BaseModel):
    """Save one provider's tenant settings. Secret fields are write-only:
    send a non-empty value to replace, or omit / send "" to keep the stored
    value (the client never receives the secret, so it cannot echo it back).
    Non-secret fields are set to the sent value ("" clears)."""
    fields: dict[str, str] = Field(default_factory=dict)
    enabled: Optional[bool] = None


class ProviderFieldOut(BaseModel):
    name: str
    label: str
    secret: bool
    required: bool
    is_set: bool
    source: Optional[str] = None         # tenant | env | None
    value: Optional[str] = None          # non-secret fields only
    masked: Optional[str] = None         # secret fields only: •••• + last 4


class ProviderOut(BaseModel):
    provider: str                        # vapi | twilio | plivo | exotel | sarvam
    mode: str                            # managed | self_hosted
    label: str
    description: str = ""
    optional: bool = False
    configured: bool = False
    source: Optional[str] = None         # tenant | env | None
    enabled: bool = True
    # not_configured | stub | configured | connected | failed
    status: str = "not_configured"
    status_detail: str = ""
    fields: List[ProviderFieldOut] = []
    last_test_status: str = ""
    last_test_message: str = ""
    last_test_at: Optional[str] = None


class ProviderTestOut(BaseModel):
    provider: str
    status: str                          # connected | failed | stub
    message: str
    checked_at: str
    latency_ms: int = 0
    persisted: bool = False


# ── Dispatch & calls (A5) ─────────────────────────────────────────────────
# CallOut is the §8.2 CallResult contract, assembled from the session (its
# state, timestamps, transcript, recording) + the terminal result (its
# disposition, captured fields, cost). Fields not yet known (no result
# yet) serialise as None / defaults — the shape never changes between
# modes, which is the point of the contract.

class CallOut(BaseModel):
    session_id: str
    attempt_id: str
    # Nullable from A11: a browser-test session is not a campaign attempt
    # and references neither a campaign nor a lead (see channel below).
    campaign_id: Optional[str] = None
    campaign_name: str = ""
    lead_id: Optional[str] = None
    lead_name: str = ""
    to_number: str = ""
    provider_mode: str                   # managed | self_hosted (the campaign's mode)
    # How the session was originated (A11): "" = a dispatched campaign
    # attempt; "browser-test" = the in-console test harness. A test call
    # carries the same §8.2 shape as any other call and is marked here,
    # never disguised as a campaign attempt.
    channel: str = ""
    provider_mode: str                   # managed | self_hosted (the campaign's mode)
    transport: str = "stub"              # the transport that actually ran it (A5: stub)
    state: str                           # QUEUED | DIALING | IN_CALL | terminal state
    final_state: Optional[str] = None    # §8.2: COMPLETED | NO_ANSWER | BUSY | FAILED | VOICEMAIL
    end_reason: str = ""
    disposition: str = ""
    confidence: float = 0.0
    summary: str = ""
    captured_fields: dict = Field(default_factory=dict)
    duration_s: int = 0
    recording_url: str = ""
    transcript: List[dict] = []
    # Self-hosted STT leg (A7): "" when the call had no STT leg (stub /
    # managed / never streamed); else active | failed | not_configured,
    # plus the Saaras language the session opened with and the failure
    # reason when failed. Transcript turns may carry "partial": true —
    # the live in-flight segment of a call still IN_CALL (in-memory
    # only; finals are persisted as they arrive).
    stt_status: str = ""
    stt_language: str = ""
    stt_detail: str = ""
    # Self-hosted LLM + TTS legs (A8): same status vocabulary as the STT
    # leg ("" when the call never streamed, else active | failed |
    # not_configured) with failure detail. review_required is the §8.2
    # result's flag: classification could not produce a trustworthy
    # disposition (reason in summary) — the disposition stays "" rather
    # than an invented or out-of-set bucket.
    llm_status: str = ""
    llm_detail: str = ""
    tts_status: str = ""
    tts_detail: str = ""
    review_required: bool = False
    cost_breakdown: dict = Field(default_factory=dict)
    started_at: Optional[str] = None
    answered_at: Optional[str] = None
    ended_at: Optional[str] = None


class CallListOut(BaseModel):
    total: int
    counts_by_state: dict = Field(default_factory=dict)
    calls: List[CallOut]


class DispatchOut(BaseModel):
    campaign_id: str
    transport: str                       # stub in A5
    stub: bool
    provider_mode: str
    provider_ready: bool                 # A4: is a real provider configured?
    timezone: str = ""                   # A10: the market timezone the schedule was evaluated in
    outside_window: bool = False
    created: int = 0                     # new attempts admitted by this run
    already_dispatched: int = 0          # leads skipped: attempt already exists
    retry_waiting: int = 0               # A10: leads skipped — their retry gap has not elapsed
    concurrency_capped: int = 0          # A12: leads withheld — the tenant's in-flight calls fill its concurrency limit
    cancelled_inactive: int = 0          # A12: queued attempts withdrawn — the campaign stopped being active mid-dispatch
    finalized: int = 0                   # results written by this run
    placed: int = 0                      # real-transport calls placed (A6) — result lands via webhook / stream
    counts_by_state: dict = Field(default_factory=dict)
    message: str = ""


class ResultIn(BaseModel):
    """The §8.2 result payload, delivered to the idempotent receiver."""
    session_id: str = ""
    attempt_id: str = ""
    provider_mode: str = ""
    final_state: str = "COMPLETED"
    end_reason: str = ""
    disposition: str = ""
    confidence: float = 0.0
    review_required: bool = False        # A10: was accepted by the schema but dropped before — now carried
    summary: str = ""
    captured_fields: dict = Field(default_factory=dict)
    duration_s: int = 0
    recording_url: str = ""
    transcript: List[dict] = []
    cost_breakdown: dict = Field(default_factory=dict)


class ResultOut(BaseModel):
    session_id: str
    attempt_id: str = ""
    created: bool                        # False = duplicate delivery, stored result returned
    call: CallOut


# ── Results surface & dashboard (A10) ─────────────────────────────────────
# The read models the console's Results card and Dashboard are built from.
# Both are assembled from the SAME §8.2 records every mode lands through
# finalize_attempt — that is what "managed-mode parity" means in practice:
# there is no second, mode-specific results shape anywhere.

class CampaignResultsOut(BaseModel):
    campaign_id: str
    agent_id: str = ""
    # The bucket set parsed from the campaign agent's own single prompt
    # (its DISPOSITIONS section) — counts_by_disposition carries every
    # bucket, zero-filled, plus any stored disposition outside the set.
    buckets: List[str] = []
    counts_by_disposition: dict = Field(default_factory=dict)
    attempts_made: int = 0               # sessions created for the campaign
    results_count: int = 0               # sessions with a terminal §8.2 result
    connected: int = 0                   # results whose final_state is COMPLETED
    connect_rate: float = 0.0            # connected / results_count (0 when no results)
    review_required: int = 0             # results flagged for human review
    leads_total: int = 0
    leads_by_state: dict = Field(default_factory=dict)   # pending | done | exhausted
    awaiting_retry: int = 0              # pending leads whose retry gap has not elapsed
    total_duration_s: int = 0
    # A12: real cost totals from the results' cost_breakdowns, grouped
    # per currency (never mixed): [{currency, total, calls,
    # connected_calls, cost_per_connected}]. Stub results are excluded —
    # simulated money is not money.
    costs: List[dict] = []


class DashboardOut(BaseModel):
    timezone: str = ""                   # the tenant's market timezone
    active_campaigns: int = 0
    campaigns_by_status: dict = Field(default_factory=dict)
    calls_today: int = 0                 # sessions started on the market-local today
    calls_total: int = 0
    results_total: int = 0
    dispositions: dict = Field(default_factory=dict)     # tenant-wide totals across campaigns
    review_queue_count: int = 0          # results flagged review_required, all campaigns
    # A12: real cost totals per currency (stub results excluded) —
    # [{currency, total, calls, connected_calls, cost_per_connected}].
    costs: List[dict] = []


# ── Browser test harness (A11) ────────────────────────────────────────────
# An operator talks to an agent from the browser — no phone, no telephony
# provider — to hear and judge it before any live call. Creating a test
# makes a CallSession with channel "browser-test", campaign_id / lead_id
# NULL, and the test-lead SNAPSHOT on the session (never a lead row: a
# test consumes no campaign lead's attempts and triggers no retry).

class BrowserTestLeadIn(BaseModel):
    """Hand-typed test-lead values (fictitious). Used when no saved lead
    is picked; any field may be left blank."""
    name: str = ""
    phone_number: str = ""
    bank_name: str = ""
    state: str = ""
    product_identifier: str = ""


class BrowserTestIn(BaseModel):
    agent_id: str
    # Optional context: a campaign of that agent (supplies the product +
    # lead list), and optionally one of its saved leads to test against.
    campaign_id: Optional[str] = None
    lead_id: Optional[str] = None       # requires campaign_id
    lead: Optional[BrowserTestLeadIn] = None   # hand-typed values


class BrowserTestOut(BaseModel):
    session_id: str
    channel: str = "browser-test"
    state: str = "QUEUED"
    agent_id: str
    agent_name: str = ""
    lead_name: str = ""
    to_number: str = ""
    # The harness media endpoint for this session (token passed as a
    # query parameter — browsers cannot set WebSocket headers).
    stream_path: str = ""
