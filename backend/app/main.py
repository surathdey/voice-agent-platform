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
Chunk 6: A7 (STT live — a Sarvam Saaras streaming session opens per
self-hosted call inside the Plivo media stream, in the lead's resolved
language; Plivo media frames are forwarded to it, partial transcripts
ride an in-memory live view, final segments persist to the session as
they arrive and land in the §8.2 result. No Sarvam config → no STT leg
and the stream behaves exactly like the A6 spike; an STT failure is
recorded on the session and marks the transcript partial — the call
itself never fails because the ear did. The reply brain — LLM/TTS —
is A8.)
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import box as box_module
from . import providers as prov
from . import seed as seed_module
from .config import settings
from .db import SessionLocal, init_db
from .routers import agents, auth, browser_test, calls, campaigns, settings as settings_router, telephony, tenants

# Turn-loop tracing (stall fix, Oct 26, 2026) logs at INFO through the
# "app" namespace — but the process has no handler at INFO for that
# namespace (uvicorn configures only its own loggers), which silently
# swallowed every app log line: the Oct 26 verify call left no trace
# lines at all for exactly this reason. Give the "app" namespace its
# own INFO handler here, at import time, so engine/telephone trace
# lines reach the container logs. The root level is untouched (no
# SQLAlchemy query flood); uvicorn's own loggers are untouched.
_app_log = logging.getLogger("app")
_app_log.setLevel(logging.INFO)
if not _app_log.handlers:
    _app_handler = logging.StreamHandler()
    _app_handler.setLevel(logging.INFO)
    _app_handler.setFormatter(
        logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
    _app_log.addHandler(_app_handler)

app = FastAPI(title="Voice Agent Platform", version="0.6.0")

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
        # Epic C (C1): which Secure Box this process is. The box dict
        # carries no secrets and no PII — slug + database fingerprint
        # only — so operators can verify box-to-database mapping.
        "box": box_module.box_info(),
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
app.include_router(browser_test.router)
