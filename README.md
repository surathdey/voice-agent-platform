# Voice Agent Platform

Greenfield build of the AI voice-agent platform from *Detailed Design —
Voice Agent Platform V2 (Greenfield + QGBS MVP)*. Agents are **data, not
code**: each agent is one prompt plus structured voice/model/language
configuration, run in campaigns against lead lists.

**This build = Phase A, chunks 1–8: A1 (tenancy + core data model), A2
(Agent Config Studio), A3 (Campaigns & Leads), A4 (Provider Settings),
A5 (Orchestrator skeleton — dispatch + stub transport + §8.2 results),
A6 (Telephony spike — real Vapi + Plivo transports behind the same
boundary, per-language Vapi voice profiles), A7 (STT live — Sarvam
Saaras inside the media stream), A8 (the full conversation loop —
Saaras finals → Sarvam LLM → Bulbul TTS, with post-call classification
against the agent prompt's own disposition definitions) and A9
(barge-in + silence handling inside the loop — callee speech cuts
the agent's audio, silence earns a prompt, then an honest
`no_input` close).**
See `BUILD-LOG.md` for what is verified and what comes next
(A10: campaign dispatch at scale, results/dashboard, managed-mode
parity).

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
      (run `backend/.venv/bin/python scripts/smoke_test.py` — 311 checks automate all of this).

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
      stream; A7/A8 fill that stream with live transcription and the
      conversation brain (below).

**A7 — STT live (Sarvam Saaras in the self-hosted stream)**
- [ ] On a self-hosted call with a Sarvam key configured, opening the
      media stream also opens ONE Saaras streaming session for the call,
      in the language the agent's language rule resolves for the lead
      (West Bengal → bn-IN, otherwise hi-IN) — watch the Calls page:
      the row's **STT** pill shows `STT · bn-IN` while the call runs.
- [ ] Click a call row to expand it: final transcript segments appear
      as the customer speaks (persisted as they arrive), and the
      in-flight partial segment shows marked *(live…)*. The §8.2 result
      at the end carries the same accumulated transcript.
- [ ] Remove the Sarvam key and stream another call: the row shows
      **STT off**, the transcript is empty and the result is the A6
      spike outcome — no STT leg exists without a key.
- [ ] Point `SARVAM_WS_URL` at a dead address and stream a call: the
      row shows **STT failed** (hover for the reason), the transcript
      is headed by a `[TRANSCRIPT PARTIAL …]` marker, and the call
      still completes normally — a dead ear never fails a call.
- [ ] Self-hosted calls transcribe but (in A7 alone) speak nothing back
      and produce no disposition: the reply brain (LLM → Bulbul TTS)
      is A8, next.

**A8 — Full conversation loop (STT → LLM → TTS in the self-hosted stream)**
- [ ] On a self-hosted call with a Sarvam key configured, the agent
      speaks FIRST: the LLM opens the conversation from the agent
      prompt's FLOW, with the prompt itself as the system prompt
      (verbatim, lead variables like `{{lead_name}}` bound per call).
- [ ] Each final transcript segment drives one reply turn; replies are
      sentence-chunked into Bulbul TTS (the agent's voice; per call
      language) and the audio streams back into the call as it is
      generated. The Calls row's **Legs** column shows STT · LLM · TTS.
- [ ] When the prompt's flow reaches its closing, the model calls the
      end-call action: the closing is spoken in full, the stream ends,
      and the result's end reason is `agent_end_call`.
- [ ] Expand the finished call: the transcript is the REAL conversation
      (agent and customer turns interleaved, in order), and the result
      carries a **disposition + captured fields** classified after the
      call against the DISPOSITIONS definitions inside the agent's own
      single prompt — no separate rule store anywhere.
- [ ] Agent limits are honoured: set **Max turns** to 1 in the studio
      and the callee's answer gets no reply (end reason `max_turns`);
      a 1-second max duration ends the loop the same honest way
      (`max_duration`).
- [ ] Point `SARVAM_BASE_URL` at a dead address and stream a call: STT
      still transcribes live, nothing is spoken, the Legs column shows
      LLM/TTS failed, the disposition stays empty and the result is
      flagged **Needs review** — a dead brain degrades, never fakes.
- [ ] A classifier answer outside the prompt's bucket set (or
      unparseable) is never stored: disposition stays empty and the
      result is flagged for review with the reason in the summary.

**A9 — Barge-in + silence (inside the self-hosted loop)**
- [ ] Talk over the agent mid-reply (any speech the STT hears): the
      audio stops at once, a `clearAudio` flush is sent to the stream,
      and the agent answers what you actually said. Expand the call:
      the cut turn is marked *(interrupted — only the words actually
      spoken)* and the transcript holds only the words that were
      really spoken before the cut.
- [ ] Say nothing after the agent's turn: after **Silence prompt
      after (s)** (Agent studio; default 8) the agent prompts you —
      wording comes from its own prompt via the LLM, nothing is
      hard-coded. Answer, and the conversation continues; the prompt
      counter resets.
- [ ] Stay silent through **Silence prompts before close** prompts
      (default 1): the agent speaks its closing and the call ends
      with end reason `no_input` on the Calls page.
- [ ] Outbound audio is paced to real time (20 ms per frame), so
      what the platform counts as "spoken" is what the callee has
      actually heard — that is what makes the interruption record
      honest.

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
            app/stt.py = the STT leg (A7): Saaras streaming client, live
            transcript view, per-session STT persistence;
            app/conversation.py = the conversation brain (A8/A9): Sarvam
            chat-completions client (prompt as system prompt, end_call
            tool), sentence chunker, Bulbul TTS client, the per-call turn
            engine (barge-in interrupts, silence prompts, no_input
            close), and the post-call classifier;
            app/routers/ = auth, tenants, agents, campaigns, settings, calls,
            telephony (provider webhook / answer / stream routes);
            migrations/001–006 = DDL of record)
frontend/   React (Vite) console — theme tokens in src/theme.css (wireframe design language);
            pages: Agents, AgentEditor, Campaigns, CampaignWizard, CampaignDetail, LeadsUpload, Settings, Calls
scripts/    smoke_test.py — automated acceptance checks (A1 + A2 + A3 + A4 + A5 + A6 + A7 + A8 + A9)
docker-compose.yml   db (PostgreSQL 16) + backend + frontend (nginx)
```
