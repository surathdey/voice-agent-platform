# Voice Agent Platform

Greenfield build of the AI voice-agent platform from *Detailed Design —
Voice Agent Platform V2 (Greenfield + QGBS MVP)*. Agents are **data, not
code**: each agent is one prompt plus structured voice/model/language
configuration, run in campaigns against lead lists.

**This build = Phase A, chunks 1–5: A1 (tenancy + core data model), A2
(Agent Config Studio), A3 (Campaigns & Leads), A4 (Provider Settings),
A5 (Orchestrator skeleton — dispatch + stub transport + §8.2 results) and
A6 (Telephony spike — real Vapi + Plivo transports behind the same
boundary, per-language Vapi voice profiles).**
See `BUILD-LOG.md` for what is verified and what comes next
(A7: Sarvam STT inside the media stream).

## Run it (Docker — the owner's route)

```bash
cp .env.example .env        # defaults are fine for testing; provider keys can stay empty
docker compose up --build
```

- Console (frontend): http://localhost:5173
- API health: http://localhost:8000/api/health
- **Demo login:** `demo@qgbs.example` / `demo-password` (tenant **QGBS**, seeded on first boot with a sample *RPI Verification* agent)

No provider keys are needed to test: `TELEPHONY_MODE=stub` (see `.env.example`)
means everything except live calling works. Provider credentials can be
supplied per tenant in the console under **Settings → Providers** (tenant
values override the `.env` placeholders; secrets there are stored
server-side and only ever shown masked, `••••` + last 4).

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
      (run `backend/.venv/bin/python scripts/smoke_test.py` — 183 checks automate all of this).

**A2 — Agent Config Studio**
- [ ] Agents library matches the wireframe layout: cards with LLM, STT/TTS,
      Captures, prompt version; the seeded RPI agent carries a
      **REFERENCE AGENT** badge.
- [ ] Create a complete agent in the UI — prompt, voice/model, language rule,
      captured fields — with **no code or database edits**.
- [ ] Edit the prompt and save: the version history gains a new version.
- [ ] Open an older version, roll back: the old text goes live again as a
      **new** version (history is append-only, nothing is rewritten).

**A3 — Campaigns & Leads**
- [ ] Campaigns list: status summary badges + table (agent, channel, leads).
- [ ] “+ New Campaign” walks the 5-step wizard — Agent → Details → Leads →
      Schedule → Review — in the wireframe layout. Details asks for product /
      purpose / audience and a named **Channel**; there is deliberately **no**
      phone-number field and nothing agent-level (no model/voice/prompt).
- [ ] Step 3: drop an RPI-format CSV (`callee_name, phone_number, bank_name,
      state`) — the mapping is suggested automatically; upload reports
      created / duplicates skipped / invalid. For a vehicle-loan campaign, a
      bank file's `RegistrationNumber` maps to the **product identifier**.
- [ ] Campaign page: launch / pause / resume / complete follow the lifecycle
      (draft → active ⇄ paused → completed); the leads table shows **only
      that campaign's** leads.

**A4 — Provider Settings**
- [ ] Settings → Providers lists both modes: Vapi + Twilio (managed),
      Plivo + Exotel + Sarvam (self-hosted). A fresh tenant shows the
      primary providers as **Stub** and the optional ones **Not configured**.
- [ ] Save a key: it re-reads as `••••` + last 4 only — the full secret
      never appears in any screen or API response again.
- [ ] The Vapi card also holds the tenant-default **model + voice** used
      at managed dispatch (the Agent editor's Voice & model section is for
      self-hosted mode — its info note says so).
- [ ] “Test connection” with nothing saved reports **stub** honestly; with
      a fake key it reports **failed** (it really tries; success is never
      faked). Tenant B cannot see tenant A's settings.
- [ ] A campaign whose mode has no working provider still saves, but its
      page carries the stub-mode flag naming what's missing; configuring
      the providers (Plivo + Sarvam, or Vapi) clears it.

**A5 — Dispatch & Calls (orchestrator skeleton, stub transport)**
- [ ] On an **active** campaign with leads, “Run dispatch (stub)” admits
      one attempt per pending lead and reports how many were created and
      finalized; on a draft / paused / completed campaign it refuses.
- [ ] The campaign page shows attempt counts by state; the **Calls** page
      lists every session with state, disposition, duration, mode and a
      gold **stub** transport marker — nothing pretends a real call ran.
- [ ] Dispositions come from the **agent's own prompt** (its DISPOSITIONS
      section), round-robin in prompt order — edit the prompt's buckets
      and the next dispatch follows the new set.
- [ ] Running dispatch again creates nothing new; each lead shows exactly
      one attempt. A duplicate result delivery (`POST /api/results`) is a
      no-op — exactly one result per session, always.
- [ ] Admission honours the schedule: outside the calling window nothing
      is admitted, and the daily cap admits only that many per day.

**A6 — Telephony spike (real transports, voice profiles)**
- [ ] With `TELEPHONY_MODE=stub` (the default), dispatch never touches a
      provider — even with every key configured. A real transport runs
      only when `TELEPHONY_MODE` equals the campaign's mode **and** the
      tenant's provider for that mode is configured.
- [ ] **Settings → Providers → Vapi** shows a Voice profiles section
      (English / Hindi / Bengali: provider, TTS model, female + male
      voice IDs) pre-filled with the owner's verified RPI voice setup
      (11labs; English eleven_turbo_v2_5, Hindi + Bengali eleven_v3).
      Edit one — the dispatch payload for that language uses your value.
- [ ] In the Agent studio, **Voice gender** (female / male) selects
      which of the profile's voice IDs a managed call uses; the
      assistant (LLM) model is the separate Vapi **Default model**.
- [ ] Managed dispatch (`TELEPHONY_MODE=managed` + a Vapi key) *places*
      calls — sessions sit in DIALING on the Calls page and land as
      §8.2 results when Vapi's end-of-call webhook arrives (set
      `WEBHOOK_SECRET` + a public `PUBLIC_BASE_URL` for a live run).
- [ ] Self-hosted dispatch (`TELEPHONY_MODE=self_hosted` + Plivo +
      Sarvam) places Plivo calls whose answer URL opens the media
      stream; in this build the stream speaks nothing yet (the Sarvam
      brain is A7/A8) and finalizes with an honestly labelled spike
      result.

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
backend/    FastAPI app (app/models.py = all six design entities + prompt_versions
            + provider_settings; app/providers.py = provider registry, masked
            views, credential resolution, Test connection;
            app/orchestrator.py = dispatch service, stub transport, the one
            idempotent finalize path for §8.2 results;
            app/transports.py = the real transports (A6): Vapi start-call +
            report mapping, Plivo create-call + answer XML + spike outcomes;
            app/routers/ = auth, tenants, agents, campaigns, settings, calls,
            telephony (provider webhook / answer / stream routes);
            migrations/001–004 = DDL of record)
frontend/   React (Vite) console — theme tokens in src/theme.css (wireframe design language);
            pages: Agents, AgentEditor, Campaigns, CampaignWizard, CampaignDetail, LeadsUpload, Settings, Calls
scripts/    smoke_test.py — automated acceptance checks (A1 + A2 + A3 + A4 + A5 + A6)
docker-compose.yml   db (PostgreSQL 16) + backend + frontend (nginx)
```
