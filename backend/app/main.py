"""Voice Agent Platform — application layer API (Phase A).

Chunk 1: A1 (tenancy + core data model) and A2 (Agent Config Studio).
Chunk 2: A3 (campaigns & leads — CRUD bound to one agent, CSV lead upload
with column mapping, schedule/retry config; dispatch engine is A5/A10).
Telephony is NOT wired yet (A5+); the app boots and is testable with
TELEPHONY_MODE=stub and no provider keys.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import seed as seed_module
from .config import settings
from .db import SessionLocal, init_db
from .routers import agents, auth, campaigns, tenants

app = FastAPI(title="Voice Agent Platform", version="0.2.0")

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
    return {
        "status": "ok",
        "service": "voice-agent-platform",
        "telephony_mode": settings.TELEPHONY_MODE,
        "providers_configured": {
            "vapi": bool(settings.VAPI_API_KEY),
            "plivo": bool(settings.PLIVO_AUTH_ID),
            "sarvam": bool(settings.SARVAM_API_KEY),
        },
    }


app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(agents.router)
app.include_router(campaigns.router)
