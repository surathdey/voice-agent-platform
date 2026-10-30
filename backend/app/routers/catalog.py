"""Admin catalog + adoption API (Epic C, C2 — design §11).

Authorship is shared, adoption is per tenant:

- The platform side AUTHORS catalog agents and PUBLISHES immutable
  versions (a published version NEVER changes — there is deliberately
  NO update or delete endpoint for versions; a fix becomes a new
  version row). Version metadata (suitable_for / model / validated_on)
  is the mismatch guard: a tenant must never adopt a version unproven
  on its own stack.
- A tenant ADOPTS a version from a dropdown (agent + version).
  Adoption COPIES the version's FULL content — prompt + stack (model
  / STT / TTS / voice) + language_rule + captured_fields +
  duration/turn/silence settings — into the tenant's own database
  (its AgentConfig + a PromptVersion row) and records the adoption
  (TenantAdoption = current pointer, AdoptionLog = append-only
  history, the only cross-tenant view, no customer data). The agent
  row is only the job identity (slug / name / stage / purpose);
  adoption NEVER reads the agent row's config, so an adopted agent
  reproduces exactly what the Super Admin edited on that version and
  no stale agent default ever leaks in. After adoption the tenant
  runtime reads only its own database.
- A new catalog version NEVER auto-applies: tenants see a "new
  version available" flag and adopt when ready. Running campaigns keep
  their pinned version for life (see Campaign.pinned_prompt); new
  campaigns pin the newly adopted version.

Same-DB vs cross-box: in the current single-box deployment the catalog
tables and the tenant tables live in one database, so adoption resolves
the version locally. When the platform box and tenant boxes are separate
databases, the tenant box imports via the export endpoint: GET a version's
full payload from the platform box and POST it as AdoptionIn.payload —
no code path ever reads across databases.

Roles (C5): catalog WRITES use require_catalog_admin (deps), which is
Super Admin only — the prompt editor lives on the platform side and
nowhere else. Catalog READS are Super Admin + Tenant Admin (the
adoption side). Adoption itself is Tenant Admin only: adoption
records the adopting tenant, and a Super Admin belongs to the
platform scope, not to any tenant — at setup the Super Admin
creates the Tenant Admin account, and the Tenant Admin adopts.
The adoption log is the platform view: Super Admin sees all
tenants, Tenant Admin sees only their own tenant.
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..box import box_tenant_slug
from ..config import settings
from ..db import get_db
from ..deps import (
    ROLE_SUPER_ADMIN,
    get_current_user,
    require_catalog_admin,
    require_catalog_reader,
    require_tenant_admin,
    require_tenant_admin_or_super,
)
from ..models import (
    AdoptionLog,
    AgentConfig,
    CatalogAgent,
    CatalogVersion,
    PromptVersion,
    TenantAdoption,
    User,
    new_id,
)
from ..schemas import (
    AdoptionIn,
    AdoptionLogOut,
    AdoptionOut,
    CatalogAgentIn,
    CatalogAgentOut,
    CatalogVersionIn,
    CatalogVersionOut,
)

router = APIRouter(prefix="/api/catalog", tags=["catalog"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
SUITABLE_FOR = ("managed", "self_hosted", "both")


def _tenant_stack() -> str:
    """This box's stack for the mismatch flag: managed | self_hosted | stub."""
    mode = (getattr(settings, "TELEPHONY_MODE", "") or "").strip().lower()
    return mode if mode in ("managed", "self_hosted") else "stub"


def _fit(version: CatalogVersion, stack: str) -> tuple[bool, str]:
    """(unproven_on_stack, fit_label) for one version on this stack."""
    if stack == "stub":
        return False, ""
    if version.suitable_for not in ("both", stack):
        return True, f"not validated on {stack} (for {version.suitable_for})"
    token = "managed" if stack == "managed" else "self_hosted"
    validated = (version.validated_on or "").lower().replace("-", "_")
    if token in validated:
        return False, f"best for {stack}"
    return False, f"intended for {stack}"


# Settled stacks (do not invent others):
#   self_hosted = Sarvam: LLM sarvam-*, STT saaras, TTS bulbul.
#   managed     = Vapi:   LLM anything except sarvam-*, STT deepgram,
#                          TTS elevenlabs.
# A version for "both" carries ONE consistent set (Sarvam set OR Vapi
# set) — the set the prompt was proven with; the adopting tenant runs
# that set. A MIXED set (e.g. gpt-4o + saaras + bulbul) is never a
# valid version on any stack.
def _validate_stack(suitable_for: str, model: str, stt_provider: str, tts_provider: str) -> None:
    """422 unless the final stored stack is internally consistent."""
    m = (model or "").strip().lower()
    stt = (stt_provider or "").strip().lower()
    tts = (tts_provider or "").strip().lower()
    if suitable_for == "self_hosted":
        ok = m.startswith("sarvam") and stt == "saaras" and tts == "bulbul"
    elif suitable_for == "managed":
        ok = (not m.startswith("sarvam")) and stt == "deepgram" and tts == "elevenlabs"
    else:  # "both": one consistent set, Sarvam set or Vapi set.
        ok = ((m.startswith("sarvam") and stt == "saaras" and tts == "bulbul")
              or ((not m.startswith("sarvam")) and stt == "deepgram" and tts == "elevenlabs"))
    if not ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"stack mismatch: suitable_for={suitable_for} with model={model!r}, "
            f"stt={stt_provider!r}, tts={tts_provider!r} is not a consistent stack")


def _resolve_version_fields(ca: CatalogAgent, body: CatalogVersionIn) -> dict:
    """Final stored fields for a NEW version row.

    Explicit values win. Omitted fields default: stack providers and
    model default BY suitable_for — never from the agent row, whose
    Sarvam defaults would otherwise poison a managed version (this is
    exactly how the seed's managed v1 used to adopt a Sarvam stack);
    non-stack config defaults from the agent row. validated_on is
    HISTORY: it passes through untouched, never derived from
    suitable_for, so a version flipped to another stack keeps its
    validation history and reads "intended for X" until a live call
    on the new stack proves it. The result is stack-validated (422
    on mismatch) before any row is written.
    """
    suitable = body.suitable_for
    if suitable not in SUITABLE_FOR:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "suitable_for must be 'managed', 'self_hosted' or 'both'")

    def _str(v):
        return v if isinstance(v, str) and v.strip() else None

    model = _str(body.model)
    if model is None:
        model = {"managed": "gpt-4o", "self_hosted": "sarvam-105b"}.get(
            suitable, ca.llm_model or "sarvam-105b")
    stt = _str(body.stt_provider)
    if stt is None:
        stt = {"self_hosted": "saaras", "managed": "deepgram"}.get(
            suitable, ca.stt_provider or "saaras")
    tts = _str(body.tts_provider)
    if tts is None:
        tts = {"self_hosted": "bulbul", "managed": "elevenlabs"}.get(
            suitable, ca.tts_provider or "bulbul")
    voice = _str(body.tts_voice_id)
    if voice is None:
        if suitable == "managed":
            voice = ""  # voice comes from the tenant's Vapi profile
        elif suitable == "self_hosted":
            voice = ca.tts_voice_id if (ca.tts_provider or "").lower() == "bulbul" else ""
        else:
            voice = ca.tts_voice_id or ""
    gender = _str(body.voice_gender)
    if gender not in ("female", "male"):
        gender = ca.voice_gender if ca.voice_gender in ("female", "male") else "female"
    fields = {
        "prompt": body.prompt, "note": body.note, "suitable_for": suitable,
        "model": model, "validated_on": body.validated_on,
        "stt_provider": stt,
        "stt_language": _str(body.stt_language) or ca.stt_language or "hi-IN",
        "tts_provider": tts, "tts_voice_id": voice, "voice_gender": gender,
        "language_rule": body.language_rule if body.language_rule is not None else (ca.language_rule or {}),
        "captured_fields": ([f.model_dump() for f in body.captured_fields]
                            if body.captured_fields is not None else (ca.captured_fields or [])),
        "max_duration_s": body.max_duration_s if body.max_duration_s is not None else ca.max_duration_s,
        "max_turns": body.max_turns if body.max_turns is not None else ca.max_turns,
        "silence_prompt_after_s": (body.silence_prompt_after_s
                                  if body.silence_prompt_after_s is not None else ca.silence_prompt_after_s),
        "silence_max_prompts": (body.silence_max_prompts
                               if body.silence_max_prompts is not None else ca.silence_max_prompts),
    }
    _validate_stack(suitable, model, stt, tts)
    return fields


def _version_out(v: CatalogVersion, stack: str) -> CatalogVersionOut:
    unproven, label = _fit(v, stack)
    # status is authoritative; a draft's published_at is only the
    # creation timestamp left by the column default, so it is masked.
    status = v.status or "published"
    return CatalogVersionOut(
        version_id=v.version_id, catalog_agent_id=v.catalog_agent_id,
        version=v.version, prompt=v.prompt, note=v.note or "",
        suitable_for=v.suitable_for, model=v.model or "",
        validated_on=v.validated_on or "", published_by=v.published_by or "",
        published_at=None if status == "draft" else (v.published_at.isoformat() if v.published_at else None),
        status=status,
        unproven_on_stack=unproven, fit_label=label,
        stt_provider=v.stt_provider or "", stt_language=v.stt_language or "hi-IN",
        tts_provider=v.tts_provider or "", tts_voice_id=v.tts_voice_id or "",
        voice_gender=v.voice_gender or "female",
        language_rule=v.language_rule or {},
        captured_fields=v.captured_fields or [],
        max_duration_s=v.max_duration_s, max_turns=v.max_turns,
        silence_prompt_after_s=v.silence_prompt_after_s,
        silence_max_prompts=v.silence_max_prompts,
    )


def _resolve_agent(db: Session, slug_or_id: str) -> CatalogAgent:
    ca = (
        db.query(CatalogAgent)
        .filter((CatalogAgent.slug == slug_or_id) | (CatalogAgent.catalog_agent_id == slug_or_id))
        .first()
    )
    if ca is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Catalog agent not found")
    return ca


def _agent_out(db: Session, user: User, ca: CatalogAgent) -> CatalogAgentOut:
    stack = _tenant_stack()
    all_versions = (
        db.query(CatalogVersion)
        .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id)
        .order_by(CatalogVersion.version)
        .all()
    )
    # Tenant-facing reads see ONLY published versions: a tenant must never
    # see or adopt a draft. Super Admin sees all versions with status.
    if user.role == ROLE_SUPER_ADMIN:
        versions = all_versions
    else:
        versions = [v for v in all_versions if v.status == "published"]
    adopted = (
        db.query(TenantAdoption)
        .filter(TenantAdoption.tenant_id == user.tenant_id,
                TenantAdoption.catalog_agent_id == ca.slug)
        .first()
    )
    latest = versions[-1].version if versions else None
    adopted_v = adopted.catalog_version if adopted else None
    return CatalogAgentOut(
        slug=ca.slug, name=ca.name, stage=ca.stage, purpose=ca.purpose,
        llm_model=ca.llm_model, stt_provider=ca.stt_provider,
        stt_language=ca.stt_language, tts_provider=ca.tts_provider,
        tts_voice_id=ca.tts_voice_id or "", voice_gender=ca.voice_gender or "female",
        language_rule=ca.language_rule or {},
        captured_fields=ca.captured_fields or [],
        max_duration_s=ca.max_duration_s, max_turns=ca.max_turns,
        silence_prompt_after_s=ca.silence_prompt_after_s,
        silence_max_prompts=ca.silence_max_prompts,
        catalog_agent_id=ca.catalog_agent_id,
        versions=[_version_out(v, stack) for v in versions],
        adopted_version=adopted_v, latest_version=latest,
        new_version_available=bool(
            adopted_v is not None and latest is not None and latest > adopted_v),
        unproven_on_stack=bool(
            stack in ("managed", "self_hosted") and versions
            and not any(v.suitable_for in ("both", stack) for v in versions)),
        tenant_stack=stack,
    )


def _latest_version(db: Session, agent_id: str) -> int:
    row = (
        db.query(PromptVersion)
        .filter(PromptVersion.agent_id == agent_id)
        .order_by(PromptVersion.version.desc())
        .first()
    )
    return row.version if row else 0


@router.get("/agents", response_model=list[CatalogAgentOut])
def list_catalog_agents(user: User = Depends(require_catalog_reader), db: Session = Depends(get_db)):
    rows = db.query(CatalogAgent).order_by(CatalogAgent.created_at).all()
    return [_agent_out(db, user, ca) for ca in rows]


@router.post("/agents", response_model=CatalogAgentOut, status_code=status.HTTP_201_CREATED)
def create_catalog_agent(body: CatalogAgentIn, user: User = Depends(require_catalog_admin),
                         db: Session = Depends(get_db)):
    """Create a catalog agent. When body.initial_version carries a
    non-blank prompt (the create-agent form), v1 is written in the
    SAME transaction as a DRAFT, so an agent created from the form is
    never version-less. When initial_version is absent (the seed
    flow: agent first, then a separate immediate-publish call), the
    agent is created alone. A stack-mismatched initial version is a
    422 and nothing is written."""
    slug = (body.slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "slug must be lowercase letters/digits/hyphens")
    if db.query(CatalogAgent).filter(CatalogAgent.slug == slug).first():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"catalog agent slug '{slug}' already exists")
    ca = CatalogAgent(catalog_agent_id=new_id("cat"))
    ca.slug = slug
    ca.name = body.name
    ca.stage = body.stage
    ca.purpose = body.purpose
    ca.llm_model = body.llm_model
    ca.stt_provider = body.stt_provider
    ca.stt_language = body.stt_language
    ca.tts_provider = body.tts_provider
    ca.tts_voice_id = body.tts_voice_id
    ca.voice_gender = body.voice_gender if body.voice_gender in ("female", "male") else "female"
    ca.language_rule = body.language_rule
    ca.captured_fields = [f.model_dump() for f in body.captured_fields]
    ca.max_duration_s = body.max_duration_s
    ca.max_turns = body.max_turns
    ca.silence_prompt_after_s = body.silence_prompt_after_s
    ca.silence_max_prompts = body.silence_max_prompts
    # Resolve BEFORE any write so a 422 leaves nothing behind.
    iv = body.initial_version
    v1_fields = _resolve_version_fields(ca, iv) if (iv is not None and (iv.prompt or "").strip()) else None
    db.add(ca)
    if v1_fields is not None:
        db.add(CatalogVersion(
            catalog_agent_id=ca.catalog_agent_id, version=1,
            status="draft", published_by="", **v1_fields))
    db.commit()
    db.refresh(ca)
    return _agent_out(db, user, ca)


@router.get("/agents/{slug_or_id}", response_model=CatalogAgentOut)
def get_catalog_agent(slug_or_id: str, user: User = Depends(require_catalog_reader),
                      db: Session = Depends(get_db)):
    return _agent_out(db, user, _resolve_agent(db, slug_or_id))


@router.post("/agents/{slug_or_id}/versions", response_model=CatalogVersionOut,
             status_code=status.HTTP_201_CREATED)
def publish_version(slug_or_id: str, body: CatalogVersionIn,
                    user: User = Depends(require_catalog_admin), db: Session = Depends(get_db)):
    """Publish a NEW immutable version. There is no update/delete for
    versions: a published version never changes, a fix is a new row.
    The version stores the FULL content (prompt + stack + config);
    omitted fields resolve from the agent row / by suitable_for and
    the final stack is validated (422 on mismatch) before the row is
    written. The seed flow (prompt/note/suitable_for/model/
    validated_on only) keeps working: a managed seed version resolves
    to the Vapi stack, not the agent row's Sarvam defaults."""
    ca = _resolve_agent(db, slug_or_id)
    fields = _resolve_version_fields(ca, body)
    nxt = (
        db.query(CatalogVersion)
        .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id)
        .order_by(CatalogVersion.version.desc())
        .first()
    )
    version = (nxt.version + 1) if nxt else 1
    v = CatalogVersion(
        catalog_agent_id=ca.catalog_agent_id, version=version,
        published_by=user.email, **fields,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return _version_out(v, _tenant_stack())


@router.post("/agents/{slug_or_id}/versions/draft", response_model=CatalogVersionOut,
             status_code=status.HTTP_201_CREATED)
def save_draft_version(slug_or_id: str, body: CatalogVersionIn,
                       user: User = Depends(require_catalog_admin), db: Session = Depends(get_db)):
    """Save a NEW version as DRAFT. It NEVER modifies any existing version
    (published versions stay immutable, and a draft being viewed stays
    as-is — editing it also creates a new row) and it does NOT publish:
    the row carries status 'draft' until the publish endpoint flips it.
    Version numbering stays sequential, assigned at save; publishing
    does not renumber. The draft carries the FULL edited content
    (prompt + stack + config), so adopting it later reproduces exactly
    what was edited; the final stack is validated (422 on mismatch).
    The existing immediate-publish endpoint is unchanged."""
    ca = _resolve_agent(db, slug_or_id)
    fields = _resolve_version_fields(ca, body)
    nxt = (
        db.query(CatalogVersion)
        .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id)
        .order_by(CatalogVersion.version.desc())
        .first()
    )
    version = (nxt.version + 1) if nxt else 1
    v = CatalogVersion(
        catalog_agent_id=ca.catalog_agent_id, version=version,
        status="draft", published_by="", **fields,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return _version_out(v, _tenant_stack())


@router.post("/agents/{slug_or_id}/versions/{version}/publish", response_model=CatalogVersionOut)
def publish_draft_version(slug_or_id: str, version: int,
                          user: User = Depends(require_catalog_admin), db: Session = Depends(get_db)):
    """Publish a draft version: sets status 'published' + published_at +
    published_by. Only drafts can be published; an already-published
    version is rejected (it never changes). Publishing does not
    renumber."""
    ca = _resolve_agent(db, slug_or_id)
    v = (
        db.query(CatalogVersion)
        .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id,
                CatalogVersion.version == version)
        .first()
    )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Catalog version not found")
    if v.status == "published":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Version is already published")
    v.status = "published"
    v.published_at = datetime.utcnow()
    v.published_by = user.email
    db.commit()
    db.refresh(v)
    return _version_out(v, _tenant_stack())


@router.get("/agents/{slug_or_id}/versions/{version}/export")
def export_version(slug_or_id: str, version: int, user: User = Depends(require_catalog_reader),
                   db: Session = Depends(get_db)):
    """Full version payload for cross-box adoption import (POST it as
    AdoptionIn.payload on the tenant box). No customer data — prompt
    text + structured config + metadata only."""
    ca = _resolve_agent(db, slug_or_id)
    v = (
        db.query(CatalogVersion)
        .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id,
                CatalogVersion.version == version)
        .first()
    )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Catalog version not found")
    if v.status != "published" and user.role != ROLE_SUPER_ADMIN:
        # A tenant must never see a draft: hide it as not found.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Catalog version not found")
    # The version is authoritative for ALL config: the agent row is
    # only identity + creation defaults, and reading it here would
    # reintroduce the stale-stack bug (a managed version adopting
    # the agent row's Sarvam defaults).
    return {
        "catalog_agent_id": ca.slug, "name": ca.name, "stage": ca.stage,
        "purpose": ca.purpose, "version": v.version, "prompt": v.prompt,
        "note": v.note or "", "suitable_for": v.suitable_for,
        "model": v.model or "", "validated_on": v.validated_on or "",
        "llm_model": v.model or "", "stt_provider": v.stt_provider or "",
        "stt_language": v.stt_language or "hi-IN", "tts_provider": v.tts_provider or "",
        "tts_voice_id": v.tts_voice_id or "", "voice_gender": v.voice_gender or "female",
        "language_rule": v.language_rule or {},
        "captured_fields": v.captured_fields or [],
        "max_duration_s": v.max_duration_s, "max_turns": v.max_turns,
        "silence_prompt_after_s": v.silence_prompt_after_s,
        "silence_max_prompts": v.silence_max_prompts,
    }


@router.post("/adoptions", response_model=AdoptionOut, status_code=status.HTTP_201_CREATED)
def adopt_version(body: AdoptionIn, user: User = Depends(require_tenant_admin),
                  db: Session = Depends(get_db)):
    """Adopt a catalog version into this tenant's own database.

    Only PUBLISHED versions can be adopted: an explicit draft version is
    rejected, and latest-resolution considers published versions only.

    Same-DB mode: catalog_agent_id + version resolve against local
    catalog tables. Import mode: body.payload (from the export
    endpoint) carries the full version and wins. Either way the
    tenant's AgentConfig is updated in place (or created), a
    PromptVersion row records the adopted text, TenantAdoption points
    at it, and AdoptionLog records the adoption. Nothing is read
    across databases: the tenant runtime reads only its own rows
    after this call.
    """
    if body.payload is not None:
        p = body.payload
        slug = (p.get("catalog_agent_id") or "").strip().lower()
        prompt = p.get("prompt") or ""
        version = p.get("version")
        if not slug or not prompt or not isinstance(version, int):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "payload needs catalog_agent_id, prompt and integer version")
        cfg = {
            "name": p.get("name", slug), "stage": p.get("stage", ""),
            "purpose": p.get("purpose", ""),
            "llm_model": p.get("llm_model", "sarvam-105b"),
            "stt_provider": p.get("stt_provider", "saaras"),
            "stt_language": p.get("stt_language", "hi-IN"),
            "tts_provider": p.get("tts_provider", "bulbul"),
            "tts_voice_id": p.get("tts_voice_id", ""),
            "voice_gender": p.get("voice_gender", "female"),
            "language_rule": p.get("language_rule") or {},
            "captured_fields": p.get("captured_fields") or [],
            "max_duration_s": p.get("max_duration_s", 300),
            "max_turns": p.get("max_turns", 50),
            "silence_prompt_after_s": p.get("silence_prompt_after_s", 8),
            "silence_max_prompts": p.get("silence_max_prompts", 1),
        }
    else:
        ca = _resolve_agent(db, body.catalog_agent_id)
        slug = ca.slug
        if body.version is not None:
            v = (
                db.query(CatalogVersion)
                .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id,
                        CatalogVersion.version == body.version)
                .first()
            )
            if v is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Catalog version not found")
            if v.status != "published":
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    "Cannot adopt a draft version")
        else:
            v = (
                db.query(CatalogVersion)
                .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id,
                        CatalogVersion.status == "published")
                .order_by(CatalogVersion.version.desc())
                .first()
            )
            if v is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "Catalog agent has no published versions")
        prompt = v.prompt
        version = v.version
        # Full-content adoption: EVERYTHING except identity comes
        # from the version row, so the adopted agent reproduces
        # exactly what the Super Admin edited on that version. The
        # agent row's config is never read here (it is only creation
        # defaults); reading it would carry a stale stack whenever a
        # version's stack changed after the agent was created.
        cfg = {
            "name": ca.name, "stage": ca.stage, "purpose": ca.purpose,
            "llm_model": v.model or "", "stt_provider": v.stt_provider or "",
            "stt_language": v.stt_language or "hi-IN", "tts_provider": v.tts_provider or "",
            "tts_voice_id": v.tts_voice_id or "", "voice_gender": v.voice_gender or "female",
            "language_rule": v.language_rule or {},
            "captured_fields": v.captured_fields or [],
            "max_duration_s": v.max_duration_s, "max_turns": v.max_turns,
            "silence_prompt_after_s": v.silence_prompt_after_s,
            "silence_max_prompts": v.silence_max_prompts,
        }

    # Reuse the tenant's current adoption target; else the single
    # same-named agent; else create. The previous prompt text stays in
    # PromptVersion history, so adoption never destroys anything.
    adopted = (
        db.query(TenantAdoption)
        .filter(TenantAdoption.tenant_id == user.tenant_id,
                TenantAdoption.catalog_agent_id == slug)
        .first()
    )
    agent = None
    if adopted is not None:
        agent = (
            db.query(AgentConfig)
            .filter(AgentConfig.agent_id == adopted.agent_id,
                    AgentConfig.tenant_id == user.tenant_id)
            .first()
        )
    if agent is None:
        agent = (
            db.query(AgentConfig)
            .filter(AgentConfig.tenant_id == user.tenant_id,
                    AgentConfig.name == cfg["name"])
            .order_by(AgentConfig.created_at)
            .first()
        )
    if agent is None:
        agent = AgentConfig(tenant_id=user.tenant_id)
        db.add(agent)
    agent.name = cfg["name"]
    agent.stage = cfg["stage"]
    agent.purpose = cfg["purpose"]
    agent.prompt = prompt
    agent.llm_model = cfg["llm_model"]
    agent.stt_provider = cfg["stt_provider"]
    agent.stt_language = cfg["stt_language"]
    agent.tts_provider = cfg["tts_provider"]
    agent.tts_voice_id = cfg["tts_voice_id"]
    agent.voice_gender = cfg["voice_gender"] if cfg["voice_gender"] in ("female", "male") else "female"
    agent.language_rule = cfg["language_rule"]
    agent.captured_fields = cfg["captured_fields"]
    agent.max_duration_s = cfg["max_duration_s"]
    agent.max_turns = cfg["max_turns"]
    agent.silence_prompt_after_s = cfg["silence_prompt_after_s"]
    agent.silence_max_prompts = cfg["silence_max_prompts"]
    agent.adopted_catalog_agent = slug
    agent.adopted_catalog_version = version
    db.flush()
    db.add(PromptVersion(
        tenant_id=user.tenant_id, agent_id=agent.agent_id,
        version=_latest_version(db, agent.agent_id) + 1,
        prompt=prompt, note=f"Adopted {slug} v{version}", created_by=user.email,
    ))
    if adopted is None:
        adopted = TenantAdoption(
            tenant_id=user.tenant_id, catalog_agent_id=slug,
            catalog_version=version, agent_id=agent.agent_id, adopted_by=user.email,
        )
        db.add(adopted)
    else:
        adopted.catalog_version = version
        adopted.agent_id = agent.agent_id
        adopted.adopted_by = user.email
    db.add(AdoptionLog(
        tenant_id=user.tenant_id, tenant_slug=box_tenant_slug(),
        catalog_agent_id=slug, catalog_version=version, adopted_by=user.email,
    ))
    db.commit()
    db.refresh(agent)
    db.refresh(adopted)
    return AdoptionOut(
        adoption_id=adopted.adoption_id, tenant_id=user.tenant_id,
        catalog_agent_id=slug, catalog_version=version, agent_id=agent.agent_id,
        agent_name=agent.name, adopted_by=user.email,
        adopted_at=adopted.adopted_at.isoformat() if adopted.adopted_at else None,
    )


@router.get("/adoptions", response_model=list[AdoptionOut])
def list_adoptions(user: User = Depends(require_tenant_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(TenantAdoption)
        .filter(TenantAdoption.tenant_id == user.tenant_id)
        .order_by(TenantAdoption.adopted_at)
        .all()
    )
    out = []
    for r in rows:
        agent = db.get(AgentConfig, r.agent_id)
        out.append(AdoptionOut(
            adoption_id=r.adoption_id, tenant_id=r.tenant_id,
            catalog_agent_id=r.catalog_agent_id, catalog_version=r.catalog_version,
            agent_id=r.agent_id, agent_name=agent.name if agent else "",
            adopted_by=r.adopted_by or "",
            adopted_at=r.adopted_at.isoformat() if r.adopted_at else None,
        ))
    return out


@router.get("/adoption-log", response_model=list[AdoptionLogOut])
def list_adoption_log(user: User = Depends(require_tenant_admin_or_super),
                      db: Session = Depends(get_db)):
    """Platform view: exactly which tenant runs which version. No
    customer data — tenant slug, agent, version, who, when.
    Super Admin sees all tenants; Tenant Admin sees only their own
    tenant's rows."""
    query = db.query(AdoptionLog)
    if user.role == "tenant_admin":
        query = query.filter(AdoptionLog.tenant_id == user.tenant_id)
    rows = query.order_by(AdoptionLog.adopted_at.desc()).all()
    return [
        AdoptionLogOut(
            tenant_id=r.tenant_id, tenant_slug=r.tenant_slug or "",
            catalog_agent_id=r.catalog_agent_id, catalog_version=r.catalog_version,
            adopted_by=r.adopted_by or "",
            adopted_at=r.adopted_at.isoformat() if r.adopted_at else None,
        )
        for r in rows
    ]
