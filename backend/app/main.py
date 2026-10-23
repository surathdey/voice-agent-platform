"""Voice Agent Platform — application layer API (Phase A).

Chunk 1: A1 (tenancy + core data model) and A2 (Agent Config Studio).
Chunk 2: A3 (campaigns & leads — CRUD bound to one agent, CSV lead upload
with column mapping, schedule/retry config; dispatch engine is A5/A10).
Chunk 3: A4 (per-tenant provider settings + secret store, masked reads,
Test connection per provider; campaigns are flagged when their mode has no
working provider — they run in stub mode).
Chunk 4: A5 (orchestrator skeleton — dispatch service with schedule /
concurrency admission, deterministic attempt ids, the stub transport, and
the single idempotent finalize path every mode's §8.2 result lands on;
calls list + §8.2 result receiver).
Chunk 5: A6 (telephony spike — REAL transports behind the same boundary:
Vapi start-call + end-of-call webhook in managed mode, Plivo outbound +
answer XML + placeholder media-stream endpoint in self-hosted mode,
per-language Vapi voice profiles in the tenant settings. A real
transport runs only when TELEPHONY_MODE equals the campaign's mode AND
the tenant's provider is ready; stub remains the default everywhere
else. The conversation brain — Sarvam STT/LLM/TTS — is A7/A8.)
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import providers as prov
from . import seed as seed_module
from .config import settings
from .db import SessionLocal, init_db
from .routers import agents, auth, calls, campaigns, settings as settings_router, telephony, tenants

app = FastAPI(title="Voice Agent Platform", version="0.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev only — restrict before production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()
    db = SessionLocal()
    try:
        seed_module.seed(db)
    finally:
        db.close()


@app.get("/api/health")
def health():
    # Booleans only — provider VALUES are never reported, here or anywhere.
    # `providers` is the per-mode view of the .env fallback (health has no
    # tenant context); tenant-level configuration lives in /api/settings.
    return {
        "status": "ok",
        "service": "voice-agent-platform",
        "telephony_mode": settings.TELEPHONY_MODE,
        "providers_configured": {
            "vapi": bool(settings.VAPI_API_KEY),
            "plivo": bool(settings.PLIVO_AUTH_ID),
            "sarvam": bool(settings.SARVAM_API_KEY),
        },
        "providers": prov.env_provider_status(),
    }


app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(agents.router)
app.include_router(campaigns.router)
app.include_router(settings_router.router)
app.include_router(calls.router)
app.include_router(telephony.router)
