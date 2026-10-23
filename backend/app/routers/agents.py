"""Agent Config Studio API (A2).

An agent is configured entirely here — no code or DB edits (A2 acceptance).
SINGLE PROMPT model (owner decision, 2026-10-23): one `prompt` text carries
behaviour, dialogue/flow and the full disposition logic, structured as
ROLE / FLOW / DISPOSITIONS / CLOSINGS. There are no script-template or
disposition-rule endpoints by design.

PROMPT VERSIONING: every save that changes the prompt appends a new row to
prompt_versions (append-only). Rollback appends a new version equal to the
chosen old text — history is reviewable and edits are reversible.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import AgentConfig, PromptVersion, User
from ..schemas import AgentIn, AgentOut, PromptVersionOut

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _latest_version(db: Session, agent_id: str) -> int:
    row = (
        db.query(PromptVersion)
        .filter(PromptVersion.agent_id == agent_id)
        .order_by(PromptVersion.version.desc())
        .first()
    )
    return row.version if row else 0


def _snapshot(db: Session, user: User, agent: AgentConfig, note: str = "") -> int:
    nxt = _latest_version(db, agent.agent_id) + 1
    db.add(PromptVersion(
        tenant_id=user.tenant_id, agent_id=agent.agent_id, version=nxt,
        prompt=agent.prompt, note=note, created_by=user.email,
    ))
    return nxt


def _out(db: Session, a: AgentConfig) -> AgentOut:
    return AgentOut(
        agent_id=a.agent_id, tenant_id=a.tenant_id, name=a.name, stage=a.stage,
        purpose=a.purpose, is_reference=a.is_reference, prompt=a.prompt,
        llm_model=a.llm_model, stt_provider=a.stt_provider, stt_language=a.stt_language,
        tts_provider=a.tts_provider, tts_voice_id=a.tts_voice_id,
        voice_gender=a.voice_gender or "female",
        language_rule=a.language_rule or {}, captured_fields=a.captured_fields or [],
        max_duration_s=a.max_duration_s, max_turns=a.max_turns,
        silence_prompt_after_s=a.silence_prompt_after_s,
        prompt_version=_latest_version(db, a.agent_id),
    )


def _apply(a: AgentConfig, body: AgentIn) -> None:
    a.name = body.name
    a.stage = body.stage
    a.purpose = body.purpose
    a.is_reference = body.is_reference
    a.prompt = body.prompt
    a.llm_model = body.llm_model
    a.stt_provider = body.stt_provider
    a.stt_language = body.stt_language
    a.tts_provider = body.tts_provider
    a.tts_voice_id = body.tts_voice_id
    a.voice_gender = body.voice_gender if body.voice_gender in ("female", "male") else "female"
    a.language_rule = body.language_rule
    a.captured_fields = [f.model_dump() for f in body.captured_fields]
    a.max_duration_s = body.max_duration_s
    a.max_turns = body.max_turns
    a.silence_prompt_after_s = body.silence_prompt_after_s


def _get_scoped(db: Session, user: User, agent_id: str) -> AgentConfig:
    a = (
        db.query(AgentConfig)
        .filter(AgentConfig.agent_id == agent_id, AgentConfig.tenant_id == user.tenant_id)
        .first()
    )
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return a


@router.get("", response_model=list[AgentOut])
def list_agents(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(AgentConfig)
        .filter(AgentConfig.tenant_id == user.tenant_id)
        .order_by(AgentConfig.created_at)
        .all()
    )
    return [_out(db, a) for a in rows]


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(body: AgentIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    a = AgentConfig(tenant_id=user.tenant_id)
    _apply(a, body)
    db.add(a)
    db.flush()
    _snapshot(db, user, a, note="Initial version")
    db.commit()
    db.refresh(a)
    return _out(db, a)


@router.get("/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _out(db, _get_scoped(db, user, agent_id))


@router.put("/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, body: AgentIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    a = _get_scoped(db, user, agent_id)
    prompt_changed = a.prompt != body.prompt
    _apply(a, body)
    db.flush()
    if prompt_changed:
        _snapshot(db, user, a, note="Edit")
    db.commit()
    db.refresh(a)
    return _out(db, a)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    a = _get_scoped(db, user, agent_id)
    db.query(PromptVersion).filter(PromptVersion.agent_id == a.agent_id).delete()
    db.delete(a)
    db.commit()
    return None


# ── Prompt version history ────────────────────────────────────────────────

def _version_out(v: PromptVersion) -> PromptVersionOut:
    return PromptVersionOut(
        version=v.version, prompt=v.prompt, note=v.note,
        created_by=v.created_by,
        created_at=v.created_at.isoformat() if v.created_at else None,
    )


@router.get("/{agent_id}/prompt-versions", response_model=list[PromptVersionOut])
def list_prompt_versions(agent_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_scoped(db, user, agent_id)  # 404 if not ours
    rows = (
        db.query(PromptVersion)
        .filter(PromptVersion.agent_id == agent_id, PromptVersion.tenant_id == user.tenant_id)
        .order_by(PromptVersion.version.desc())
        .all()
    )
    return [_version_out(v) for v in rows]


@router.get("/{agent_id}/prompt-versions/{version}", response_model=PromptVersionOut)
def get_prompt_version(agent_id: str, version: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_scoped(db, user, agent_id)
    v = (
        db.query(PromptVersion)
        .filter(PromptVersion.agent_id == agent_id, PromptVersion.version == version,
                PromptVersion.tenant_id == user.tenant_id)
        .first()
    )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt version not found")
    return _version_out(v)


@router.post("/{agent_id}/prompt-versions/{version}/rollback", response_model=AgentOut)
def rollback_prompt(agent_id: str, version: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Make an older prompt version live again — as a NEW version (append-only)."""
    a = _get_scoped(db, user, agent_id)
    v = (
        db.query(PromptVersion)
        .filter(PromptVersion.agent_id == agent_id, PromptVersion.version == version,
                PromptVersion.tenant_id == user.tenant_id)
        .first()
    )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt version not found")
    a.prompt = v.prompt
    db.flush()
    _snapshot(db, user, a, note=f"Rollback to v{version}")
    db.commit()
    db.refresh(a)
    return _out(db, a)
