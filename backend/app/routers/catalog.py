"""Admin catalog + adoption API (Epic C, C2 — design §11).

Authorship is shared, adoption is per tenant:

- The platform side AUTHORS catalog agents and PUBLISHES immutable
  versions (a published version NEVER changes — there is deliberately
  NO update or delete endpoint for versions; a fix becomes a new
  version row). Version metadata (suitable_for / model / validated_on)
  is the mismatch guard: a tenant must never adopt a version unproven
  on its own stack.
- A tenant ADOPTS a version from a dropdown (agent + version).
  Adoption COPIES the prompt text + structured config into the
  tenant's own database (its AgentConfig + a PromptVersion row) and
  records the adoption (TenantAdoption = current pointer, AdoptionLog
  = append-only history, the only cross-tenant view, no customer data).
  After adoption the tenant runtime reads only its own database.
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
    db.add(ca)
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
    versions: a published version never changes, a fix is a new row."""
    ca = _resolve_agent(db, slug_or_id)
    if body.suitable_for not in SUITABLE_FOR:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "suitable_for must be 'managed', 'self_hosted' or 'both'")
    nxt = (
        db.query(CatalogVersion)
        .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id)
        .order_by(CatalogVersion.version.desc())
        .first()
    )
    version = (nxt.version + 1) if nxt else 1
    v = CatalogVersion(
        catalog_agent_id=ca.catalog_agent_id, version=version,
        prompt=body.prompt, note=body.note, suitable_for=body.suitable_for,
        model=body.model, validated_on=body.validated_on, published_by=user.email,
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
    (published versions stay immutable) and it does NOT publish: the row
    carries status 'draft' until the publish endpoint flips it. Version
    numbering stays sequential, assigned at save; publishing does not
    renumber. The existing immediate-publish endpoint is unchanged."""
    ca = _resolve_agent(db, slug_or_id)
    if body.suitable_for not in SUITABLE_FOR:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "suitable_for must be 'managed', 'self_hosted' or 'both'")
    nxt = (
        db.query(CatalogVersion)
        .filter(CatalogVersion.catalog_agent_id == ca.catalog_agent_id)
        .order_by(CatalogVersion.version.desc())
        .first()
    )
    version = (nxt.version + 1) if nxt else 1
    v = CatalogVersion(
        catalog_agent_id=ca.catalog_agent_id, version=version,
        prompt=body.prompt, note=body.note, suitable_for=body.suitable_for,
        model=body.model, validated_on=body.validated_on,
        status="draft", published_by="",
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
    return {
        "catalog_agent_id": ca.slug, "name": ca.name, "stage": ca.stage,
        "purpose": ca.purpose, "version": v.version, "prompt": v.prompt,
        "note": v.note or "", "suitable_for": v.suitable_for,
        "model": v.model or "", "validated_on": v.validated_on or "",
        "llm_model": ca.llm_model, "stt_provider": ca.stt_provider,
        "stt_language": ca.stt_language, "tts_provider": ca.tts_provider,
        "tts_voice_id": ca.tts_voice_id or "", "voice_gender": ca.voice_gender or "female",
        "language_rule": ca.language_rule or {},
        "captured_fields": ca.captured_fields or [],
        "max_duration_s": ca.max_duration_s, "max_turns": ca.max_turns,
        "silence_prompt_after_s": ca.silence_prompt_after_s,
        "silence_max_prompts": ca.silence_max_prompts,
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
        cfg = {
            "name": ca.name, "stage": ca.stage, "purpose": ca.purpose,
            "llm_model": ca.llm_model, "stt_provider": ca.stt_provider,
            "stt_language": ca.stt_language, "tts_provider": ca.tts_provider,
            "tts_voice_id": ca.tts_voice_id or "", "voice_gender": ca.voice_gender or "female",
            "language_rule": ca.language_rule or {},
            "captured_fields": ca.captured_fields or [],
            "max_duration_s": ca.max_duration_s, "max_turns": ca.max_turns,
            "silence_prompt_after_s": ca.silence_prompt_after_s,
            "silence_max_prompts": ca.silence_max_prompts,
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
