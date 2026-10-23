"""Campaigns & Leads API (A3) — design §8.5 / §11.2 row A3, §15 rules.

A campaign is bound to exactly ONE agent and carries BUSINESS configuration
only: product / purpose / audience, a named CHANNEL (never a phone number),
provider mode, schedule and retry rules. Nothing agent-level (model, voice,
language rule, the single prompt) is settable here — campaigns reference the
agent; the prompt is edited only in the Agent Config Studio (A2).

Leads arrive as CSV — one separate list per campaign, in the format of the
campaign's agent (RPI: callee_name · phone_number · bank_name · state; the
bank allocation format: ACCOUNT_NO · CUSTOMER_NAME · PRINCIPAL · OUTSTANDING
· MOBILE_NO · RegistrationNumber). Upload is two steps: `preview` (headers,
sample rows, suggested mapping) then `upload` (file + confirmed mapping).
The mapped product-identifier column is stored in agent_fields under the
canonical keys product_identifier / product_identifier_type, typed by the
campaign's product (design §15).
"""
import csv
import io
import json
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from .. import providers as prov
from ..db import get_db
from ..deps import get_current_user
from ..models import AgentConfig, Campaign, Lead, User
from ..schemas import (
    PRODUCTS,
    CampaignIn,
    CampaignOut,
    CampaignStatusIn,
    LeadListOut,
    LeadOut,
    UploadResultOut,
)

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

# Status lifecycle (A3): draft → active ⇄ paused → completed (terminal).
TRANSITIONS = {
    "draft": {"active"},
    "active": {"paused", "completed"},
    "paused": {"active", "completed"},
    "completed": set(),
}

CORE_TARGETS = {"name", "phone_number", "bank_name", "state", "product_identifier"}
ALL_TARGETS = CORE_TARGETS | {"agent_field", "ignore"}

# Header aliases used to SUGGEST a mapping in the preview (operator confirms).
ALIASES = {
    "name": {"callee_name", "customer_name", "name", "customer", "full_name"},
    "phone_number": {"phone_number", "mobile_no", "mobile", "phone", "contact_number", "mobile_number"},
    "bank_name": {"bank_name", "bank", "lender_name", "institution"},
    "state": {"state", "state_province", "province", "state/province"},
}
IDENTIFIER_ALIASES = {
    "registration_number": {"registrationnumber", "registration_number", "reg_no", "vehicle_no", "registration_no"},
    "card_last4": {"card_last4", "card_last_4", "last4", "card_number_last4"},
    "account_number": {"account_no", "account_number", "loan_account_no", "account", "loan_no"},
}


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


def _suggest_mapping(headers: list[str], identifier_type: str) -> dict:
    """column name -> target, guessed from header aliases (preview only)."""
    out = {}
    used = set()
    id_aliases = IDENTIFIER_ALIASES.get(identifier_type, set())
    for h in headers:
        n = _norm_header(h)
        target = "agent_field"
        if n in id_aliases:
            target = "product_identifier"
        else:
            for core, aliases in ALIASES.items():
                if n in aliases and core not in used:
                    target = core
                    break
        if target in CORE_TARGETS:
            if target in used:  # two columns claim one core field — keep data instead
                target = "agent_field"
            else:
                used.add(target)
        out[h] = target
    return out


def _read_csv(raw: bytes) -> tuple[list[str], list[dict]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "CSV has no header row")
    headers = [h for h in reader.fieldnames if h is not None]
    rows = [{h: (row.get(h) or "").strip() for h in headers} for row in reader]
    return headers, rows


def _get_scoped(db: Session, user: User, campaign_id: str) -> Campaign:
    c = (
        db.query(Campaign)
        .filter(Campaign.campaign_id == campaign_id, Campaign.tenant_id == user.tenant_id)
        .first()
    )
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return c


def _agent_scoped(db: Session, user: User, agent_id: str) -> AgentConfig:
    a = (
        db.query(AgentConfig)
        .filter(AgentConfig.agent_id == agent_id, AgentConfig.tenant_id == user.tenant_id)
        .first()
    )
    if a is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "agent_id does not name an agent of this tenant")
    return a


def _out(db: Session, c: Campaign) -> CampaignOut:
    agent = db.get(AgentConfig, c.agent_id)
    meta = PRODUCTS.get(c.product, PRODUCTS["other"])
    # A4: flag campaigns whose mode has no working provider — creating and
    # running them is allowed, but their calls run in stub mode.
    ready, missing = prov.mode_readiness(db, c.tenant_id, c.provider_mode)
    flag = None
    if not ready:
        mode_label = "managed" if c.provider_mode == "managed" else "self-hosted"
        flag = (f"No working provider configured for {mode_label} mode "
                f"(missing: {', '.join(missing)}) — calls will run in stub mode. "
                "Configure it under Settings → Providers.")
    return CampaignOut(
        campaign_id=c.campaign_id, tenant_id=c.tenant_id,
        agent_id=c.agent_id, agent_name=agent.name if agent else "",
        name=c.name, product=c.product, product_label=meta["label"],
        product_detail=c.product_detail, purpose=c.purpose, audience=c.audience,
        channel=c.channel, provider_mode=c.provider_mode, status=c.status,
        identifier_type=meta["identifier_type"], identifier_label=meta["identifier_label"],
        schedule=c.schedule or {}, retry_rules=c.retry_rules or {},
        routing_rules=c.routing_rules or [],
        lead_count=db.query(Lead).filter(Lead.campaign_id == c.campaign_id).count(),
        provider_configured=ready, provider_flag=flag,
        created_at=c.created_at.isoformat() if c.created_at else None,
    )


def _apply(c: Campaign, body: CampaignIn) -> None:
    c.agent_id = body.agent_id
    c.name = body.name
    c.product = body.product
    c.product_detail = body.product_detail
    c.purpose = body.purpose
    c.audience = body.audience
    c.channel = body.channel
    c.provider_mode = body.provider_mode
    c.schedule = body.schedule.model_dump()
    c.retry_rules = body.retry_rules.model_dump()
    c.routing_rules = [r.model_dump() for r in body.routing_rules]


def _validate(body: CampaignIn) -> None:
    if body.product not in PRODUCTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"product must be one of {sorted(PRODUCTS)}")
    if body.provider_mode not in ("managed", "self_hosted"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "provider_mode must be 'managed' or 'self_hosted'")


@router.get("", response_model=list[CampaignOut])
def list_campaigns(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(Campaign)
        .filter(Campaign.tenant_id == user.tenant_id)
        .order_by(Campaign.created_at)
        .all()
    )
    return [_out(db, c) for c in rows]


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
def create_campaign(body: CampaignIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _validate(body)
    _agent_scoped(db, user, body.agent_id)  # bound agent must be this tenant's
    c = Campaign(tenant_id=user.tenant_id, status="draft")
    _apply(c, body)
    db.add(c)
    db.commit()
    db.refresh(c)
    return _out(db, c)


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _out(db, _get_scoped(db, user, campaign_id))


@router.put("/{campaign_id}", response_model=CampaignOut)
def update_campaign(campaign_id: str, body: CampaignIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = _get_scoped(db, user, campaign_id)
    _validate(body)
    _agent_scoped(db, user, body.agent_id)
    _apply(c, body)
    db.commit()
    db.refresh(c)
    return _out(db, c)


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign(campaign_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = _get_scoped(db, user, campaign_id)
    db.query(Lead).filter(Lead.campaign_id == c.campaign_id).delete()
    db.delete(c)
    db.commit()
    return None


@router.post("/{campaign_id}/status", response_model=CampaignOut)
def set_status(campaign_id: str, body: CampaignStatusIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = _get_scoped(db, user, campaign_id)
    allowed = TRANSITIONS.get(c.status, set())
    if body.status not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot move a {c.status} campaign to {body.status}; allowed: {sorted(allowed) or 'none'}",
        )
    c.status = body.status
    db.commit()
    db.refresh(c)
    return _out(db, c)


# ── Leads ─────────────────────────────────────────────────────────────────

def _lead_out(l: Lead) -> LeadOut:
    return LeadOut(
        lead_id=l.lead_id, campaign_id=l.campaign_id, phone_number=l.phone_number,
        name=l.name, bank_name=l.bank_name, state=l.state,
        agent_fields=l.agent_fields or {}, attempt_count=l.attempt_count,
        status=l.status, created_at=l.created_at.isoformat() if l.created_at else None,
    )


@router.get("/{campaign_id}/leads", response_model=LeadListOut)
def list_leads(campaign_id: str, limit: int = 500, offset: int = 0,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = _get_scoped(db, user, campaign_id)  # 404 unless this tenant's campaign
    q = db.query(Lead).filter(Lead.campaign_id == c.campaign_id)
    total = q.count()
    rows = q.order_by(Lead.created_at, Lead.lead_id).offset(offset).limit(limit).all()
    return LeadListOut(total=total, leads=[_lead_out(l) for l in rows])


@router.post("/{campaign_id}/leads/preview")
async def preview_leads(campaign_id: str, file: UploadFile = File(...),
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Parse an uploaded CSV without storing anything: headers, row count,
    sample rows and a SUGGESTED column mapping for the operator to confirm."""
    c = _get_scoped(db, user, campaign_id)
    headers, rows = _read_csv(await file.read())
    identifier_type = PRODUCTS.get(c.product, PRODUCTS["other"])["identifier_type"]
    return {
        "filename": file.filename,
        "headers": headers,
        "total_rows": len(rows),
        "sample_rows": rows[:5],
        "suggested_mapping": _suggest_mapping(headers, identifier_type),
        "identifier_type": identifier_type,
    }


@router.post("/{campaign_id}/leads/upload", response_model=UploadResultOut)
async def upload_leads(campaign_id: str, file: UploadFile = File(...),
                       mapping: str = Form(...),
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Store leads from a CSV using the confirmed column mapping.

    mapping: JSON object {csv_column: target}; target ∈ name | phone_number |
    bank_name | state | product_identifier | agent_field | ignore.
    Rows with no phone number are invalid; a phone already in this campaign
    (or repeated in the file) is a duplicate and is skipped — the same
    behaviour the wireframes show ("858 valid · 2 duplicates skipped").
    """
    c = _get_scoped(db, user, campaign_id)
    try:
        mapping = json.loads(mapping)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mapping must be a JSON object")
    if not isinstance(mapping, dict) or "phone_number" not in mapping.values():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "mapping must map one column to phone_number")
    bad = {t for t in mapping.values() if t not in ALL_TARGETS}
    if bad:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown mapping targets: {sorted(bad)}")

    headers, rows = _read_csv(await file.read())
    identifier_type = PRODUCTS.get(c.product, PRODUCTS["other"])["identifier_type"]
    seen = {p for (p,) in db.query(Lead.phone_number).filter(Lead.campaign_id == c.campaign_id)}
    created = duplicates = invalid = 0
    for row in rows:
        fields = {"name": "", "phone_number": "", "bank_name": "", "state": ""}
        agent_fields = {}
        for col, target in mapping.items():
            if target == "ignore" or col not in row:
                continue
            value = row[col]
            if target in fields:
                fields[target] = value
            elif target == "product_identifier":
                if value:
                    agent_fields["product_identifier"] = value
                    agent_fields["product_identifier_type"] = identifier_type
            else:  # agent_field — keep under the normalised column name
                if value:
                    agent_fields[_norm_header(col)] = value
        phone = re.sub(r"\s+", "", fields["phone_number"])
        if not phone:
            invalid += 1
            continue
        if phone in seen:
            duplicates += 1
            continue
        seen.add(phone)
        db.add(Lead(
            tenant_id=user.tenant_id, campaign_id=c.campaign_id,
            phone_number=phone, name=fields["name"], bank_name=fields["bank_name"],
            state=fields["state"], agent_fields=agent_fields,
        ))
        created += 1
    db.commit()
    return UploadResultOut(total_rows=len(rows), created=created,
                           duplicates_skipped=duplicates, invalid_skipped=invalid)
