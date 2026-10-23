# Voice Agent Platform

Greenfield build of the AI voice-agent platform from *Detailed Design —
Voice Agent Platform V2 (Greenfield + QGBS MVP)*. Agents are **data, not
code**: each agent is one prompt plus structured voice/model/language
configuration, run in campaigns against lead lists.

**This build = Phase A, chunk 1: A1 (tenancy + core data model) and A2
(Agent Config Studio).** See `BUILD-LOG.md` for what is verified and what
comes next (A3: campaigns & leads).

## Run it (Docker — the owner's route)

```bash
cp .env.example .env        # defaults are fine for testing; provider keys can stay empty
docker compose up --build
```

- Console (frontend): http://localhost:5173
- API health: http://localhost:8000/api/health
- **Demo login:** `demo@qgbs.example` / `demo-password` (tenant **QGBS**, seeded on first boot with a sample *RPI Verification* agent)

No provider keys are needed to test: `TELEPHONY_MODE=stub` (see `.env.example`)
means everything except live calling works. Add `VAPI_API_KEY` /
`PLIVO_AUTH_ID` / `PLIVO_AUTH_TOKEN` / `SARVAM_API_KEY` later — they are
placeholders until the telephony chunks (A4–A6) land.

## Run it natively (development)

```bash
# backend
cd backend && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000        # SQLite file DB by default; set DATABASE_URL for PostgreSQL

# frontend (separate terminal)
cd frontend && npm install && npm run dev   # http://localhost:5173, proxies /api → :8000
```

## What to test (acceptance checklist)

**A1 — tenancy & data model**
- [ ] Log in as the demo user; the sidebar shows tenant **QGBS**.
- [ ] `GET /api/health` returns `telephony_mode: "stub"`.
- [ ] Create a second tenant (`POST /api/tenants`), log in as its admin:
      it sees **zero** agents, and QGBS agent URLs return 404
      (run `python3 scripts/smoke_test.py` — 17 checks automate all of this).

**A2 — Agent Config Studio**
- [ ] Agents library matches the wireframe layout: cards with LLM, STT/TTS,
      Captures, prompt version; the seeded RPI agent carries a
      **REFERENCE AGENT** badge.
- [ ] Create a complete agent in the UI — prompt, voice/model, language rule,
      captured fields — with **no code or database edits**.
- [ ] Edit the prompt and save: the version history gains a new version.
- [ ] Open an older version, roll back: the old text goes live again as a
      **new** version (history is append-only, nothing is rewritten).

## Design decisions that override the design doc

1. **Single prompt (owner, 2026-10-23).** The design doc's three-layer prompt
   assembly (script templates + disposition rules + main prompt) is
   **superseded**. Each agent has exactly ONE open-text prompt holding its
   dialogue/flow **and** the full disposition logic; the post-call classifier
   will read disposition definitions from that same text. Structured config
   outside the prompt: voice, model, language rule, captured-fields list.
2. **Prompt structure.** Prompts are written in four fixed sections:
   **ROLE / FLOW / DISPOSITIONS / CLOSINGS** (guidance shown in the studio).
3. **Prompt versioning.** Every prompt-changing save appends a version;
   history is viewable and any version can be restored (rollback appends a
   new version — history is never rewritten).

## Layout

```
backend/    FastAPI app (app/models.py = all six design entities + prompt_versions,
            app/routers/ = auth, tenants, agents; migrations/001_initial.sql = DDL of record)
frontend/   React (Vite) console — theme tokens in src/theme.css (wireframe design language)
scripts/    smoke_test.py — automated acceptance checks for this chunk
docker-compose.yml   db (PostgreSQL 16) + backend + frontend (nginx)
```
