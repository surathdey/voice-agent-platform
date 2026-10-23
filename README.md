# Voice Agent Platform

Greenfield build of the AI voice-agent platform from *Detailed Design —
Voice Agent Platform V2 (Greenfield + QGBS MVP)*. Agents are **data, not
code**: each agent is one prompt plus structured voice/model/language
configuration, run in campaigns against lead lists.

**This build = Phase A, chunks 1–11: A1 (tenancy + core data model), A2
(Agent Config Studio), A3 (Campaigns & Leads), A4 (Provider Settings),
A5 (Orchestrator skeleton — dispatch + stub transport + §8.2 results),
A6 (Telephony spike — real Vapi + Plivo transports behind the same
boundary, per-language Vapi voice profiles), A7 (STT live — Sarvam
Saaras inside the media stream), A8 (the full conversation loop —
Saaras finals → Sarvam LLM → Bulbul TTS, with post-call classification
against the agent prompt's own disposition definitions), A9
(barge-in + silence handling inside the loop — callee speech cuts
the agent's audio, silence earns a prompt, then an honest
`no_input` close), A10 (dispatch at scale — schedule, calling
windows, days and daily caps evaluated in the tenant's MARKET-LOCAL
time, retry/re-queue of no-answer/busy/failed attempts as new
attempts, per-campaign results summary + tenant Dashboard, and
managed-mode parity with the self-hosted result surface) and A11
(browser test harness — talk to any agent from the console over a
WebSocket transport running the same engine: pick an agent, optionally
a campaign lead as a snapshot, and hold a real voice conversation;
the result lands in Calls marked "browser-test" and never consumes a
campaign attempt) and A12 (edge cases, load, and REAL per-call
cost — tenant rate cards in Provider Settings; the breakdown is
computed inside the one finalize path: telephony/STT/LLM from call
duration, TTS from the audio frames actually pumped; figures a
provider reports itself are kept verbatim).**
See `BUILD-LOG.md` for what is verified. **Phase A is complete** —
Phase B (the five agents, RPI first) comes next.

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

## Upgrading an existing deployment

Pulling new code and rebuilding is the whole upgrade:

```bash
git pull
docker compose up --build
```

On boot, the migration runner (`backend/app/migrations.py`) brings an
existing database up to the current schema **in place** — the numbered
steps in `backend/migrations/` are applied idempotently and recorded
in a `schema_migrations` table, so a database volume created by an
early build gains the columns and tables later builds added, with its
data intact. (Before this runner existed, an old volume kept its old
shape and a new build crashed at startup on the first missing column.)
A current database boots untouched — the runner is a no-op.

The nuclear option still exists for **test data only**: `docker compose
down -v` deletes the database volume, and the next `up` seeds a fresh
platform. Never use it on a database whose data you want to keep.

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
- [x] Log in as the demo user; the sidebar shows tenant **QGBS**.
- [x] `GET /api/health` returns `telephony_mode: "stub"`.
- [x] Create a second tenant (`POST /api/tenants`), log in as its admin:
      it sees **zero** agents, and QGBS agent URLs return 404
      (run `backend/.venv/bin/python scripts/smoke_test.py` — the suite's
      580 checks automate all of this).

**A2 — Agent Config Studio**
- [x] Agents library matches the wireframe layout: cards with LLM, STT/TTS,
      Captures, prompt version; the seeded RPI agent carries a
      **REFERENCE AGENT** badge.
- [x] Create a complete agent in the UI — prompt, voice/model, language rule,
      captured fields — with **no code or database edits**.
- [x] Edit the prompt and save: the version history gains a new version.
- [x] Open an older version, roll back: the old text goes live again as a
      **new** version (history is append-only, nothing is rewritten).

**A3 — Campaigns & Leads**
- [x] Campaigns list: status summary badges + table (agent, channel, leads).
- [x] “+ New Campaign” walks the 5-step wizard — Agent → Details → Leads →
      Schedule → Review — in the wireframe layout. Details asks for product /
      purpose / audience and a named **Channel**; there is deliberately **no**
      phone-number field and nothing agent-level (no model/voice/prompt).
- [x] Step 3: drop an RPI-format CSV (`callee_name, phone_number, bank_name,
      state`) — the mapping is suggested automatically; upload reports
      created / duplicates skipped / invalid. For a vehicle-loan campaign, a
      bank file's `RegistrationNumber` maps to the **product identifier**.
- [x] Campaign page: launch / pause / resume / complete follow the lifecycle
      (draft → active ⇄ paused → completed); the leads table shows **only
      that campaign's** leads.

**A4 — Provider Settings**
- [x] Settings → Providers lists both modes: Vapi + Twilio (managed),
      Plivo + Exotel + Sarvam (self-hosted). A fresh tenant shows the
      primary providers as **Stub** and the optional ones **Not configured**.
- [x] Save a key: it re-reads as `••••` + last 4 only — the full secret
      never appears in any screen or API response again.
- [x] The Vapi card also holds the tenant-default **model + voice** used
      at managed dispatch (the Agent editor's Voice & model section is for
      self-hosted mode — its info note says so).
- [x] “Test connection” with nothing saved reports **stub** honestly; with
      a fake key it reports **failed** (it really tries; success is never
      faked). Tenant B cannot see tenant A's settings.
- [x] A campaign whose mode has no working provider still saves, but its
      page carries the stub-mode flag naming what's missing; configuring
      the providers (Plivo + Sarvam, or Vapi) clears it.

**A5 — Dispatch & Calls (orchestrator skeleton, stub transport)**
- [x] On an **active** campaign with leads, “Run dispatch (stub)” admits
      one attempt per pending lead and reports how many were created and
      finalized; on a draft / paused / completed campaign it refuses.
- [x] The campaign page shows attempt counts by state; the **Calls** page
      lists every session with state, disposition, duration, mode and a
      gold **stub** transport marker — nothing pretends a real call ran.
- [x] Dispositions come from the **agent's own prompt** (its DISPOSITIONS
      section), round-robin in prompt order — edit the prompt's buckets
      and the next dispatch follows the new set.
- [x] Running dispatch again creates nothing new; each lead shows exactly
      one attempt. A duplicate result delivery (`POST /api/results`) is a
      no-op — exactly one result per session, always.
- [x] Admission honours the schedule: outside the calling window nothing
      is admitted, and the daily cap admits only that many per day.

**A6 — Telephony spike (real transports, voice profiles)**
- [x] With `TELEPHONY_MODE=stub` (the default), dispatch never touches a
      provider — even with every key configured. A real transport runs
      only when `TELEPHONY_MODE` equals the campaign's mode **and** the
      tenant's provider for that mode is configured.
- [x] **Settings → Providers → Vapi** shows a Voice profiles section
      (English / Hindi / Bengali: provider, TTS model, female + male
      voice IDs) pre-filled with the owner's verified RPI voice setup
      (11labs; English eleven_turbo_v2_5, Hindi + Bengali eleven_v3).
      Edit one — the dispatch payload for that language uses your value.
- [x] The same card carries **Transcriber profiles** (the STT side:
      provider + model per language, Deepgram via Vapi), likewise
      pre-filled and editable per tenant. Shipped defaults: English +
      Hindi `nova-2`; Bengali `nova-3` — Vapi's nova-2 language enum
      has no Bengali, so a Bengali call on nova-2 is rejected at
      placement (HTTP 400, live-proven 2026-10-23). When a placement is
      rejected, the call record keeps the provider's own error message,
      not just the HTTP status.
- [x] In the Agent studio, **Voice gender** (female / male) selects
      which of the profile's voice IDs a managed call uses; the
      assistant (LLM) model is the separate Vapi **Default model**.
- [x] Managed dispatch (`TELEPHONY_MODE=managed` + a Vapi key) *places*
      calls — sessions sit in DIALING on the Calls page and land as
      §8.2 results when Vapi's end-of-call webhook arrives (set
      `WEBHOOK_SECRET` + a public `PUBLIC_BASE_URL` for a live run).
- [x] Self-hosted dispatch (`TELEPHONY_MODE=self_hosted` + Plivo +
      Sarvam) places Plivo calls whose answer URL opens the media
      stream; A7/A8 fill that stream with live transcription and the
      conversation brain (below).

**A7 — STT live (Sarvam Saaras in the self-hosted stream)**
- [x] On a self-hosted call with a Sarvam key configured, opening the
      media stream also opens ONE Saaras streaming session for the call,
      in the language the agent's language rule resolves for the lead
      (West Bengal → bn-IN, otherwise hi-IN) — watch the Calls page:
      the row's **STT** pill shows `STT · bn-IN` while the call runs.
- [x] Click a call row to expand it: final transcript segments appear
      as the customer speaks (persisted as they arrive), and the
      in-flight partial segment shows marked *(live…)*. The §8.2 result
      at the end carries the same accumulated transcript.
- [x] Remove the Sarvam key and stream another call: the row shows
      **STT off**, the transcript is empty and the result is the A6
      spike outcome — no STT leg exists without a key.
- [x] Point `SARVAM_WS_URL` at a dead address and stream a call: the
      row shows **STT failed** (hover for the reason), the transcript
      is headed by a `[TRANSCRIPT PARTIAL …]` marker, and the call
      still completes normally — a dead ear never fails a call.
- [x] Self-hosted calls transcribe but (in A7 alone) speak nothing back
      and produce no disposition: the reply brain (LLM → Bulbul TTS)
      is A8, next.

**A8 — Full conversation loop (STT → LLM → TTS in the self-hosted stream)**
- [x] On a self-hosted call with a Sarvam key configured, the agent
      speaks FIRST: the LLM opens the conversation from the agent
      prompt's FLOW, with the prompt itself as the system prompt
      (verbatim, lead variables like `{{lead_name}}` bound per call).
- [x] Each final transcript segment drives one reply turn; replies are
      sentence-chunked into Bulbul TTS (the agent's voice; per call
      language) and the audio streams back into the call as it is
      generated. The Calls row's **Legs** column shows STT · LLM · TTS.
- [x] When the prompt's flow reaches its closing, the model calls the
      end-call action: the closing is spoken in full, the stream ends,
      and the result's end reason is `agent_end_call`.
- [x] Expand the finished call: the transcript is the REAL conversation
      (agent and customer turns interleaved, in order), and the result
      carries a **disposition + captured fields** classified after the
      call against the DISPOSITIONS definitions inside the agent's own
      single prompt — no separate rule store anywhere.
- [x] Agent limits are honoured: set **Max turns** to 1 in the studio
      and the callee's answer gets no reply (end reason `max_turns`);
      a 1-second max duration ends the loop the same honest way
      (`max_duration`).
- [x] Point `SARVAM_BASE_URL` at a dead address and stream a call: STT
      still transcribes live, nothing is spoken, the Legs column shows
      LLM/TTS failed, the disposition stays empty and the result is
      flagged **Needs review** — a dead brain degrades, never fakes.
- [x] A classifier answer outside the prompt's bucket set (or
      unparseable) is never stored: disposition stays empty and the
      result is flagged for review with the reason in the summary.

**A9 — Barge-in + silence (inside the self-hosted loop)**
- [x] Talk over the agent mid-reply (any speech the STT hears): the
      audio stops at once, a `clearAudio` flush is sent to the stream,
      and the agent answers what you actually said. Expand the call:
      the cut turn is marked *(interrupted — only the words actually
      spoken)* and the transcript holds only the words that were
      really spoken before the cut.
- [x] Say nothing after the agent's turn: after **Silence prompt
      after (s)** (Agent studio; default 8) the agent prompts you —
      wording comes from its own prompt via the LLM, nothing is
      hard-coded. Answer, and the conversation continues; the prompt
      counter resets.
- [x] Stay silent through **Silence prompts before close** prompts
      (default 1): the agent speaks its closing and the call ends
      with end reason `no_input` on the Calls page.
- [x] Outbound audio is paced to real time (20 ms per frame), so
      what the platform counts as "spoken" is what the callee has
      actually heard — that is what makes the interruption record
      honest.

**A10 — Dispatch at scale, results & dashboard, managed parity**
- [x] The sidebar's **Dashboard** is the landing view: active
      campaigns, calls today, results recorded and the human-review
      count, plus per-disposition totals across campaigns. "Today"
      and every schedule on the platform run on the tenant's market
      clock — the Dashboard names the timezone (QGBS: Asia/Kolkata).
- [x] Set a campaign's calling window around the CURRENT market-local
      time and dispatch: attempts are admitted. The same wall-clock
      window in UTC no longer decides anything — windows, calling
      days, start dates and daily caps are all market-local now
      (A5 evaluated the window in UTC).
- [x] On a campaign page, the **Results** card shows attempts made,
      connected count, connect rate and counts per disposition —
      the buckets are the ones in the bound agent's own prompt,
      zero-filled — plus leads by state (pending / attempted /
      exhausted).
- [x] Calls page filters: pick a campaign, a disposition, or tick
      "Needs review only" — the list and the state counts follow the
      filters. A result whose classification could not be trusted
      (either mode) is the kind that shows up under the review filter.
- [x] Retry: on a managed campaign, let a call end **no-answer**
      (fake/test provider) — the lead returns to *pending* with a
      "Next attempt" time (last attempt + the campaign's retry gap).
      Dispatch again inside the gap: nothing is admitted and the
      message says the lead is waiting. After the gap (or with a
      0-day gap), dispatch creates **attempt 2** — a new call record;
      the attempt-1 result is untouched, one result per attempt.
      After the campaign's max attempts, the lead is *exhausted* and
      is never dispatched again.
- [x] Parity: a managed (Vapi) call and a stub call open to the exact
      same record shape on the Calls page — disposition, duration,
      recording, captured fields, transcript, cost breakdown — and a
      connected managed call whose analysis returns no disposition
      lands flagged **Needs review**, same as a self-hosted call the
      classifier could not place.

**A11 — Browser test harness (talk to an agent, no phone)**
- [x] Sidebar → **Browser Test**: pick an agent, press **Start test
      call**, allow the microphone — the agent speaks first from its
      prompt's FLOW and you hold a two-way voice conversation in the
      browser (headphones recommended: through speakers the agent can
      hear itself). Needs a Sarvam configuration (Settings or env) —
      with none, the page says so and the attempt lands in Calls as a
      FAILED result, never a silent hang.
- [x] The live transcript appears line by line as you talk: your
      in-flight words show marked *(live…)*; talk over the agent
      mid-reply and its audio stops at once — the cut turn is marked
      *(interrupted — only the words actually spoken)*.
- [x] End the test three ways and check Calls each time: let the
      agent reach its closing (its `end_call`), press **End test
      call**, or stay silent until its silence guard closes the call
      (`no_input`). Every ending produces exactly one result.
- [x] Pick a campaign + one of its saved leads for the test: the
      agent addresses that lead (name, bank, language from its
      state) — then check the campaign page: the lead's attempt
      count is unchanged, it is still *pending*, and the campaign's
      Results show zero attempts. A test is a snapshot, never an
      attempt, and never creates a retry.
- [x] The finished test sits in **Calls** with a gold **Browser
      test** marker instead of a campaign, the `[BROWSER TEST]`
      summary prefix, a disposition classified against the agent's
      own prompt, and it counts on the Dashboard like any call.

**A12 — Real per-call cost, edge cases, load**
- [x] Settings → Providers → **Rate card**: per-minute rates for
      telephony (Vapi USD / Plivo INR), Sarvam STT, LLM and TTS —
      plain settings, shipped empty; an unset rate honestly costs 0.
- [x] Finish a self-hosted call and open it in Calls: the breakdown
      prices telephony + STT + LLM from the call's duration and TTS
      from the synthesized audio actually pumped (a barged-in
      sentence counts only its pumped frames), each leg with its
      basis note and the rate snapshot used. A Vapi report carrying
      its own cost is kept verbatim; one without is priced
      telephony-only from the rate card and says so. Stub results
      stay zero, marked stub.
- [x] Campaign Results and the Dashboard show total cost and cost
      per connected call **per currency** (INR and USD never
      mixed); stub runs never enter the totals. A browser test
      costs its real legs with telephony 0, labelled browser.
- [x] Edge cases behave: zero-lead dispatch is a clean no-op; a
      malformed number fails at placement without stopping the
      batch; a provider that accepts and then hangs ends FAILED
      honestly; the same end-of-call report delivered 3× still
      writes exactly one result; completing a campaign mid-dispatch
      stops it (queued attempts withdrawn, leads untouched); the
      tenant concurrency cap binds across two campaigns at once;
      another tenant's IDs return 404/zeros on every surface.
- [x] Load: a stub campaign of 500 leads dispatches to completion —
      500 sessions, exactly 500 results, disposition counts summing
      to 500 (≈7–9 s on the build VM).

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
            app/market_time.py = the tenant market timezone — windows, days,
            caps and retry due times are all evaluated on it (A10);
            app/costing.py = rate cards + the finalize-time cost
            computation (A12: per-leg rates, TTS from pumped frames,
            provider-reported figures kept verbatim, per-currency
            aggregate helper for results + dashboard);
            app/orchestrator.py = dispatch service (market-local schedule
            admission, tenant-concurrency batches, retry/re-queue), stub
            transport, the one idempotent finalize path for §8.2 results;
            app/transports.py = the real transports (A6): Vapi start-call +
            report mapping (review-flag parity, A10), Plivo create-call +
            answer XML + spike outcomes;
            app/stt.py = the STT leg (A7): Saaras streaming client, live
            transcript view, per-session STT persistence;
            app/conversation.py = the conversation brain (A8/A9): Sarvam
            chat-completions client (prompt as system prompt, end_call
            tool), sentence chunker, Bulbul TTS client, the per-call turn
            engine (barge-in interrupts, silence prompts, no_input
            close), and the post-call classifier;
            app/audio_codec.py = hand-rolled G.711 μ-law codec + the
            PCM16 16 kHz → μ-law 8 kHz downsample the browser harness
            feeds the STT session with (A11);
            app/routers/ = auth, tenants, agents, campaigns, settings, calls
            (incl. campaign results summary + tenant dashboard, A10),
            telephony (provider webhook / answer / stream routes),
            browser_test (A11: test-session create + the browser media
            stream — the same engine/STT/finalize path, snapshot leads,
            channel "browser-test");
            migrations/001–009 = DDL of record)
frontend/   React (Vite) console — theme tokens in src/theme.css (wireframe design language);
            pages: Dashboard, Agents, AgentEditor, Campaigns, CampaignWizard,
            CampaignDetail (dispatch + Results card incl. cost), LeadsUpload, Settings,
            Calls (result filters + per-call cost breakdown), BrowserTest (A11 harness: mic capture
            via AudioWorklet, Web Audio playback, live transcript)
scripts/    smoke_test.py — automated acceptance checks (A1 + A2 + A3 + A4 + A5 + A6 + A7 + A8 + A9 + A10 + A11 + A12)
docker-compose.yml   db (PostgreSQL 16) + backend + frontend (nginx)
```
