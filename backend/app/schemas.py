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
    schedule: ScheduleIn = Field(default_factory=ScheduleIn)
    retry_rules: RetryIn = Field(default_factory=RetryIn)
    routing_rules: List[RoutingRule] = []


class CampaignOut(CampaignIn):
    campaign_id: str
    tenant_id: str
    status: str = "draft"                # draft → active → paused → completed
    agent_name: str = ""
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
    created_at: Optional[str] = None


class LeadListOut(BaseModel):
    total: int
    leads: List[LeadOut]


class UploadResultOut(BaseModel):
    total_rows: int
    created: int
    duplicates_skipped: int
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
