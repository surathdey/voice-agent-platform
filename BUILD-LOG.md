# BUILD LOG

## Fix — Vapi webhook auth: reference our own server credential by ID (2026-10-23, post-A12)

### The defect, plainly
Follow-up to the `server` object fix below. The next live test still
401'd every real Vapi server message — but this time the auth-failure
fingerprint added in that fix showed exactly what Vapi sends:
`x-vapi-secret` present on every message, **64 characters**, its
sha8 not our WEBHOOK_SECRET's. The owner's Vapi dashboard named the
mechanism: on his phone number — *"Credential fallback active — No
credential is explicitly assigned to this phone number. Your
organization's default server credential will be used
automatically."* Vapi's current server-auth model is
credential-based: real server messages are signed with the
**organization's default server credential** token, not with our
inline `server.secret` and not with the legacy `serverUrlSecret`.
Our webhook was right to reject them — they were signed with a
foreign secret.

Per Vapi's docs (server-url/server-authentication), the remedy is
to reference **our own** credential on the server object —
`{"server": {"url": ..., "credentialId": ...}}` replaces inline
auth. The owner creates that credential in the Vapi dashboard
(Bearer type, header X-Vapi-Secret, no Bearer prefix, token = the
platform's WEBHOOK_SECRET value) and pastes its ID into Settings.

### Built
- **New tenant field** on the Vapi provider, following the A6
  pattern exactly (config env override → provider field spec →
  Settings renders it generically): `server_credential_id`, label
  "Server credential ID (webhook auth)", **non-secret** (it is an
  ID; the credential and its token live in the tenant's Vapi
  account), empty default. Env override `VAPI_SERVER_CREDENTIAL_ID`
  in `backend/app/config.py`, documented in `.env.example`, passed
  through `docker-compose.yml` like the other VAPI_* vars. The
  Vapi card's description in Settings now explains the field.
- **`backend/app/transports.py` — `build_vapi_payload`**,
  `assistant.server` precedence: a resolved credential ID (tenant
  field → .env) → `{"url": <webhook URL>, "credentialId": <id>}`
  with **no** inline secret in the server object; else
  WEBHOOK_SECRET set → the previous `{"url": ..., "secret": ...}`;
  else no server object (unchanged). The legacy top-level
  serverUrl/serverUrlSecret are exactly as they were.

### Verification
- `scripts/smoke_test.py`, A6 section (new sub-section "Vapi
  server credential ID — credential-based webhook auth"): the
  field pre-fills empty (no registry default, `secret` False); a
  tenant override sticks with source = tenant; with the ID set to
  a dummy `cred_test_123`, the fake-captured create-call payload's
  `assistant.server` equals `{"url": <webhook URL>,
  "credentialId": "cred_test_123"}` and carries no "secret" key;
  after clearing the field, a fresh dispatch's payload is back to
  the inline-secret shape — and the pre-existing secret-mode
  payload assertion still passes.
- Full suite: **627 passed / 0 failed**. NOT yet live-verified:
  the owner's next live dispatch — with the credential created in
  Vapi and its ID saved in Settings — is the deciding test. If any
  real message still 401s, the fingerprint line remains the
  evidence of what Vapi actually sent.

## Fix — Vapi webhook 401s: the `server` object + auth-failure fingerprint (2026-10-23, post-A12)

### The defect, plainly
Live test, 2026-10-23: every real Vapi server message to
`POST /api/telephony/vapi/webhook` came back **401**, so no managed
call ever finalized — while a manual curl carrying
`x-vapi-secret: <the configured WEBHOOK_SECRET>` with a status-update
body returned **200** (`handled: false`, correctly ignored). The
handler, the secret and the tunnel were each proven good; what Vapi
actually sends on real messages was invisible from our side.

Per Vapi's current docs (server-url/server-authentication; Hookdeck's
Vapi skill), server-message authentication is opt-in and the secret
Vapi transmits as the `X-Vapi-Secret` header lives on the assistant's
inline **`server` object** — `{"server": {"url": ..., "secret": ...}}`.
Our payload set only the legacy `serverUrl` / `serverUrlSecret`
fields on the assistant: the URL is honoured for routing (the
messages did arrive), and the secret is stored and echoed back by
Vapi — but never transmitted as a header. Hence a correctly
configured webhook rejecting every genuine message.

### Built
- **`backend/app/transports.py` — `build_vapi_payload`** now also
  sets `assistant.server = {"url": <the same webhook URL>, "secret":
  settings.WEBHOOK_SECRET}`, only when a secret is configured
  (skipped entirely when WEBHOOK_SECRET is empty, so stub/local
  payloads are unchanged). The legacy serverUrl/serverUrlSecret stay
  exactly as they were — routing from them is live-proven, and the
  duplication is the same URL.
- **`backend/app/routers/telephony.py` — auth-failure fingerprint.**
  On the 401 path only (responses unchanged; the body is read once
  up front so its size is known — Starlette caches it and the
  post-auth JSON parse is untouched), the handler logs one WARNING
  line: sorted lowercased header NAMES, and for `x-vapi-secret` /
  `authorization` only presence, length and the first 8 hex of the
  value's sha256, plus the expected secret's sha8, content type and
  body size. No header value, in whole or in part, ever reaches the
  log. This stays as the verifier: if real messages still 401 after
  the `server` object, one live request's fingerprint shows exactly
  what Vapi sent.

### Verification
- `scripts/smoke_test.py`, A6 section: new payload check —
  `assistant.server` equals {webhook URL, the configured test
  secret}; new regression checks — the wrong-secret POST still 401s
  and leaves exactly one fingerprint line carrying sha8(wrong
  secret) and expected sha8(configured secret), `present=True
  len=12`, and neither raw secret string; the correct-secret
  status-update still returns 200 handled=false and logs no
  fingerprint.
- Full suite: **611 passed / 0 failed** (two runs: 610 and 611 —
  the total wobbles by a check or two between runs, as before;
  0 failed is stable). NOT yet live-verified: the next live Vapi
  test decides whether real messages now arrive with the header —
  and if any still 401, the fingerprint line is the evidence.

## Fix — Migration runner: the upgrade path (2026-10-23, post-A12)

### The defect, plainly
The platform had **no upgrade path, and it was untested because every
prior verification used fresh databases**. Schema creation was
`Base.metadata.create_all`, which creates missing tables but never
alters existing ones, and the numbered files in `backend/migrations/`
were "DDL of record" that nothing executed. So a database volume
created by an early build kept its early shape forever. The owner hit
it on his first real Docker run of the complete Phase A code: he
pulled, rebuilt, and the backend died at startup —
`psycopg2.errors.UndefinedColumn: column tenants.timezone does not
exist` (migration 008, A10) — because his volume predated migrations
003–009 and nothing was ever going to apply them.

### Built
- **`backend/app/migrations.py` — a boot-time migration runner**,
  called from `db.init_db()` BEFORE `create_all` and the seed. It
  tracks applied steps in a `schema_migrations` table (version PK,
  name, applied_at) and mirrors the nine numbered files (which keep
  their numbering and intent, and remain the DDL of record — each
  file header now says so).
- **Baseline by marker introspection, not assumption.** A database
  without `schema_migrations` can be at ANY historical shape, so an
  unrecorded step is never applied blind: its marker — the live
  presence of the objects the step owns (008: `tenants.timezone` AND
  `leads.next_attempt_at`; 003: the `provider_settings` table; 009:
  `channel` + `test_lead` columns, plus campaign/lead nullability on
  PostgreSQL) — decides "backfill the record, run no DDL" vs "apply".
- **Applies are per-object idempotent** (each column added only if
  missing), so a partial state WITHIN a step converges and an
  interrupted step resumes safely on the next boot. Steps are
  dialect-aware: timestamp spellings per dialect; 002's
  `DEFAULT now()` (PostgreSQL-only) is expressed as a UTC timestamp
  literal so existing rows are stamped with the migration time in
  both dialects; 009's `DROP NOT NULL` runs on PostgreSQL only —
  exactly as 009's own header prescribes, SQLite dev databases keep
  the legacy NOT NULL (recreate those).
- **Single source of truth preserved.** On an empty database, step
  001 creates the initial tables FROM THE MODELS (later markers then
  verify true and backfill), so `create_all` — which still runs right
  after the runner — and the migrations cannot fight: fresh and
  ancient databases end at the identical model schema.

### Verification (native, SQLite — same Docker caveat as chunks 1–11)
- Runner probed directly against four shapes before the suite grew
  the section: 002-era (001/002 backfilled, 003–009 applied, tenant
  row intact with the 008 default), fresh (001/003 applied from the
  models, rest backfilled), partial-within-008 (timezone present,
  next_attempt_at missing → converged), 001-era (002 applied on top).
  Second run in every case: `[]` — a strict no-op.
- `scripts/smoke_test.py`: the new FIX section builds a database at
  the **002-era shape** (001's file executed verbatim + 002's
  columns, one legacy tenant row), boots the real app against it and
  asserts startup succeeds, `tenants.timezone` /
  `leads.next_attempt_at` / `call_sessions.channel` + `test_lead`
  (and every other step's objects) exist afterwards, the legacy row
  survived, and `schema_migrations` holds exactly 1–9; then boots a
  SECOND time and asserts the migration record and tenants are
  byte-identical; plus an explicit fresh-database boot assertion.
  **Full suite: 589 passed, 0 failed** (580 pre-existing green + the
  new FIX checks).
- The owner's own volume is the field test: his next
  `docker compose up --build` is the first production-shaped run of
  the runner (PostgreSQL dialect). The SQLite path above is the same
  code with dialect branches; the PostgreSQL branches (incl. 009's
  DROP NOT NULL) follow the files' DDL verbatim but have not run
  against a live PostgreSQL on this VM (no Docker) — stated plainly
  rather than implied.

### Known limitations (deliberate, for this fix)
- Indexes on tables that already existed are not backfilled (the
  001-era files define none; `create_all` adds indexes only with new
  tables). A performance nuance on ancient volumes, not a correctness
  gap; a future step can add index markers if it ever matters.
- Column TYPE drift between eras (e.g. campaigns.product VARCHAR(120)
  in 001 vs String(40) in the models) is not reconciled — markers
  check presence, not width; nothing in the app depends on the width.

### Next step
Owner re-runs `git pull && docker compose up --build` on his Mac —
his existing volume should now boot the full Phase A app with his
chunk-1-era test data intact. Then Phase B (B1, RPI) as planned.

## Chunk 11 — Phase A: A12 Real Per-Call Cost, Edge Cases, Load (2026-10-23)

### Built
- **Rate cards** (`app/costing.py` + the provider registry): per-minute
  rate fields on the existing provider cards — Vapi telephony (USD),
  Plivo telephony (INR), Sarvam STT / LLM / TTS (INR) — plain tenant
  settings like every other provider field, resolved tenant → env →
  unset, and shipped **empty**: an unset rate honestly costs 0; there
  is no price list anywhere in the code. The Settings UI gains a
  "Rate card" group on each provider; the same rates exist as
  optional env fallbacks (`VAPI_RATE_…`, `PLIVO_RATE_…`,
  `SARVAM_RATE_…`) wired through `.env.example` + compose.
- **Finalize-time computation**, inside `finalize_attempt` (still the
  single writer of results): a stub breakdown passes through
  untouched (zeros, mode "stub" — no fake money). A breakdown a
  provider reported itself (Vapi's own figures in the end-of-call
  report) is **kept verbatim**, stamped `cost_source: "provider"` —
  the stamp is applied only when the report actually carried
  figures; a report without any gets the rate-card fallback:
  telephony-only priced from the call duration, the other legs zero
  with a "not reported for this call" basis note. Everything else
  is computed from the rate card: telephony + STT from call
  duration; **LLM from call duration** — the honest basis, stated
  in the breakdown, because the Sarvam chat responses in this
  integration carry no usage figures to price per-token; **TTS from
  the synthesized audio actually pumped** — the conversation engine
  now counts pumped 160-byte μ-law frames (20 ms each) on every
  exit path, barge-in included, and both stream handlers (Plivo +
  browser) pass the count to finalize, so an interrupted sentence
  costs only the frames that were sent. Every computed breakdown
  carries per-leg basis notes and the rate snapshot used.
  Browser-test channel: the same computation, telephony 0 with a
  "browser channel" basis note.
- **Aggregates**: campaign results and the Dashboard now sum cost
  **per currency, never mixed** — total, costed calls, connected
  calls, cost per connected call. Stub results are excluded
  (simulated money is not money). The Calls detail shows the full
  breakdown with its basis notes; the Results card and Dashboard
  show the per-currency totals.
- **Edge behaviour in the dispatcher**: admission now respects the
  tenant concurrency limit **across campaigns** (in-flight sessions
  of all the tenant's campaigns count; held leads are reported as
  `concurrency_capped` and stay pending). The worker re-reads the
  campaign's status before **every** attempt: a campaign paused or
  completed mid-dispatch stops cleanly — the sessions this dispatch
  created but never ran are withdrawn (no provider call, no result,
  no attempt count) and reported as `cancelled_inactive`.
- **No migration** (010 stays unused): rate cards live in the
  provider-settings JSON; costs in the existing `cost_breakdown`
  JSON.

### Verification (native, fresh SQLite DBs — same Docker caveat as chunks 1–10)
- `scripts/smoke_test.py`: **580 passed, 0 failed** (the 478
  pre-existing checks all green, plus 102 new A12 checks including
  the settings round-trips inside them). `vite build` clean.
- **Load**: a stub campaign with **500 leads** dispatched to
  completion — 500 sessions, exactly 500 results (exactly-one-result
  under volume), per-disposition counts summing to 500 — in ≈7–9 s
  wall-clock across runs (9.0 s on the final full-suite run).
  Zero-lead dispatch is a clean no-op with a clear message.
- **Real cost, exact**: against mock Sarvam + the fake Plivo, a
  scripted call's breakdown equals rate × duration for telephony /
  STT / LLM and rate × pumped-frames × 20 ms for TTS — with the
  wire's playAudio event count matching the mock's synthesized
  frames one-for-one. A browser test on the same rates prices its
  legs identically, telephony 0. A 120-sentence agent turn
  (~3,000 characters) classifies normally — no crash, no
  truncation, no review-flag excuse.
- **Managed edges** against a purpose-built fake provider: a number
  that accepts then hangs ends FAILED (end_reason provider_error,
  "network error" on the record) after the transport timeout; a
  malformed number ends FAILED ("rejected the call placement
  (HTTP 400)"); the batch still places the good lead. The same
  end-of-call report delivered **3× writes exactly one result**,
  its provider-reported 0.05 kept verbatim. A report without
  figures falls back to telephony-only — 1.5 × 2 min = 3.0 USD
  once the rate is set, honest zeros before.
- **Mid-dispatch completion**: the fake completes the campaign
  inside the first placement — the dispatch places exactly 1 call,
  withdraws the other 2 queued attempts, and a re-dispatch is
  refused (campaign completed). **Concurrency**: tenant limit 2 —
  campaign A admits 2 of 3, campaign B admits 0; after A's calls
  finalize, A admits its last lead and B one of two (A's fresh call
  holds the other slot); once everything finalizes, B's held lead
  is admitted.
- **Cross-tenant fuzz**: the second tenant gets 404 on the first
  tenant's campaign results / calls / dispatch / status,
  browser-test create and result submit — and zeros (incl. empty
  costs) on the filtered Calls list and its own Dashboard.

### Known limitations (deliberate, for this chunk)
- LLM cost is per-minute of call duration, not per-token: the
  Sarvam responses in this integration carry no usage object. If a
  future API version returns usage, the rate field and the basis
  note are the only places that need to change.
- TTS frame counting is exact on the self-hosted and browser paths
  (our own pump). Managed calls rely on the provider's reported
  cost or the telephony-only fallback — there is no frame stream
  to count on that side.
- The worker is still synchronous inside the dispatch request (the
  A10 caveat): the concurrency cap is admission control across
  campaigns, not parallel in-flight execution. The 500-lead figure
  is the stub transport on SQLite; PostgreSQL + real transports
  will differ.
- Rate cards are per provider, not per campaign/voice/language —
  contracted rates are provider-level in practice; the settings
  shape allows a finer card later without a migration.

### Phase A — COMPLETE
Phase A (steps A1–A12, chunks 1–11) is built and verified end to
end: multi-tenant platform with tenant-scoped everything; the
Agent Config Studio with the single-prompt model (versioned,
append-only); campaigns bound to one agent with CSV lead intake;
provider settings with masked secrets and honest test-connection;
the orchestrator (market-local schedule admission, retries,
cross-campaign concurrency admission, mid-dispatch stop) behind
the one idempotent finalize path that writes every §8.2 result;
both orchestration modes — managed (Vapi) and self-hosted (Plivo
answer/stream + Sarvam Saaras STT → Sarvam LLM → Bulbul TTS, with
barge-in and silence handling) — producing the identical result
shape; the browser test harness on the same engine; and real
per-call costing from tenant rate cards with per-currency
aggregates. **580 automated checks pass; the console builds
clean.** What Phase A has never done is place a real call: every
transport is verified against local fakes/mocks by design. The
first live test (Vapi, with the owner's own credentials, pulled
once on his machine) is the owner's next step, and Phase B builds
the five agents on this platform — RPI first, as the reference.

### Next step
**Phase B, step B1 — the RPI Verification agent** as the reference
agent on this platform (its seeded sample becomes the real B1
configuration), followed by the owner's one-time pull + live Vapi
test when he is ready.

## Chunk 10 — Phase A: A11 Browser Test Harness (2026-10-23)

### Built
- **A "Browser test" page + a browser transport for the existing
  engine.** An operator picks any agent (tenant-scoped), optionally a
  campaign of that agent for context (its product + lead list) and
  either one of the campaign's saved leads or hand-typed fictitious
  test values, presses **Start test call**, allows the microphone, and
  holds a two-way voice conversation: the agent speaks first from its
  single prompt's FLOW, replies stream back as audio, the live
  transcript appears line by line (partials marked *(live…)*,
  interrupted turns marked), and the call ends by the model's
  `end_call`, the operator's **End test call** button, or the agent's
  existing guards (`max_turns` / `max_duration` / silence →
  `no_input`) — A8/A9 behaviour, unchanged, because the harness runs
  the **same** `ConversationEngine` + Saaras STT session as the Plivo
  stream (`backend/app/routers/browser_test.py`). No conversation
  logic was forked: the handler mirrors the Plivo stream handler's
  lifecycle (live view → engine → legs → classify → `finalize_attempt`)
  with a transport seam of three callables (send audio / clear audio /
  close stream).
- **The test is a snapshot, never an attempt.**
  `POST /api/browser-tests` creates a `CallSession` with
  `campaign_id = NULL`, `lead_id = NULL`, `channel = "browser-test"`
  and a `test_lead` JSON snapshot (migration
  `009_a11_browser_test.sql`; the two FK columns are now nullable).
  A picked lead is read once, snapshotted, and **never written back**:
  `finalize_attempt`'s lead-routing block (attempt counting, status,
  retry re-queue) is skipped entirely for the browser-test channel, so
  a test cannot consume attempts or schedule retries. The campaign
  supplies product/variables read-only. Results land in the same §8.2
  surface with the marker everywhere it matters: Calls shows a gold
  **Browser test** pill in place of a campaign, the summary is
  prefixed `[BROWSER TEST]`, and the dashboard counts the call like
  any other. The wire protocol: `WS /api/telephony/browser/stream/{id}`
  authenticated by the operator's JWT as a query parameter (browsers
  cannot set WebSocket headers; the token is the same short-lived
  operator token, and the session must belong to that operator's
  tenant — bad token closes 4401, cross-tenant 4404, non-startable
  4409).
- **Codec, honestly.** Browsers cannot produce μ-law, so the page
  captures microphone audio with Web Audio (AudioWorklet, with a
  ScriptProcessor fallback), **downsamples to PCM16 @16 kHz** by
  pair-averaging (the simplest robust path — no Opus decode anywhere),
  and sends binary PCM; the server (`backend/app/audio_codec.py`,
  hand-rolled G.711 μ-law — Python 3.13 removed `audioop`) converts to
  the exact 160-byte μ-law frames the STT session and the engine
  already eat, so the agent hears what a phone callee's audio becomes.
  Replies return as binary **PCM16 @8 kHz** messages, scheduled into
  Web Audio by the page. The smoke test asserts the conversion at the
  STT boundary itself: constant-value PCM chunks arrive at the mock
  Saaras as constant 160-byte μ-law payloads whose byte differs with
  the input sample value.
- **Barge-in rides the engine's interrupt, one flush event.** When an
  STT partial cuts a reply (A9's path, untouched), the handler's
  clear-audio callable sends a single `{"event": "interrupt"}` and the
  page stops the sounding buffer source and drops its playback queue.
  Difference from Plivo, stated plainly: Plivo's `clearAudio` asks the
  provider to flush audio already sent (best-effort — hence A9's
  real-time pacing); in the browser the *unsent* tail never leaves the
  server (same pump-stop) and the *sent* tail lives in the page's own
  queue, so the cut there is exact rather than best-effort. Playback
  pacing on the wire is identical (20 ms per frame), so "spoken"
  means the same thing in the transcript.
- **Honest refusals.** With no Sarvam configured for the tenant, the
  stream sends a `not_configured` error event and finalizes the
  session as a FAILED `[BROWSER TEST]` result (end reason
  `not_configured`) — visible in Calls, never a silent hang. A session
  whose stream never connects simply stays QUEUED.

### Verification
- `scripts/smoke_test.py`: **478 passed, 0 failed** (one repeat run
  showed 472 passed / 2 failed — an A8 timing check that passed in the
  other three runs, the suite's known poll/timing variance, plus the
  A11 classifier-routing miss fixed below; the final run is fully
  green). The new A11 section (48 counted checks) drives the harness
  protocol with a sync WebSocket client against mock Sarvam HTTP +
  a recording mock Saaras: session create (marker, snapshot, stream
  path) with zero campaign state touched; cross-tenant create → 404;
  stream closes 4401 (bad token) / 4404 (other tenant); `ready` names
  the resolved language (bn-IN from the lead's state); the agent
  speaks first (audio bytes exactly 2× the μ-law total, transcript
  event follows); two operator turns are answered in order and the
  model's `end_call` ends the test with the `ended` event carrying
  disposition ANSWERED + captured `favourite_colour`; the mock Saaras
  saw exactly one bn-IN connection and the μ-law conversion is
  asserted byte-for-byte in shape; the result is in Calls + dashboard
  with campaign results/attempts at zero and the picked lead still
  `pending`, attempt_count 0; an operator-End (socket close)
  finalizes exactly once as `stream_closed`; barge-in — a partial
  mid-opening produces the `interrupt` event with zero audio after it
  for that reply, the live partial rides a `partial` event, the
  interrupting line is answered, and the recorded opening turn is the
  spoken prefix flagged `interrupted`; no-config → `not_configured`
  event + FAILED result + no STT connection attempted. Frontend:
  `vite build` clean.
- **Two defects found by the harness tests, both in test-side
  assumptions, fixed in the section:** (1) the A8/A9 mock AI servers
  are shut down by their own sections — and a shut-down
  `HTTPServer` leaves its socket listening, so calls to the old URL
  connect and then hang to a 60 s timeout; A11 therefore boots its
  own twin mock serving both scripts. (2) Port 18112 already belongs
  to A10's retry instance (all instances live until the global
  finally); the barge instance moved to 18114.

### Known limitations (deliberate, for this chunk)
- The harness needs a Sarvam configuration (tenant settings or env)
  to run a conversation — it exercises the self-hosted loop; there is
  no managed-mode (Vapi) browser test, by design: Vapi has no
  browser-audio seam in this platform.
- Codec path is PCM16 @16 kHz → μ-law @8 kHz with pair-averaged
  downsampling (a box filter, not a polyphase resampler): entirely
  adequate for judging the agent, and exactly the STT input a phone
  call produces, but the page's microphone chain (browser echo
  cancellation, device sample rates) means a browser test is a
  judgement aid, not a telephony-fidelity measurement. Speakers
  without headphones can feed the agent's own voice back to it; the
  page says so.
- The WebSocket carries the operator JWT as a query parameter (the
  only browser-viable channel); it is the standard short-lived
  operator token, and the stream re-checks tenant ownership at
  connect. A dedicated short-lived stream token is a hardening step.
- Still **not proven against live providers** — no keys yet (owner
  step, unchanged). Everything above is mock/fake-verified, including
  the Saaras wire shape; the first live Sarvam spike remains the
  codec-negotiation check noted in A7.

### Next step
**A12 — edge cases, load & cost:** failure-path hardening across the
dispatch/stream surface, a concurrency/load pass over the
orchestrator, and real per-call cost figures in `cost_breakdown`
(the A5 contract has carried the shape since the skeleton; A12 puts
numbers in it).

## Chunk 9 — Phase A: A10 Dispatch at Scale, Results Surface & Dashboard, Managed-Mode Parity (2026-10-23)

### Built
- **Market-local schedule (the A5 UTC debt, closed).** New module
  `app/market_time.py` + `tenants.timezone` (migration
  `008_a10_dispatch_scale.sql`; provisioning derives it from the market
  — IN → Asia/Kolkata, NA → America/Toronto — and it is exposed on the
  tenant API). ALL schedule questions are now answered in the tenant's
  market time: the calling window, the calling days (parsed from the
  schedule's text — "Mon – Sat" / "Mon, Wed, Fri" / ranges; unparseable
  text imposes no restriction rather than silently blocking), the
  campaign start date, and the daily cap (sessions started on the
  market-local day count). The dispatch report carries the timezone it
  evaluated in and its refusal message names it.
- **Retry / re-queue (exactly-one-result stays per attempt, absolute).**
  `finalize_attempt` — still the single terminal writer — now routes
  the lead by outcome: NO_ANSWER / BUSY / FAILED with attempts
  remaining **re-queues** the lead (`pending` + `next_attempt_at` =
  attempt end + the campaign's retry gap; with `retry_different_time`
  the due time is moved to the opposite end of the calling window in
  market-local time). Dispatch admits a re-queued lead only once its
  gap has elapsed — as a NEW session with the next deterministic
  attempt_id (…_2, …_3) and its own single result; leads skipped for
  the gap are counted in the report (`retry_waiting`), never silently
  dropped. Attempts exhausted → `exhausted` (final); COMPLETED /
  VOICEMAIL → `attempted` (done, unchanged A5 semantics). Tenant
  concurrency batching is unchanged: the worker drains admitted
  attempts in batches of `tenant.concurrency_limit`, behind the same
  service boundary as A5/A6.
- **Results surface.** `GET /api/campaigns/{id}/results`: counts per
  disposition over the bucket set parsed from the campaign agent's own
  single prompt (zero-filled; a stored disposition outside the set is
  appended, never hidden), attempts made, results, connected count,
  connect rate, review count, leads by state + awaiting-retry count,
  total duration. `GET /api/dashboard` (the tenant home): active
  campaigns + campaigns by status, calls today (market-local), calls /
  results totals, per-disposition totals across campaigns, and the
  human-review queue count. `GET /api/calls` gains `?disposition=` and
  `?review_required=` filters (counts computed over the filtered set).
  UI: a new **Dashboard** page (nav enabled; the console's landing
  view) with stat cards + disposition bars + review-queue panel; the
  campaign page gains a **Results card** (attempts / connected /
  connect rate / needs-review stats, per-bucket bars, leads-by-state
  pills) and a Next-attempt column on the leads table; the Calls page
  gains a campaign / disposition / needs-review filter bar. Also
  folded in: `ResultIn` now actually carries `review_required` (the
  receiver read it, the schema dropped it).
- **Managed-mode parity (audited field by field).** The audit compared
  the Vapi webhook mapping against the stub outcome and the self-hosted
  finalize outcomes across the result surface: duration, recording
  reference, cost_breakdown, disposition, captured fields, transcript
  shape, final/end state, review flag. All were present and equivalent
  **except one gap, now closed**: a CONNECTED managed call whose
  analysis returned no disposition was stored as an unclassified
  success, where the self-hosted path flags exactly that case for
  review (A8 classifier honesty). `map_vapi_report` now sets
  `review_required` for COMPLETED + no disposition (with an explanatory
  summary when Vapi sent none) — no bucket is ever invented. The parity
  smoke test runs the same assertions against a stub-completed call and
  a fake-Vapi webhook-completed call: identical CallOut field set,
  identical cost-contract keys, and the shared guarantees (final state,
  disposition, duration > 0, captured-field typing, transcript turn
  shape, review flag semantics).

### Verification
- `scripts/smoke_test.py`: **426 passed, 0 failed**, repeat run
  **429/0** (the small count difference is the pre-existing poll-gated
  checks in the A5/A9 sections; every A10 check passed in both runs).
  All 337 pre-existing checks intact. The new A10 section (89 counted
  checks) covers: tenant timezone exposure; a window around
  market-local now admits (it excludes UTC-now by 5½ hours, so it
  proves the fix, not just the code path); a window 3 h ahead refuses
  with the timezone named; calling-days and start-date gates refuse;
  results summary exact against the A5 batch (8 prompt buckets,
  zero-filled, connect rate 1.0, leads by state); dashboard totals
  (12 calls/results tenant-wide, per-disposition totals, review queue
  0) and tenant-2 zeros + 404; calls filters (disposition present /
  absent / combined with campaign, review flag); retry on a fake-Vapi
  instance — gap 1 day re-queue (due ≈ +24 h, dispatch reports
  `retry_waiting: 1`, results show `awaiting_retry: 1`), gap 0 re-queue
  (second dispatch creates attempt-2 sessions; 4 attempts → 4 results,
  one each), exhaustion to the final state, and no re-dispatch after;
  parity (identical field sets + values, cost keys, transcript shape,
  and the review-flag gap closed: connected + no disposition → flagged,
  visible on the dashboard count and through the calls filter).
  Frontend: `vite build` clean.
- **Fixture note:** legacy dispatch fixtures (A5/A6 helpers) now pin
  `calling_days: "Mon - Sun"` — calling-days became enforced behaviour
  in this chunk, and those fixtures assert dispatch/window/cap
  behaviour, not weekday behaviour. Their assertions are unchanged.
  A campaign left on the product default ("Mon – Sat") will now
  genuinely not dispatch on a Sunday — that is the schedule working
  as configured.

### Known limitations (deliberate, for this chunk)
- The dispatch worker is still synchronous inside the dispatch
  request (the A5 boundary); concurrency is honoured as batch size,
  not as parallel in-flight calls. A background worker remains a
  production-hardening step behind the same `Dispatcher` boundary.
- Retry due times are computed once, at finalize, from the campaign's
  rules as they then stand; editing retry rules afterwards does not
  move already-stamped `next_attempt_at` values.
- `calling_days` is free text by design (A3); the parser covers the
  shapes the wizard produces and common variants, and treats anything
  else as "no day restriction" — a structured day picker is a UI
  refinement, not a dispatch blocker.
- The QGBS seed tenant's timezone is Asia/Kolkata; tenant timezones
  are set at provisioning (no tenant-settings editor yet — the API
  accepts `timezone` on tenant creation).
- Still **not proven against live providers** — no keys yet (owner
  step, unchanged). Everything above is mock/fake-verified.

### Next step
**A11 — Browser test harness:** an in-browser (microphone) test-call
surface so the owner can drive the self-hosted conversation loop
without a phone call, reusing the same stream/finalize path.

## Chunk 8 — Phase A: A9 Barge-In + Silence Handling in the Self-Hosted Loop (2026-10-23)

### Built
- **Barge-in (design §5.2 — a first-class event, never an error).** The
  A7 STT session gains an `on_partial` callback (same guarded contract
  as `on_final`): every non-empty partial is a "the callee is
  speaking" signal into the conversation engine. While a reply is
  being spoken, the signal sets the engine's interrupt: the token
  consumer races token delivery against the interrupt, the LLM stream
  task is cancelled, unsent sentences are **never synthesised**, and
  the in-flight sentence's frame pump stops at the next frame
  boundary. The agent turn is recorded with **only the words actually
  spoken** (fully delivered sentences + the in-flight sentence when at
  least one of its frames went out) and flagged `interrupted` — in
  the transcript, in the live view, and in the LLM history, so the
  model never "remembers" saying words the callee never heard. A
  model's `end_call` inside an interrupted turn is discarded — an
  interrupted closing is not a closing. The interrupting utterance
  then drives the next turn as normal input.
- **clearAudio semantics (documented in the stream handler).** Two
  cuts, honestly separated: stopping our own frame pump is the
  **authoritative** cut (no further `playAudio` is ever sent for the
  reply — asserted in stream order by the tests), and the handler
  additionally sends Plivo's `clearAudio` event carrying the stream
  id Plivo supplied, a best-effort flush of audio the provider has
  buffered but not yet played. Audio already played cannot be
  recalled; nothing claims otherwise.
- **Real-time playback pacing (new, and load-bearing).** The stream
  handler now paces outbound frames to the 8 kHz clock (20 ms per
  160-byte μ-law frame; first frame of a burst immediate; an idle gap
  restarts the clock). A8 forwarded whole sentences as fast as the
  socket took them, which made "sent" meaningless next to "heard"
  and left barge-in nothing to cut; with pacing the engine's speaking
  window is the callee's hearing window, in production and in tests
  alike.
- **Silence handling (§5.2).** The engine's input wait carries a
  deadline from the agent's `silence_prompt_after_s` (default 8 s),
  measured from the later of the wait start and the last speech
  activity — a callee mid-utterance (partials landing) re-arms the
  wait and is never nudged. On expiry the engine takes a **nudge
  turn**: an LLM turn with a bracketed engine note ("the callee has
  been silent…") as context — the spoken wording stays the model's
  own from the agent's prompt, nothing is hard-coded. How many
  nudges are allowed is the new agent field **`silence_max_prompts`**
  (migration `007_a9_barge_in_silence.sql`, Agent Studio field;
  default **1** = the design's behaviour: one prompt, a second
  silence closes). Any callee answer resets the counter. When the
  prompts are exhausted the engine takes a closing turn (same
  engine-note mechanism); if the callee answers even that, the call
  continues, but when the closing stands the call ends with
  end_reason **`no_input`** — assigned by the engine, deliberately
  overriding any `agent_end_call` the model set in that turn,
  because silence is the cause of the end.
- **Guards interplay.** Barge-in and silence live inside the A8 turn
  machinery, so `max_turns` / `max_duration_s` keep their exact
  meanings; silence turns consume turns like any other, and a guard
  that fires during a silence closing keeps its own end reason. The
  post-call classifier sees the coherent transcript (interruption
  markers included) and the finalize summary notes the interruption
  count and any `no_input` ending.
- **UI.** Agent Studio limits are now duration · max turns · silence
  prompt after (s) · silence prompts before close, with the A9
  behaviour spelled out in the hint. The Calls transcript renders an
  interrupted agent turn with a gold marker: "(interrupted — only
  the words actually spoken)".

### Verification
- `scripts/smoke_test.py`: **337 passed, 0 failed** (repeat run
  336/0 — the one-count difference is a pre-existing poll-gated A5
  check; every A9 check passed in both runs). All 311 pre-existing
  checks intact, including the A8 scripted call unchanged. The new
  A9 section runs against the same local mocks (mock Sarvam HTTP +
  mock Saaras; no real provider called):
  - **Barge-in scenario:** the mock Saaras emits a partial on the
    first inbound media frame, mid-opening. Asserted: `clearAudio`
    arrives carrying Plivo's stream id and **no `playAudio` follows
    it** in stream order; total audio is a strict cut of the full
    reply; TTS requests are a strict prefix of the reply's sentences
    (unsent sentences abandoned); the interrupting line is answered
    next; the transcript is exactly [agent (spoken prefix,
    `interrupted`), customer, agent reply]; the LLM history for the
    interrupting turn carries the spoken prefix, never the full
    reply; finalize = SPOKEN, no review, summary notes the
    interruption.
  - **Silence scenario** (agent configured 1 s / 1 prompt): opening
    spoken in full → nudge after the window (asserted as an LLM turn
    carrying the silence context) → callee answers, gets a reply →
    fresh silence earns a **fresh** nudge (counter reset) → final
    silence: closing spoken, stream closed by the server, finalize
    end_reason `no_input`; transcript exactly [opening, nudge,
    answer, reply, nudge, closing].
  - Frontend: `vite build` clean.
- **Defect found and fixed during verification:** the first wiring
  passed the engine's zero-argument `note_speech_activity` where
  the STT session calls `on_partial(text)`; the resulting TypeError
  was swallowed by the STT session's (correct) "a consumer must
  never break the session" guard, so partials landed in the live
  view but never reached the engine. The engine method now accepts
  the partial text, mirroring `on_final`'s contract. Both legs were
  additionally proven in isolation (STT probe + paced engine probe)
  before the integrated run went green.

### Known limitations (deliberate, for this chunk)
- The barge-in trigger is the STT partial signal (design-sanctioned:
  it is the speech signal we have). Raw inbound-media energy
  detection is not implemented — a callee making non-speech noise
  will not cut the agent, and a partial that arrives in the same
  instant a reply ends is treated as the next turn's input, which
  is the correct outcome anyway.
- `clearAudio` is best-effort by construction: Plivo offers no
  acknowledgement for it, and the platform does not pretend the
  provider's buffer state is knowable. The authoritative guarantee
  is on our side of the socket (no further frames).
- Silence timing is wall-clock from the agent's configured seconds;
  per-campaign overrides do not exist (silence is agent behaviour,
  like max turns).
- Still **not proven against the live Sarvam/Plivo APIs** — no keys
  yet (owner step, unchanged). Everything above is mock-verified.

### Next step
**A10 — Campaign dispatch at scale, results surface & dashboard,
managed-mode parity:** scheduled dispatch of a campaign's leads
through the A5 orchestrator in both modes, the end-of-campaign
output (per-agent spreadsheet shape), campaign progress/results
views, and the managed (Vapi) path reaching the same §8.2 result
surface as the self-hosted loop.

## Chunk 7 — Phase A: A8 Full Conversation Loop — STT → LLM → TTS in the Self-Hosted Stream (2026-10-23)

### Built
- **The conversation brain** (`backend/app/conversation.py`, wired into
  the stream handler in `routers/telephony.py` beside the A7 STT leg).
  When a self-hosted call's Sarvam key is configured, a
  `ConversationEngine` runs for the call: one kickoff turn at stream
  `start` (the agent speaks first — the LLM opens from the prompt's
  FLOW via a fixed "call connected" kickoff, no greeting text in code),
  then one reply turn per STT final, until the callee hangs up, the
  stream stops, the model ends the call, or a guard fires.
- **LLM leg:** Sarvam chat completions, streaming. The system prompt is
  the agent's **single prompt verbatim**, with lead variables bound per
  call (`{{lead_name}}`, `{{bank_name}}`, … — the same binding the Vapi
  leg performs; the stored prompt is never mutated). History accumulates
  across turns. One tool is offered — `end_call`: when the model calls
  it after the closing, the engine ends the stream (end reason
  `agent_end_call`). HTTP runs in worker threads bridged to the event
  loop via queues, so the media receive loop never blocks; request
  sockets carry a timeout so a dead provider fails the leg instead of
  hanging the call.
- **TTS leg:** the reply stream is sentence-chunked (terminators
  `. ! ? । …`); each completed sentence goes to Bulbul in its own
  request (design §8.4 — one synthesis stream per reply sentence) with
  the agent's voice — `tts_voice_id` when set, else derived from call
  language + the agent's voice gender — and the audio returns into the
  Plivo stream as `playAudio` μ-law frames at 8 kHz.
- **Guards (agent limits, finally wired end to end):** `max_turns`
  (new — migration `006_a8_conversation.sql`, Agent Studio field beside
  max duration, create/update API) and `max_duration_s` end the loop
  honestly with end reasons `max_turns` / `max_duration`.
- **Post-call classification (the design's Post-Call Processor):** at
  finalize, one non-streamed LLM call whose context is the agent's own
  prompt (its DISPOSITIONS are the only allowed buckets) + the real
  transcript returns disposition + captured fields + confidence as
  JSON. Captured fields are filtered/coerced to the agent's declared
  schema. An unknown bucket, unparseable output, or a failed classifier
  call stores **no** disposition: the result is flagged
  `review_required` with the reason in the summary — never an invented
  bucket.
- **Per-leg record:** sessions carry `llm_status` / `llm_detail` /
  `tts_status` / `tts_detail` beside the A7 STT fields, and results
  carry `review_required` (migration 006). The Calls page now shows a
  **Legs** column (STT · LLM · TTS pills) and a **Needs review** flag on
  flagged rows. `SARVAM_BASE_URL` / `SARVAM_TTS_MODEL` env seams added
  (`.env.example`, compose) — the same Sarvam key serves all three legs.
- **Honest degradation, three shapes:** full loop (real interleaved
  transcript + classification); brain configured but LLM leg failed →
  the A7 STT-shaped outcome stands (STT transcript, no classification)
  + a summary note naming the failure + review flag; LLM leg that never
  completed one turn before the stream ended is recorded **failed**
  at drain time, never left dressed as "active". The end-call close is
  signalled to the handler (an event raced against the receive pump) —
  closing the socket from the engine task proved unreliable under
  Starlette, so the handler ends the stream by returning, as in A6/A7.

### Verification
- `scripts/smoke_test.py`: **311 passed, 0 failed** (all A1–A7 checks
  intact — including the A7 live-transcript shape surviving beside a
  configured-but-unreachable brain — plus the new A8 section, all
  against local mocks: mock Sarvam HTTP + mock Saaras; no real
  provider called). The A8 section's scripted survey agent drives a
  full simulated call and asserts: the agent speaks first (audio
  arrives before any callee media); system prompt = the rendered
  single prompt (lead name bound, no `{{…}}` left); three LLM turns
  with accumulating history; Bulbul called once per reply sentence,
  in order, with the agent's speaker + the call language (bn-IN);
  the playAudio bytes are exactly the synthesized audio in order;
  the model's `end_call` closes the stream after the full closing;
  finalize = COMPLETED / `agent_end_call`, transcript interleaved
  [agent, customer, agent, customer, agent], disposition `ANSWERED`,
  captured `favourite_colour = blue`, all three legs active.
  Classifier honesty: unknown bucket and garbage output both land as
  empty disposition + review flag. Guards: `max_turns = 1` (no second
  reply generated or spoken; end reason `max_turns`) and
  `max_duration_s = 1` (late answer unanswered; `max_duration`).
  LLM-down instance (dead `SARVAM_BASE_URL`): STT finals still persist
  live, zero audio is ever sent, finalize records LLM + TTS failed,
  STT transcript stands, disposition empty, review flagged.
  Frontend: `vite build` clean.

### Known limitations (deliberate, for this chunk)
- **Not yet proven against the live Sarvam API** — no key exists yet
  (owner step, unchanged). The LLM/TTS clients speak the documented
  Sarvam shapes (chat-completions SSE deltas incl. `tool_calls`,
  Bulbul `audios[]` base64, `api-subscription-key` header) against
  local mocks that mirror them; first live contact may need envelope
  adjustments, isolated inside `app/conversation.py`.
- Bulbul audio is forwarded as returned (the request asks for
  8000 Hz); real-time pacing of outbound frames rides on Bulbul's
  streaming in production hardening — the mock returns whole
  sentences at once.
- A turn that hangs past the finish drain (~2.5 s) is cancelled; its
  worker thread is left to its socket timeout and only ever writes
  into a dead queue.
- Cost breakdown remains zeros in self-hosted results — per-call cost
  metering lands with the India-readiness work, as before.
- No barge-in yet: callee speech during an agent reply is transcribed
  and answered next turn, but the reply audio is never interrupted —
  that is A9, with silence handling.

### Next step
**A9 — Barge-in / silence handling:** interrupt outbound audio when
the callee starts speaking mid-reply; silence prompts per the agent's
`silence_prompt_after_s`; no-input timeouts that close honestly.

## Chunk 6 — Phase A: A7 STT Live — Sarvam Saaras in the Self-Hosted Stream (2026-10-23)

### Built
- **The STT leg inside the Plivo stream** (`backend/app/stt.py`, wired
  into the stream handler in `routers/telephony.py` — the A6 seam).
  When a self-hosted call's media stream opens, one **Saaras streaming
  session per call** is opened alongside it: Plivo `media` frames
  (base64 μ-law 8 kHz, decoded by the handler) are forwarded to Saaras
  as audio messages; media arriving while the session is still opening
  is buffered (capped ≈ 8 s of frames), never queued forever, and after
  an STT failure frames are dropped — STT can never back-pressure or
  crash the media stream.
- **Language per the agent's rule:** the session opens with the
  language `transports.resolve_language` resolves for the lead (agent
  language rule → state rule → default), normalised to the Saaras
  codes (bn-IN / hi-IN / en-IN). The language used is **recorded** on
  the session; mid-call language handling stays the LLM leg's job (A8).
- **Transcript handling:** partial transcripts update an **in-memory
  live view** per in-flight session; the Calls API overlays the current
  partial onto an IN_CALL call's transcript, marked `"partial": true`
  (display-only, never persisted). **Final segments are persisted to
  `call_sessions.transcript` as they arrive** (speaker `customer`,
  stream-relative offsets), so the transcript is visible mid-call, and
  the §8.2 finalize outcome now carries the **real accumulated
  transcript** instead of the A6 spike's empty one.
- **STT record on the session** (migration `005_a7_stt.sql`):
  `stt_status` (`""` = no STT leg · `active` · `failed` ·
  `not_configured`), `stt_language`, `stt_detail` (failure reason —
  exception class / short label only, never a provider body, never a
  secret). Exposed on `CallOut`; the **Calls page** gains an STT pill
  column (teal `STT · bn-IN` / red `STT failed` / slate `STT off`) and
  expandable rows showing the summary + transcript turns (system
  markers in red, live partials marked *(live…)*).
- **Three honest finalize shapes** (`transports.plivo_stream_outcome`,
  keyed by the recorded STT status):
  - `active` → transcript = the real finals; summary states live
    Saaras transcription + segment count, and that no speech was
    generated (reply brain is A8). Disposition stays empty — no fake
    classification.
  - `failed` → the call still finalizes **COMPLETED exactly once**;
    captured finals stand, headed by a `[TRANSCRIPT PARTIAL — Sarvam
    STT failed (…)]` marker turn; the `[SELF-HOSTED SPIKE]` label is
    kept and the summary names the failure. A stream that ends before
    Saaras ever opened is recorded failed with exactly that reason —
    never dressed up as active.
  - `not_configured` (no Sarvam key anywhere at stream time) → the
    stream behaves **exactly like the A6 spike** (same outcome, empty
    transcript) and **no Saaras connection is attempted**.
- **Configuration seam:** the tenant key comes from A4
  (`get_provider_config` — internal only); the endpoint is
  `SARVAM_WS_URL` (default `wss://api.sarvam.ai`, path
  `/speech-to-text/ws?language-code=…&model=…`) + `SARVAM_STT_MODEL`
  (default `saaras:v2.5`) — the same override pattern as the A6 base
  URLs, wired through `.env.example` and docker-compose. `websockets`
  is now a declared backend dependency (it was transitive via uvicorn).

### Verification (native, fresh SQLite DBs — same Docker caveat as chunks 1–5)
- `scripts/smoke_test.py`: **229/229 checks passed** (183 existing +
  46 new in the A7 section). No real Sarvam call anywhere: the A7
  section points `SARVAM_WS_URL` at a **local mock Saaras WebSocket
  server** (scripted partial/final replies) for the live path, a dead
  port for the down path, and drives real Plivo-stream WebSocket
  clients against app instances booted for the purpose. Covered:
  dispatch places the call; stream opens → mock saw **exactly one**
  Saaras connection for **bn-IN** (West Bengal lead) carrying the
  tenant's key header; the 3 media frames arrived at the mock as audio
  messages; the partial appeared on the live view mid-call (marked
  partial) with `stt_status=active` + `bn-IN` on the session; final 1
  persisted **while the call was still in flight**, final 2 appended
  in order; finalize carried the real transcript (2 customer turns) and
  an honest summary; a late Plivo status callback could not rewrite it.
  STT-down (Hindi lead): failure recorded (`failed` + reason + `hi-IN`),
  call COMPLETED once, transcript headed by the PARTIAL marker. No
  config at stream time (tenant key deleted after placement): the A6
  spike outcome verbatim in kind, `stt_status=not_configured`, and the
  mock saw **no** new connection. The A6 stream regression (fake-key
  instance, no `SARVAM_WS_URL`) still passes — its STT attempt fails
  against the unreachable real endpoint and lands in the honest
  failed/partial shape, which keeps the spike label the A6 check
  asserts. Frontend: `vite build` clean.

### Known limitations (deliberate, for this chunk)
- **Not yet proven against the live Sarvam API** — no key exists yet
  (owner step). The client speaks the documented Saaras streaming
  envelope (audio JSON messages, `api-subscription-key` header,
  `language-code` + `model` query params, `{type: data, data:
  {transcript, is_final}}` replies); exact codec negotiation for
  Plivo's μ-law frames against the live API is the first thing the
  live spike validates, and it is isolated inside `app/stt.py`.
- Partials are in-memory only (by design); a backend restart mid-call
  loses the current partial, never the persisted finals.
- STT cost is still reported as 0 in the result's cost breakdown —
  per-call cost metering lands with the India-readiness work, as
  before.
- The status-callback finalize path (`plivo_terminal_outcome`) is
  unchanged: a call that ends by hangup callback without a stream
  keeps the A6 spike outcome and an empty `stt_status`.

### Next step
**A8 — Full RPI loop:** the LLM leg (agent's single prompt → Sarvam
LLM, streaming) + Bulbul TTS leg join the stream beside STT, driven by
a generic scripted test agent: greeting on answer, reply per final
transcript, barge-in/silence behaviour per A9, disposition + captured
fields from the prompt's own definitions at call end.

## Chunk 5 — Phase A: A6 Telephony Spike + Vapi Voice Profiles (2026-10-23)

### Built
- **Real transports behind the A5 boundary** (`backend/app/transports.py`,
  registered in `orchestrator.TRANSPORTS`). The transport contract gains
  one honest state: `run()` returns an outcome dict (finalize now — the
  stub, or a placement the provider rejected) or **None** (a real call was
  placed; the attempt stays DIALING and its result lands later through the
  same `finalize_attempt` path). The dispatch report counts `placed`
  separately and says results land via webhook / stream.
- **Mode gating tightened:** a real transport runs only when
  `TELEPHONY_MODE` **equals** the campaign's mode AND A4 reports the
  tenant's provider ready. `TELEPHONY_MODE=stub` (default) still forces
  the stub even with every key configured, and a mode mismatch (managed
  platform, self-hosted campaign, Plivo ready) also falls back to the
  stub — both proven by smoke tests that assert the fake provider saw
  **zero** requests.
- **Managed transport — Vapi** (`VapiTransport`): builds the start-call
  payload from the agent's **single prompt verbatim** (assistant system
  message; Vapi substitutes `{{lead_name}}` / `{{bank_name}}` etc. from
  `assistantOverrides.variableValues` = the lead's variables incl. the
  product identifier), the tenant's Vapi defaults (`default_model` is the
  assistant **LLM** model — "name" or "provider/name"; platform fallback
  openai/gpt-4o), and an `analysisPlan` whose structured-data schema asks
  for exactly the disposition bucket set parsed from that same prompt +
  the agent's captured fields. `serverUrl` points at our webhook with the
  §8.1 shared secret as `serverUrlSecret`; `metadata` binds the call to
  session / attempt / tenant. POSTs to `{VAPI_BASE_URL}/call/phone`;
  success → session DIALING with the Vapi call id, nothing finalized.
- **Vapi end-of-call webhook** (`POST /api/telephony/vapi/webhook`,
  `routers/telephony.py`): auth = `x-vapi-secret` header vs
  `WEBHOOK_SECRET` (fails closed: unset → 503, wrong → 401). Only
  `end-of-call-report` finalizes; the report is normalised into §8.2 by
  `transports.map_vapi_report` (endedReason → final_state/end_reason,
  structuredData → disposition + captured fields, startedAt/endedAt →
  duration, artifact → recording + transcript, costBreakdown → cost with
  `mode: "managed"`) and landed via `receive_result` → `finalize_attempt`
  — duplicate deliveries return `created: false` and change nothing.
  Binding falls back to the provider call id when metadata is absent.
- **Self-hosted transport — Plivo skeleton** (`PlivoTransport`): creates
  the outbound call via Plivo REST (basic auth, form body) with our
  **answer URL** and **status callback URL** (both session-bound) and
  recording on. `GET|POST /api/telephony/plivo/answer/{session_id}`
  returns Plivo XML whose `<Stream bidirectional="true">` opens the
  media stream to `…/api/telephony/plivo/stream/{session_id}` (session id
  in the path AND as a stream `<Parameter>`). The **stream endpoint is
  the A7 seam**: it consumes start/media/stop events, marks IN_CALL on
  start, speaks nothing, and on close finalizes the attempt with the
  spike outcome (`[SELF-HOSTED SPIKE] … no speech was generated or
  heard`, disposition empty — no fake conversation).
  `POST …/plivo/status/{session_id}` maps Plivo CallStatus (ringing /
  in-progress → session state; completed / no-answer / busy / cancel /
  machine → terminal §8.2 outcomes) and verifies **Plivo Signature V2**
  whenever the signature header is present (§10). Whichever of stream /
  status lands first finalizes; the other is an idempotent no-op. A
  tenant whose self-hosted readiness is Exotel-only is honestly FAILED
  (the A6 spike ships Plivo only).
- **Vapi voice profiles (owner scope addition)** — the Vapi provider
  settings gain a structured **Voice profiles** section: per language
  (English / Hindi / Bengali) `voice_provider`, `tts_model`,
  `voice_id_female`, `voice_id_male` — plain identifier fields (not
  secrets), stored like every other Vapi field and editable in
  Settings → Providers (grouped under per-language subheadings).
  **The defaults are the owner's existing, verified RPI voice setup** —
  English: 11labs, eleven_turbo_v2_5, ♀ 90ipbRoKi4CpHXvKVtl0 /
  ♂ pzxut4zZz4GImZNlqQ3H; Hindi: 11labs, eleven_v3,
  ♀ 1qEiC6qsybMkmnNdVMbK / ♂ zT03pEAEi0VHKciJODfn; Bengali: 11labs,
  eleven_v3, ♀ 611e0Wo6wEi3GcpkO5it / ♂ GuSivqtqi1qfWxEuGhqa — shipped as
  registry defaults (resolution order tenant → .env → registry default)
  and **tenant-editable**: a tenant override wins everywhere. Registry
  defaults deliberately do NOT count toward "configured" status — a
  fresh tenant still honestly reports Vapi as stub until a key exists.
  `default_model` stays the assistant **LLM** model, distinct from the
  per-language **TTS** model. The transport selects the profile by the
  call's resolved language (agent language rule applied to the lead) and
  the **agent's voice gender** — a new structured agent field
  `voice_gender` (female | male, default female; migration
  `004_a6_voice_gender.sql`, editor select in the Voice & model section);
  languages without a profile fall back to the legacy default voice.
- **Config seams:** `VAPI_BASE_URL` / `PLIVO_BASE_URL` (provider API
  bases — how tests point at a fake provider), `PUBLIC_BASE_URL` (builds
  all provider-facing URLs), `WEBHOOK_SECRET`, `VAPI_PHONE_NUMBER_ID`
  (tenant Vapi field; sent as `phoneNumberId` when set), per-language
  profile env overrides — all wired through `.env.example` and
  docker-compose. Calls views now report the real transport for
  in-flight sessions (mode read from the session, not assumed stub);
  UI labels de-stubbed (Dispatch button, Calls page mode/transport
  indicator + footnote).

### Verification (native, fresh SQLite DBs — same Docker caveat as chunks 1–4)
- `scripts/smoke_test.py`: **183/183 checks passed** (102 existing + 81
  new A6). The A6 section boots its own app instances whose provider
  base URLs point at a local fake provider server (stdlib HTTP, in the
  test process) — **no real provider was called**. Covered: voice
  profile defaults pre-filled with source `default` and the owner's
  exact IDs; a tenant edit overriding the Bengali male ID sticks
  (source `tenant`); managed dispatch places 2 calls (`placed: 2,
  finalized: 0`) with payload assertions — prompt verbatim, LLM from
  tenant default, Bengali lead → Bengali female voice + eleven_v3,
  Hindi lead → Hindi female voice, variables, transcriber language
  (bn / hi), analysis schema = prompt buckets + captured fields,
  serverUrl/secret/metadata binding, Bearer key; sessions sit DIALING
  under transport `managed`; webhook wrong-secret → 401, non-result
  message acknowledged unhandled, report → `created: true` with full
  §8.2 mapping (COMPLETED / RPC_CONFIRMED / identity_confirmed / 47 s /
  recording / transcript / cost 0.11 USD), duplicate → `created: false`
  and the stored result untouched, metadata-less report bound by
  provider call id (customer-did-not-answer → NO_ANSWER); a male agent
  + Bengali lead → the tenant-edited Bengali **male** voice ID in the
  payload; Plivo dispatch places 2 calls (answer/status URLs
  session-bound, `record=true`, basic auth), sessions DIALING under
  `self_hosted`; answer XML parses with `bidirectional="true"`, the
  session stream URL and Parameter; a real websocket client drives
  start/media/stop → the attempt finalizes exactly once with the spike
  summary and empty disposition; a `no-answer` status callback finalizes
  the second call, a later `completed` callback cannot rewrite it, and
  a bogus Plivo signature → 403; honesty gates: stub mode with all keys
  → stub transport + **zero** fake-provider requests, and the
  mode-mismatch gate behaves the same with `provider_ready: true`.
- Frontend: `vite build` clean (Settings grouping, AgentEditor gender
  select, Calls / CampaignDetail label updates compiled in).

### Known limitations (deliberate, for this chunk)
- **No live call has been placed.** The spike is proven against a fake
  provider only. What a live run still needs from the owner: (1) a Plivo
  account (India entity + KYC) and a Plivo number placed in the tenant's
  `from_numbers`; (2) a Sarvam API key (A7); (3) `PUBLIC_BASE_URL` set
  to a publicly reachable HTTPS address for this app (providers cannot
  call localhost) and `WEBHOOK_SECRET` set to a long random value;
  (4) for managed mode, a real Vapi API key in the tenant settings (+ a
  Vapi phone number ID); (5) `TELEPHONY_MODE` flipped to the live mode.
- The media-stream endpoint is a placeholder by design: it proves
  place → answer → stream → finalize; the Sarvam conversation brain is
  A7 (STT) / A8 (LLM + TTS + disposition), which replaces the inside of
  the stream handler without changing any boundary.
- Plivo signature verification is enforced when the signature header is
  present; unsigned callbacks are still accepted in this build (Plivo
  always signs in production — tightening to required is a hardening
  step for the India-readiness pass). Status-callback URLs carry no
  per-session token in this chunk.
- Vapi's `analysisPlan.structuredDataSchema` shape follows Vapi's
  long-standing API; if Vapi requires `structuredDataPlan` for a given
  account version, the mapping point is isolated in
  `build_vapi_payload` / `map_vapi_report`.
- The dispatch worker is still synchronous inside the dispatch request:
  placing a real call is one HTTP round-trip per attempt — fine for the
  spike; a background worker remains an A10 concern.

### Next step
**A7 — Sarvam STT integration:** stream Plivo media into Saaras
(transcription WebSocket), assemble the live transcript on the session,
and surface partial/final turns — the first half of the conversation
brain inside the stream endpoint this chunk left as the seam.

## Chunk 4 — Phase A: A5 Orchestrator Skeleton (2026-10-23)

### Built
- **Dispatch service** (`backend/app/orchestrator.py`) — the service
  boundary the API calls and A6+ extends. For an ACTIVE campaign it walks
  the campaign's pending leads (`status == "pending"`, under the retry
  cap) and creates one `CallSession` per attempt, keyed by a
  **deterministic attempt_id** (`att_<campaign>_<lead>_<n>` — the §8.1
  idempotency key): re-dispatching the same attempt can never create a
  second session (the existing lead-status flow already advances leads to
  `attempted`; a pre-check on the unique `(tenant_id, attempt_id)` covers
  the rest). Admission honours the campaign schedule: **calling window**
  (outside → zero admitted, report says why) and **daily cap** (sessions
  already started today count against it); a worker loop then drains the
  admitted queue in batches of the tenant's `concurrency_limit`. In A5
  the worker runs synchronously inside the dispatch call, behind this
  boundary, so A6+ can move it to a background loop unchanged.
- **Mode routing at the dispatch boundary** via A4's
  `get_provider_config(tenant_id, mode)`: a real transport would be used
  only when the provider is ready AND `TELEPHONY_MODE != stub` AND a
  transport is registered. A5 registers exactly ONE transport — the
  **stub** — so every dispatch is simulated and *labelled* simulated:
  sessions get `provider_call_id = stub_<session>`, results are stamped
  `cost_breakdown.mode = "stub"`, and the dispatch report carries
  `transport` / `stub` / `provider_ready` so the UI never disguises a
  stub run as a real one. **No provider API is called anywhere in A5.**
- **Stub transport** simulates the full lifecycle (QUEUED → DIALING →
  IN_CALL → COMPLETED) and produces a §8.2 outcome: the disposition is
  picked **round-robin across the bucket set parsed from the agent's own
  single prompt** (its DISPOSITIONS section — the single source of truth;
  rotation seeds from the campaign's existing result count so runs
  continue where they left off), duration is a deterministic hash of the
  attempt_id (25–124 s), captured fields are typed placeholders from the
  agent's schema (the first bucket = the confirmed outcome gets
  `true` for booleans), the transcript is a two-turn placeholder with
  every line marked `[STUB]`, and `cost_breakdown` carries the contract
  fields with stub zeros, `currency: INR`, `mode: "stub"`,
  `simulated: true`.
- **One finalize path for both modes** — `finalize_attempt()` is the only
  writer of terminal state: it stamps the session (state, end reason,
  timestamps, transcript, recording), creates the session's ONE
  `CallResult`, and advances the lead (attempt count, `attempted` /
  `exhausted`). It is **idempotent**: if a result already exists for the
  session it returns it untouched — no second result, no session
  mutation, no extra attempt count (the unique constraint on
  `call_results.session_id` is the race backstop). Managed and
  self-hosted transports in A6+ plug into this same function.
- **§8.2 result receiver** (`POST /api/results`) — the idempotent
  callback the real orchestrator / normalised managed webhook will
  deliver to: keyed on `session_id` (attempt_id accepted as fallback),
  tenant-scoped, duplicate deliveries return the stored result with
  `created: false`.
- **API + UI:** `POST /api/campaigns/{id}/dispatch` (400 unless the
  campaign is active — draft / paused / completed refuse, with the reason
  in the message); `GET /api/campaigns/{id}/calls` (the campaign's calls
  + attempt **counts by state**); `GET /api/calls` (tenant-wide, optional
  `?campaign_id=`). Call views are assembled into the §8.2 shape
  (`CallOut`: session + result merged — contract fields are present even
  before a result exists; `transport` distinguishes the stub honestly).
  Campaign page gains a **Dispatch card** with a “Run dispatch (stub)”
  action (enabled only while active) and state-count pills; a new
  read-only **Calls page** (nav enabled) lists sessions tenant-wide with
  state, disposition, duration, mode, transport and cost.

### Verification (native, fresh SQLite DB — same Docker caveat as chunks 1–3)
- `scripts/smoke_test.py`: **102/102 checks passed** (68 existing + 34 new
  A5 checks/assert groups): draft campaign dispatch → 400; active
  self-hosted campaign with 5 RPI-format leads → 5 created, 5 finalized,
  counts {COMPLETED: 5}, transport `stub` (with `provider_ready: true` —
  QGBS's A4 test keys are configured, but `TELEPHONY_MODE=stub` still
  forces the stub, exactly as routed); every call conforms to the §8.2
  field set incl. cost-breakdown keys; dispositions are exactly the
  prompt's first five buckets **in prompt order** (RPC_CONFIRMED …
  CALLBACK_REQUESTED) with `identity_confirmed` true only for
  RPC_CONFIRMED; re-dispatch creates nothing; a duplicate §8.2 delivery
  (different disposition/duration) returns `created: false` and leaves
  the stored result untouched; unknown session → 404; each lead shows
  exactly one attempt; paused and completed campaigns refuse dispatch;
  a window 2h in the future admits zero; `daily_cap: 2` admits exactly
  2 of 3 leads (third stays pending); tenant-wide list totals 7 with the
  campaign filter working; tenant 2 sees zero calls and gets 404 on
  QGBS's dispatch / calls / result delivery.
- Frontend: `vite build` clean (Calls page + dispatch card compiled in).

### Known limitations (deliberate, for this chunk)
- The worker loop is synchronous inside the dispatch request — fine for
  the stub (milliseconds); the async/background worker arrives with the
  real transports (A6+), behind the same `Dispatcher` boundary.
- Calling windows are evaluated in **UTC**; the design's times are
  market-local — localising (tenant market / campaign timezone) is an
  A10 refinement, and the dispatch message says “(UTC in this build)”.
- The stub's DIALING state collapses into IN_CALL (only QUEUED, IN_CALL
  and the terminal state persist observably); real transports will hold
  each state as provider callbacks arrive.
- `POST /api/results` currently authenticates with the operator JWT; the
  §8.1 shared-secret header (`X-Orchestrator-Key`) for service-to-service
  delivery lands with the real orchestrator in A6.
- Retries of no-answer/busy outcomes (re-queueing a lead for its next
  attempt number) belong to the outcome-routing chunk (A10); A5
  finalizes each attempt exactly once and stops.

### Next step
**A6 — Telephony spike:** the first real transport behind the dispatch
boundary — Plivo outbound call + answer/stream wiring in self-hosted
mode (and/or the Vapi start-call in managed mode), delivering its result
through the same `finalize_attempt` path; gated on real provider keys
from A4 settings, stub remains the fallback everywhere else.

## Chunk 3 — Phase A: A4 Provider Settings (2026-10-23)

### Built
- **Provider registry + per-tenant secret store** (design §7 “Provider
  settings”, §8.5; A4 acceptance). New table `provider_settings` (migration
  `003_a4_provider_settings.sql`): one row per (tenant, provider) holding
  all configured field values — identifiers and secrets — server-side only.
  Registry in `backend/app/providers.py` covers both orchestration modes:
  **managed** → Vapi (primary), Twilio (optional); **self-hosted** → Plivo
  (telephony, primary), Exotel (telephony alternative), Sarvam (STT/LLM/TTS).
- **Resolution order:** tenant value → platform `.env` fallback →
  `TELEPHONY_MODE=stub` global fallback (nothing configured = simulated).
  New env fallbacks `TWILIO_*`, `EXOTEL_*` added to `config.py`,
  `.env.example` and the compose passthrough.
- **Secrets are write-only.** Every read goes through one masked view
  (`••••` + last 4 for secret fields; plain values only for identifiers
  like Auth ID / Account SID). A blank secret on save means “keep the
  stored value” (the client can never echo a secret back — it never
  receives one). Saving changed credentials clears the stale test verdict.
  Statuses: `not_configured` (optional unset / partial / disabled) ·
  `stub` (primary provider, no credentials — calls simulated) ·
  `configured` (saved, untested) · `connected` / `failed` (last test).
- **Test connection** (`POST /api/settings/providers/{provider}/test`):
  a real, lightweight check per provider (Vapi call-list, Twilio/Plivo/
  Exotel account fetch, Sarvam minimal translate) with a 6s timeout.
  Nothing configured → an honest **stub** report and no network call.
  Failure is reported as failure — success is never faked; result messages
  carry status codes / error classes only, never secrets or raw provider
  bodies. The verdict persists on the tenant row and shows in the UI.
- **Orchestrator plumbing for A5:** internal `get_provider_config(tenant_id,
  mode)` returns readiness + raw values (server-side only — never
  serialised to an API response). Managed is ready with Vapi; self-hosted
  needs Sarvam **and** one telephony provider (Plivo or Exotel).
- **Campaign flagging:** `CampaignOut` gains `provider_configured` +
  `provider_flag`. A campaign whose mode has no working provider is still
  creatable/launchable, but carries a flag naming what's missing — shown
  as a banner on the campaign page (“calls will run in stub mode…
  Settings → Providers”).
- **Health:** `/api/health` now also reports `providers` per mode
  (booleans from the .env fallback only — health has no tenant context, so
  tenant values never appear there).
- **Frontend:** **Settings → Providers** screen in the same theme tokens —
  per-mode sections, provider cards with status badges (Not configured /
  Stub / Configured / Connected / Failed), masked key display with
  tenant/.env source tags, edit form (password fields, “leave blank to
  keep ••••1234”), enable toggle, Remove (revert to fallback), and a Test
  connection button with the honest result inline. Settings nav enabled.
- **Copy fix (folded in):** `Agents.jsx` footnote said “edits its prompt
  pack” — the superseded three-layer wording; now “edits its **single
  prompt** in one place”, per the owner's single-prompt decision.
- **Owner-testing clarifications (folded in):** (1) the Agent editor's
  Voice & model section now carries an info note — those Sarvam/Saaras/
  Bulbul settings apply in **self-hosted** mode; in managed mode the
  campaign runs on Vapi with its own models/voices, and only the agent's
  prompt + language rule carry over. (2) The Vapi provider entry gains
  tenant-default **model** and **voice** config fields (plain, non-secret;
  `.env` fallbacks `VAPI_DEFAULT_MODEL` / `VAPI_DEFAULT_VOICE`) alongside
  the API key, since managed dispatch needs them.

### Verification (native, fresh SQLite DB — same Docker caveat as chunks 1–2)
- `scripts/smoke_test.py`: **68/68 checks passed** (46 existing + 22 new
  A4 checks/assert groups): fresh tenant → primary providers `stub`,
  optional ones `not_configured`; self-hosted campaign created **with**
  the stub flag; sentinel keys saved for Vapi/Plivo/Sarvam appear **in no
  response** — not in PUT, GET, the providers list, health, or tenant 2's
  views — only `••••` + last 4; blank-secret save keeps the stored key;
  unknown field → 422, unknown provider → 404; partial Twilio config stays
  `not_configured`, DELETE reverts; Test connection with nothing
  configured → `stub` (no call made); Test connection with a deliberately
  fake Vapi key → **`failed`** (in this sandbox the check failed on
  reachability — TimeoutError; the verdict path is the same for a 401);
  the failure persists as the provider's status; tenant 2 sees none of
  QGBS's settings. Server log contains zero occurrences of the sentinels.
- `get_provider_config` exercised directly: self-hosted ready once Plivo +
  Sarvam are set, raw values present internally, source = tenant.
- Frontend: `vite build` clean.

### Known limitations (deliberate, for this chunk)
- The Sarvam check performs one minimal real translation when a key is
  present (negligible quota); all other checks are read-only account fetches.
- Secrets sit in a JSON column of the app database (the design's secret
  store at this scale). Envelope encryption / external vault is production
  hardening, noted for the pre-launch pass — the API-side contract (never
  returned, never logged) already holds.
- A test verdict persists only when a tenant settings row exists;
  .env-only configuration can be tested live but its verdict is not stored.

### Next step
**A5 — Orchestrator skeleton:** the dispatch boundary (design §8.1) that
consumes `get_provider_config`, admits attempts per schedule/concurrency,
and drives CallSession states in stub mode end-to-end.

## Chunk 2 — Phase A: A3 Campaigns & Leads (2026-10-23)

### Built
- **Campaign CRUD bound to one agent** (`/api/campaigns`). Business fields
  only, per the §15 campaign-screen rules: name, product (canonical kind:
  `vehicle_loan | credit_card | home_loan | personal_loan | other`) +
  free-text product detail, purpose, audience, named **channel** (“Service
  Channel 1”…), `provider_mode` (managed | self_hosted), schedule, retry
  rules, routing rules. The campaign API schema carries **no from-number**
  and no agent-level field (no model / voice / language rule / prompt) —
  enforced by a smoke check against `/openapi.json`, not just by convention.
  The agent must belong to the caller's tenant (cross-tenant bind → 422).
  Model change: `campaigns` gains `audience`, `channel`, `product_detail`,
  `updated_at` (migration `002_a3_campaign_fields.sql`).
- **Status lifecycle:** draft → active ⇄ paused → completed (terminal),
  via `POST /api/campaigns/{id}/status`; illegal transitions → 400.
- **Leads — CSV upload with column mapping.** Two steps:
  `POST /{id}/leads/preview` (parses only: headers, row count, sample rows,
  **suggested mapping** from header aliases) and `POST /{id}/leads/upload`
  (file + confirmed mapping JSON). Targets: name / phone_number / bank_name /
  state / product_identifier / agent_field / ignore. Rows without a phone
  are invalid; a phone already in the campaign (or repeated in the file) is
  a **duplicate and skipped** — the wireframe behaviour (“858 valid · 2
  duplicates skipped”). The product identifier is stored in
  `lead.agent_fields` as `product_identifier` + `product_identifier_type`,
  typed by the campaign's product (vehicle → `registration_number`, card →
  `card_last4`, home/personal → `account_number`); all other mapped columns
  land in `agent_fields` under their normalised header names. RPI's own
  format (callee_name · phone_number · bank_name · state) and the bank
  allocation format (ACCOUNT_NO · CUSTOMER_NAME · PRINCIPAL · OUTSTANDING ·
  MOBILE_NO · RegistrationNumber) are both covered by the alias suggester.
- **Schedule + retry as configuration** (no dispatch yet — A5/A10):
  start date, calling days, calling window, daily cap; max attempts per
  lead, retry gap days, retry-at-a-different-time. Stored on the campaign
  and editable via the wizard's Schedule step / campaign PUT.
- **Frontend** (same theme tokens): **Campaigns list/history** matching
  wireframe `w11-campaigns-list.png` (status summary badges, table with
  agent / channel / status / leads, row → campaign page); the **5-step
  campaign wizard** (Agent select with the agent's needs/config panel →
  Details with gold numbered markers + “What this means” notes panel →
  Leads with expected-column chips, dropzone, mapping rows → Schedule →
  Review blocks with Edit jumps, Launch / Save-as-draft); a **campaign
  detail page** with status actions (launch / pause / resume / complete /
  delete), details + schedule panels, routing rules when present, and the
  lead table for **that campaign only** (+ add-more-leads upload). The
  Campaigns nav item is enabled. Shared upload component:
  `frontend/src/pages/LeadsUpload.jsx`.

### Verification (native, no Docker on this VM — same caveat as chunk 1)
- Backend under uvicorn on a fresh SQLite DB; `scripts/smoke_test.py`:
  **46/46 checks passed** (17 chunk-1 checks + 29 A3 checks/asserts):
  campaign schemas free of from_number/agent-level fields; create bound to
  the seeded RPI agent with product/purpose/audience/channel stored and
  `identifier_type: registration_number`; bad agent → 422, bad product →
  422; RPI CSV preview suggests the right mapping; upload of a 6-row file
  → **5 created, 1 duplicate, 0 invalid**; re-upload → 0 created, 6
  duplicates; upload without a phone mapping → 422; bank-format upload →
  identifier stored as registration_number with principal/account in
  agent_fields; credit-card campaign → `card_last4`; schedule/retry/
  routing PUT round-trips; lifecycle draft→completed with illegal moves
  → 400; **tenant isolation**: other tenant's campaign list empty,
  campaign + leads 404, upload 404, cross-tenant agent bind 422.
- Frontend: `vite build` clean; dev server served the console and proxied
  `/api/health` to the backend.

### Known limitations (deliberate, for this chunk)
- No dispatch: launching a campaign only flips its status to active; calls,
  connected counts, outcomes and the results spreadsheet arrive with
  A5/A10. Schedule/retry/routing are stored configuration until then.
- CSV upload is whole-file in memory with a per-column mapping; very large
  lists (100k+ rows) will want chunked/streamed ingest — revisit at A10.
- `product` is a fixed canonical list; a lender product outside the list
  maps to `other` (account-number identifier) until the list grows.
- The wizard infers the expected lead columns from the agent (RPI-format
  for the verification agent, bank format otherwise) — a per-agent file
  format declaration belongs with the Phase B agent builds.

### Next step
**A4 — Provider settings:** tenant provider settings + secret store +
“Test connection” for Sarvam, Plivo and the managed platform (secrets masked
on re-read, absent from all API responses).

## Chunk 1 — Phase A: A1 + A2 (2026-10-23)

### Built
- **A1 — Tenancy & core data model.** All six design entities (§7) as
  SQLAlchemy models in `backend/app/models.py`: `Tenant`, `User`,
  `Campaign`, `Lead`, `AgentConfig`, `CallSession`, `CallResult` — plus
  `PromptVersion` (see versioning below). Every API query is filtered by the
  caller's `tenant_id`; another tenant's records return 404, indistinguishable
  from missing. DDL of record generated from the models:
  `backend/migrations/001_initial.sql` (8 tables, PostgreSQL dialect); the app
  also creates tables idempotently on boot. Minimal tenant API:
  `GET /api/tenants/me`, `GET /api/tenants/{id}` (own tenant only),
  `POST /api/tenants` (provisioning: creates tenant + first admin login).
- **A2 — Agent Config Studio.** Full AgentConfig CRUD
  (`/api/agents`) + React studio: Agents library screen matching wireframe
  `w6-agents.png` (card grid: name, purpose, REFERENCE AGENT gold badge, LLM,
  STT/TTS, Captures, prompt version, “+ New agent”), and an editor covering
  basics, voice & model, language rule (default + per-state overrides),
  captured fields (name/type/description rows), the prompt, and the version
  history panel. Basic login (JWT) with the seeded demo user.
- **Seed (first boot):** tenant **QGBS**, login `demo@qgbs.example` /
  `demo-password`, one sample agent **RPI Verification** whose single prompt
  demonstrates the full model (fictitious data only).
- **Docker packaging:** `docker-compose.yml` (PostgreSQL 16 + backend +
  frontend/nginx), Dockerfiles for both services, `.env.example` with
  provider keys as placeholders; `TELEPHONY_MODE=stub` default — the app
  boots and is testable with **no real keys**.

### Owner decisions applied (override the design doc — later chunks inherit these)
1. **SINGLE PROMPT.** The design doc's three-layer prompt assembly (script
   templates + disposition rules + assembled prompt) is **superseded**. There
   is NO `script_templates` store and NO `disposition_rules` structure in the
   model or the UI. `AgentConfig.prompt` is one open-text field containing
   behaviour + dialogue/flow + the **full disposition logic**; the post-call
   classifier (A8) must take its disposition definitions from this same text —
   one source of truth. Structured config kept outside the prompt:
   `tts_voice_id`/`stt_*`, `llm_model`, `language_rule`, `captured_fields`.
2. **Prompt section structure.** Prompts follow **ROLE / FLOW / DISPOSITIONS /
   CLOSINGS**; the studio shows this as editing guidance above the prompt box.
3. **Prompt versioning.** Table `prompt_versions` (append-only). Create = v1;
   every save that changes the prompt appends a version with author + note;
   `GET …/prompt-versions`, `GET …/prompt-versions/{n}`,
   `POST …/prompt-versions/{n}/rollback` — rollback appends a NEW version
   equal to the target text, so history is reviewable and fully reversible.
4. **UI design language.** The console follows the wireframes in
   `~/workspace/voice-agent-design/`, tokenised in ONE place —
   `frontend/src/theme.css` (CSS variables): navy `#16324f` sidebar (nav:
   Dashboard / Campaigns / Agents / Calls / Journey / Human Queue / Settings,
   tenant label at the bottom), app background `#fafbfc`, white panels with
   `#c9d3de` borders and 12px radius, teal `#0e7c66` primary/active, gold
   `#c79a2e` markers/badges, navy headings, slate `#5a6b7f` secondary text.
   The 5-step campaign wizard pattern (A3+) uses the same tokens: teal step
   circles, gold field markers.

### Verification (this VM has NO Docker — noted explicitly)
Docker is not installed on the build VM, so the stack was verified
**natively** and the compose files ship untested-by-docker (first
`docker compose up` on the owner's machine is the docker-path check):
- Backend run under uvicorn with the SQLite dev fallback (compose uses
  PostgreSQL via `DATABASE_URL`; models are dialect-agnostic).
- `scripts/smoke_test.py`: **17/17 checks passed** — health (stub mode),
  401s, login, tenant fetch, seeded agent has all four prompt sections and
  `prompt_version: 1`; agent create → v1; prompt edit → v2; history [2,1];
  rollback → prompt restored, history [3,2,1] with “Rollback to v1” note;
  **A1 acceptance**: second tenant sees zero agents, QGBS agent/versions/
  tenant URLs all 404.
- Frontend: `npm install` + `vite build` clean; dev server served the app
  and proxied `/api/health` to the backend successfully.

### Known limitations (deliberate, for this chunk)
- `POST /api/tenants` is open provisioning (any caller can create a tenant);
  restrict to an agency-admin role before production.
- CORS is `*` (dev). Passwords use PBKDF2 (stdlib); JWT secret defaults to a
  dev value — set `SECRET_KEY` in `.env` for any real deployment.
- Campaigns/Leads/Calls exist as **data model only**; their APIs and screens
  are A3/A10. Sidebar items other than Agents are visibly disabled.
- No telephony yet (A5/A6): `/api/health` reports stub mode and empty
  provider configuration.

### Next step
**A3 — Campaigns & leads:** campaign CRUD bound to one agent (with product /
purpose / audience per the §15 rules), CSV lead upload with column mapping to
lead + `agent_fields`, schedule/retry/routing rules, and the 5-step campaign
wizard UI in the same theme tokens.

## Fix — Browser Test stream could not connect in the Docker deployment (2026-10-23)
Found by the owner's first real Docker test session: every screen worked (login, campaigns, dispatch), but Browser Test showed "The test stream connection failed." Root cause: the WebSocket is opened against the page origin, which in Docker is nginx — and frontend/nginx.conf proxied /api/ without the WebSocket upgrade headers (proxy_http_version 1.1 + Upgrade + Connection "upgrade"), so the handshake never reached FastAPI. Plain REST was unaffected, which is why only this one feature failed. Every prior verification exercised the WS endpoint directly (test client) or not through nginx at all; the served-through-nginx path was untested — the same class of gap as the migration fix earlier today. Fix: the three upgrade headers + proxy_read_timeout 3600s in nginx.conf, and ws:true on the vite dev proxy for parity. Verified here by config inspection + full smoke suite unchanged (the endpoint itself was never broken); the through-nginx path is verified by the owner's re-run after pull.

## Fix — provider HTTP calls rejected 403 by Vapi's edge (User-Agent), found live 2026-10-23
The owner's first live Vapi test-connection failed with "Vapi rejected the credentials (HTTP 403)" — with the original private key AND with a freshly created one. Decisive experiment (owner's terminal, same key): curl with its normal user-agent → 200; curl with `-A "Python-urllib/3.12"` → 403. The key was never the problem: every outbound provider request in the app went through bare urllib, whose default `Python-urllib/x.y` user-agent Vapi's edge rejects BEFORE auth is evaluated. All prior verification ran against local fakes, which do not UA-filter — the third same-day lesson that fakes/mocks cannot prove live-provider behaviour. Fix: a product User-Agent (`VoiceAgentPlatform/1.0`) on all three urllib call sites (providers.py probe helper, transports.py request helper, conversation.py Sarvam HTTP seam). Also: the Vapi probe now honours the documented VAPI_BASE_URL seam instead of a hardcoded URL, so the fakes can exercise it. Regression: the smoke fake's GET /call is UA-strict (403 on Python-urllib) + two checks assert the probe passes it and the recorded UA is the product UA. Suite 599/599.

## Fix bundle — Bengali calls rejected by Vapi (nova-2 has no Bengali), provider error messages preserved, transcriber made Settings-configurable (2026-10-23)
The owner's first live app-placed Vapi call failed: the Calls page showed FAILED with "Vapi rejected the call placement (HTTP 400) — no call was placed." and his phone never rang, while a test call from the Vapi dashboard worked — so number, line and balance were all fine and the fault was in our payload. A curl replay of the app's exact payload got Vapi's own verdict in writing: "assistant.transcriber.language must be one of the following values for nova-2 model: en, bg, ca, … hi … multi" — Bengali (bn) is NOT in Deepgram nova-2's language enum on Vapi. Our transport hardcoded nova-2 for every language. The same curl with nova-3 placed the call and the owner's phone rang. Three changes, one bundle:
1. **Transcriber model per language** — Bengali → deepgram/nova-3; Hindi/English stay deepgram/nova-2 (both are in the nova-2 enum).
2. **Provider error messages preserved** — the HTTP seam previously discarded error bodies, so a rejected placement recorded only "HTTP 400" (that opacity cost the debugging hour; Vapi's body named the exact field). The seam now parses error JSON, and the placement-failure summary appends the provider's own message (string or validation list, whitespace-normalised, capped at 400 chars) for both Vapi and Plivo.
3. **Transcriber is Settings-configurable per language (owner's explicit decision)** — new tenant fields {en,hi,bn}_transcriber_provider / {en,hi,bn}_transcriber_model on the Vapi card, mirroring the A6 voice profiles exactly: registry defaults (en/hi deepgram/nova-2, bn deepgram/nova-3), VAPI_<LANG>_TRANSCRIBER_PROVIDER/_MODEL .env overrides (documented in .env.example, passed through docker-compose), rendered under the same per-language headings in Settings (the UI groups by prefix, so no frontend logic changed), and the payload builder reads the resolved tenant fields with the proven defaults as fallback.
Regression coverage: the smoke fake mirrors live Vapi's rule (nova-2 + bn → 400 whose body names assistant.transcriber.language); payload assertions pin bn → nova-3 / hi → nova-2; new checks assert the transcriber defaults pre-fill (source "default"), a tenant override sticks (source "tenant"), clearing restores the defaults — and end-to-end: with the tenant's Bengali transcriber deliberately set to nova-2, a West Bengal dispatch is rejected by the fake, finalizes FAILED in-line, and the stored summary contains Vapi's message text ("transcriber.language"). Verified natively (SQLite + fake providers); the live behaviour itself was proven on the owner's machine against real Vapi earlier the same day.

## Epic C, story C1 — Database-per-tenant + Secure Box per tenant (2026-10-29)

One image, N silos (design §10, §13.3, §14). What landed:

- `backend/app/box.py` (new): box identity — BOX_TENANT_SLUG env, or
  slugified SEED_TENANT_NAME when unset, so the original single box
  needs no config change. `db_fingerprint()` names the database only
  (scheme + db name / SQLite basename), never credentials, hosts, or
  paths. `/api/health` now reports `box: {tenant_slug, database,
  telephony_mode}` (additive keys; existing health assertions
  untouched).
- `docker-compose.tenant.yml` (new): one parameterized stack —
  frontend + backend + Postgres, own volume/network per project
  name, NO published database port, backend bound to 127.0.0.1.
  The original `docker-compose.yml` is untouched.
- `scripts/provision_tenant_box.py` (new): generates one box env
  file (mode 0600) with fresh DB password, SECRET_KEY, and seed
  password (printed once); refuses to overwrite without --force;
  validates against the template when docker exists. `boxes/` is
  gitignored so generated secrets can never be pushed.
- `scripts/generate_proxy_config.py` (new): host reverse-proxy
  config from a {subdomain: frontend port} map — one subdomain to
  exactly one box, unknown hosts dropped (444), WebSocket upgrade
  headers forwarded, `--tls` mode with Let's Encrypt paths +
  HTTP->HTTPS redirect once a domain exists. Interim (no domain):
  IP:port access, proxy file generated and validated now.
- `scripts/test_tenant_isolation.py` (new): two boxes on two SQLite
  DBs over HTTP — health slug/fingerprint, cross-box invisibility
  (campaign/leads/agent/tenant all 404 across boxes), exactly one
  create_engine site in backend/app, proxy routing assertions,
  restart-A-keeps-A-data-and-B-untouched.
- `docs/multi-tenancy-runbook.md` (new): add a box, proxy cutover,
  per-tenant backup/restore, restart/rebuild rules, admin rules,
  and what C1 deliberately does not cover (C2–C7).

Isolation model, stated plainly: one process builds exactly ONE
engine bound to its own DATABASE_URL (app.db, the only
create_engine site). There is no shared app process and no code
path that opens two tenant databases in one request — isolation is
structural (separate process + database + credentials + network
per box), not a filter every query must remember.

## Epic C, story C2 — Admin catalog + adoption (2026-10-29)

Catalog/adoption (design §11). What landed:

- `backend/app/models.py`: new tables `catalog_agents`,
  `catalog_versions` (immutable by design — no update/delete
  endpoint exists), `tenant_adoptions` (current pointer, unique
  tenant+slug), `adoption_log` (append-only, no customer data).
  `AgentConfig` gains `adopted_catalog_agent` /
  `adopted_catalog_version`; `Campaign` gains `pinned_prompt` /
  `pinned_prompt_version` / `pinned_catalog_agent` /
  `pinned_catalog_version`.
- `backend/app/routers/catalog.py` (new): GET/POST
  `/api/catalog/agents`, GET `/api/catalog/agents/{slug|id}`,
  POST `/api/catalog/agents/{slug}/versions` (publish, version =
  max+1, `suitable_for` validated to managed|self_hosted|both),
  GET `.../versions/{version}/export`, POST/GET
  `/api/catalog/adoptions`, GET `/api/catalog/adoption-log`.
  Version metadata on every version: `suitable_for`, `model`,
  `validated_on`. The tenant's view adds `adopted_version`,
  `latest_version`, `new_version_available`, plus
  `unproven_on_stack` / `fit_label` from the box's own stack
  (managed|self_hosted|stub): a version is flagged when the box
  runs a real stack and `suitable_for` is neither `both` nor that
  stack; `fit_label` is "best for {stack}" when the stack token
  appears in `validated_on`, else "intended for {stack}"; the
  stub stack shows neither flag nor label (nothing is proven or
  unproven there). Write gate is `require_catalog_admin` (a stub
  for now — C5 tightens it to Super Admin).
- `backend/app/routers/campaigns.py`: campaign creation pins the
  agent's adopted version (or the edited prompt text with NULL
  catalog markers when the agent diverged from its adoption).
  PUT re-pins ONLY when the campaign is still a draft AND has no
  CallSession rows; otherwise the pinned text/version are left
  untouched. CampaignOut exposes the pinned VERSION fields, not
  the pinned text.
- `backend/app/routers/agents.py`: a PUT that changes the prompt
  text clears the agent's adopted markers (the agent no longer
  equals its adopted version); structured-only edits keep them.
- `backend/app/orchestrator.py` (`campaign_prompt()` helper),
  `transports.py` (Vapi payload system message + disposition
  enum), `conversation.py` (spoken prompt AND classifier via
  `rendered_prompt`), `routers/calls.py` (results-summary bucket
  set): every runtime read of an agent's prompt goes through the
  campaign's PINNED text when one exists, else the live prompt.
  A running campaign therefore keeps its pinned version for
  life, and a new version never auto-applies — the tenant sees
  only a "new version available" notice until it adopts.
- Adoption (same-DB or cross-box import via the export payload):
  copies prompt text + the catalog agent's full structured
  config (language rule, captured fields, voice, limits) into
  the tenant's own AgentConfig, records a PromptVersion row
  ("Adopted {slug} v{n}"), moves the TenantAdoption pointer,
  and appends an AdoptionLog row. Reuse order: current adoption
  target, then the single same-named agent, then create. The
  tenant runtime reads only its own DB after adoption.
- `backend/migrations.py` 013 + `backend/migrations/
  013_c2_catalog_pinning.sql`: marker + `_ensure_column` for the
  six new columns (all nullable); the four new tables need no
  step — `create_all` makes missing tables.
- Frontend: `Agents.jsx` catalog adoption section (adopted vs
  latest per agent, "New version available" badge, per-version
  suitable_for/model/validated_on/fit_label/unproven warning,
  Adopt button, pinned-semantics footnote); `CampaignDetail.jsx`
  "Pinned prompt" line with a "live prompt (pre-C2 campaign)"
  fallback for campaigns created before pinning.
- `scripts/test_catalog_adoption.py` (new): 64 assertions —
  empty catalog, publish v1+v2, v1 text intact, metadata shown,
  stub stack shows no flag/label, no PUT/DELETE route (404 on
  the versioned path, 405 on the collection), flag logic unit
  tests for managed/self_hosted, adopt v1, adoption log + list,
  new-version flag, campaign pins v1, publish+adopt v3 leaves
  the old campaign pinned v1, stub dispatch disposition from
  the PINNED buckets (v1 -> PINNED_ALPHA, new campaign v3 ->
  PINNED_EPSILON), manual edit clears markers so the next
  campaign pins no catalog version, export/import adoption.
  Result on a fresh SQLite backend: 64 passed, 0 failed.
- `scripts/catalog_seed_v1.json` + `scripts/seed_catalog_v1.py`
  (new): catalog v1 content = the five live agents' current
  prompts, exported read-only from the live DB 2026-10-29
  (`scripts/export_live_prompts.sql`, verified: RPI v10,
  Reminder v2, PTP v7, Settlement v8, Broken v2, live prompt ==
  latest PromptVersion text for all five). Catalog version
  numbering starts at 1 per catalog agent; the live version is
  preserved in each entry's `live_version` + `note`. Metadata
  for all five: suitable_for "managed", model "gpt-4o",
  validated_on "Vapi managed". The seeder is idempotent
  (existing slugs/versions are skipped) and takes its login
  password only from VAP_PASSWORD at exec time.

Decisions carried forward: catalog write gate stays a stub
until C5 (Super Admin); PUT re-pins only for draft campaigns
with no calls; a prompt-changing PUT clears adopted markers;
CampaignOut never exposes pinned text; the stub stack carries
no unproven flag.

Smoke-script maintenance in this story (test-only, no product
change): `scripts/smoke_test.py` counted migration versions
1-12; C2 legitimately adds migration 013, so the two
count assertions now expect 1-13. The five stale transcriber
assertions (nova-2 per-language expectations vs the settled
nova-3 + multi transcriber) are NOT touched here — they are
the parked post-demo item, still failing as before.

Follow-up fix, found by the test-box verification (not by the
local suite): the adoption markers were persisted in the
database but invisible over the API — `AgentOut` lacked the
fields AND the agents router's `_out()` builds AgentOut
field-by-field without them. Both now carry
`adopted_catalog_agent` / `adopted_catalog_version`, proven
by `scripts/verify_c2_testbox.py` (10/10, markers set on the
adopted agent). The seeder also takes its login email from
VAP_EMAIL now (default demo@qgbs.example) because a
provisioned box's seed user is admin@<slug>.example, not the
fresh-DB demo user.

## Epic C, story C5 — Roles (2026-10-29)

Roles (design §16, as decided with Surath 2026-10-29: Team
Leader is a SEPARATE login; allocation is Team Leader only;
Tenant Admin does NOT allocate — it manages Caller accounts +
tenant settings/adoption; account creation is Email OTP first,
invite-based, single-use expiring setup link, user sets own
password, SMS OTP fallback). What landed:

- `backend/app/models.py`: `User.role` now documents exactly
  four names (super_admin | tenant_admin | team_leader |
  caller, default caller); `User` gains `team_id` (FK teams)
  + `phone`; new `Team` (tenant, name, leader user, created);
  new `UserInvite` (tenant, email, name, role, team, phone,
  channel email|sms, inviter, otp_hash, setup_token_hash,
  expires_at, used_at) — ONLY hashes stored, never plaintext
  OTP/token; `Lead` gains `assigned_caller_id` /
  `allocated_by` / `allocated_at`; `CallResult` gains
  `disposition_updated_by` / `disposition_updated_at` /
  `approval_decision` / `approved_by` / `approved_at` /
  `approval_expires_at`.
- `backend/app/deps.py`: role constants + `require_super_admin`
  (strict), `require_tenant_admin` (strict — Super Admin is
  excluded because customer-data work belongs to Tenant
  Admin), `require_tenant_admin_or_super`,
  `require_team_leader` (strict), `require_catalog_admin`
  TIGHTENED to Super Admin only (the C2 seam now enforced),
  `require_catalog_reader` (super_admin or tenant_admin);
  `team_caller_ids` + `case_scope` (ALL for Tenant Admin,
  own id for Caller, team ids + unallocated pool for TL,
  403 for Super Admin).
- `backend/app/config.py`: SUPER_ADMIN_EMAIL /
  SUPER_ADMIN_PASSWORD (both empty = no Super Admin on this
  box), INVITE_DEV_OTP (default false — test gate only,
  never true in production), APPROVAL_WINDOW_HOURS
  (default 24, env-configurable).
- `backend/app/seed.py` (idempotent, runs on boot): seed user
  role "admin" -> "tenant_admin"; Super Admin bootstrap —
  only when both SUPER_ADMIN_* are set and the user does not
  exist: creates a "Platform" tenant row + super_admin user
  in it (platform scope, never a customer tenant).
- Migration 014 (`backend/app/migrations.py` +
  `backend/migrations/014_c5_roles.sql` DDL of record):
  marker `_marker_014` (users team_id+phone; leads
  assigned_caller_id+allocated_by+allocated_at; call_results
  disposition/approval audit columns) via `_ensure_column` +
  the one data migration (role 'admin' -> 'tenant_admin',
  role 'operator' -> 'caller'). New tables (teams,
  user_invites) need no step — `create_all` makes them.
- `backend/app/routers/users.py` (new): GET /api/users/me;
  GET /api/users (Super: all; Tenant Admin: own tenant; TL:
  team's Callers; Caller: 403); POST /api/users/invites
  (tenant_admin_or_super; role must be
  tenant_admin|team_leader|caller — super_admin never
  invitable, bootstrapped only; Tenant Admin invites
  Callers only; Super Admin requires tenant_id; duplicate
  email -> 409; 6-digit OTP via secrets + token_urlsafe(32)
  setup token, both PBKDF2-hashed, 24h expiry; invite
  creation is logged WITHOUT the OTP value; returns
  InviteOut with setup_url + dev_otp only when
  INVITE_DEV_OTP); GET /api/users/invites (no OTP/token in
  listings); PATCH /api/users/{id} (Super: any; Tenant
  Admin: Callers in own tenant only, name/team/is_active —
  role never changes here; cannot deactivate own account).
  Teams live under their own router (`/api/teams`): POST
  (tenant_admin_or_super; leader must already be
  team_leader in that tenant), GET (Super: all; Tenant
  Admin: own tenant; TL: teams they lead; Caller: 403),
  PATCH (tenant_admin_or_super; own-tenant teams only).
  NO direct "create user with password" endpoint exists by
  design — creation is invite-only.
- `backend/app/routers/queue.py` (new): GET /api/queue
  (Super: 403; Tenant Admin: all tenant review_required
  results; TL: team's + unassigned items; Caller: own
  only; items flag is_settlement from agent.stage ==
  "Settlement"); POST /api/queue/{result_id}/approve
  (decision approved|rejected|escalated else 422;
  Caller/Super: 403; settlement-agent items -> TL ONLY;
  TL approving outside their team -> 403; missing or
  already-resolved -> 404; past approval_expires_at -> 410
  GONE, item stays queued and overdue; approved/rejected
  clear review_required, escalated keeps it).
- `backend/app/routers/auth.py`: login UserOut now includes
  team_id/phone; new POST /api/auth/setup {invite_id,
  token, otp, password} (public; invite looked up by ID
  because PBKDF2 hashes are salted — a token-hash lookup
  can never match — then token AND otp verified with
  verify_password; failures indistinguishable 401 whether
  invite/token/OTP/expiry is wrong; sets password,
  is_active, team/phone from the invite, used_at; returns
  auto-login). The salted-hash lookup bug was found by the
  C5 suite itself (setup 401'd until the lookup changed).
- `backend/app/routers/catalog.py`: reads ->
  require_catalog_reader; adopt POST -> require_tenant_admin
  STRICT (Super Admin does not adopt — adoption records
  the adopting tenant, and Super Admin belongs to platform
  scope, not any tenant); adoptions list ->
  require_tenant_admin; adoption-log ->
  require_tenant_admin_or_super (Tenant Admin filtered to
  own tenant, Super sees all).
- `backend/app/routers/agents.py`: `_out` redacts prompt to
  "" for team_leader/caller; `_get_scoped` — Super Admin
  by id across tenants, others tenant-filtered (foreign =
  404); list — Super: all, others own tenant + redaction;
  create -> require_super_admin with body.tenant_id
  REQUIRED (422 if absent — the Super Admin's own
  tenant_id is the platform tenant, never the answer);
  get -> any role + redaction; update/delete/rollback ->
  require_super_admin; prompt-versions list/get ->
  require_tenant_admin_or_super (the old tenant filter on
  versions queries was dropped — it wrongly hid a tenant's
  versions from Super Admin; access is enforced by
  _get_scoped).
- `backend/app/routers/campaigns.py`: `_get_scoped` gains
  C5 visibility (Super -> 404; Caller -> only campaigns
  holding >=1 own allocated lead; Tenant Admin/TL -> any
  tenant campaign); list — Super: 403; Caller: only
  campaigns with own leads; TL/Tenant Admin: all tenant;
  create/update/delete/status -> require_tenant_admin;
  `_lead_out` includes allocation fields; list_leads scoped
  via case_scope; NEW POST
  `/{campaign_id}/leads/{lead_id}/allocate`
  (require_team_leader; lead in campaign+tenant; caller
  must be role=caller, same tenant, on the Leader's team
  else 422; null = unallocate; sets assigned_caller_id /
  allocated_by / allocated_at); export ->
  require_tenant_admin; preview/upload ->
  require_tenant_admin; upload mapping gains "caller"
  target (value = Caller email else exact name; email
  match wins; name match requires exactly 1, else
  unresolved; unresolved values collected once each into
  unresolved_callers; assigned leads get allocated_by =
  uploader + allocated_at = now). Deliberate distinction,
  documented: upload-time assignment honors the FILE's
  allocation data (plan step 2 / Q3), while MANUAL
  allocation is TL-only.
- `backend/app/routers/calls.py`: `_campaign_scoped` same
  C5 visibility; NEW `_scope_sessions` (Tenant Admin
  unchanged; TL: team's cases + unallocated cases; Caller:
  own cases only; lead-less browser-test sessions drop
  out of scoped views); dispatch -> require_tenant_admin;
  campaign_calls + campaign_results scoped (sessions +
  counts both scoped); dashboard — Super: 403, campaign
  counts tenant-wide for TL, own-campaigns-only for
  Caller, calls/results/review/costs scoped via session
  ids; list_calls — Super: 403, and a campaign_id filter
  returns EMPTY for a foreign campaign (lists return
  empty, not 404 — the tenant filter already excludes
  foreign sessions; the first C5 cut 404'd here and broke
  the cross-tenant fuzz, which asserts total 0);
  call_recording scope check (Super: 404; Caller: own lead
  only; TL: team's or unassigned; foreign = 404); NEW
  PATCH `/api/calls/{session_id}/disposition` (Super:
  403; Caller: own only; TL: team's; Tenant Admin: any
  tenant case; foreign = 404; no result = 404; disposition
  must be one of the campaign agent's own buckets via
  disposition_buckets(campaign_prompt(...)) else 422;
  sets disposition + disposition_updated_by/at).
  post_result (/api/results, internal receiver)
  deliberately UNCHANGED — gating it could break finalize
  flows.
- `backend/app/routers/tenants.py`: POST "" ->
  require_super_admin + invite-based admin (creates tenant
  + pending tenant_admin User + UserInvite; returns
  {tenant, admin_email, invite_id, setup_url, dev_otp only
  when INVITE_DEV_OTP} — NO password generated or
  returned; replaces the old open endpoint that returned
  admin_password "demo-password"); GET /{tenant_id} —
  Super Admin may read any tenant's metadata (not customer
  data), others own-only else 404; NEW PUT /me ->
  require_tenant_admin (timezone, from_numbers).
- `backend/app/routers/settings.py`: all 5 providers
  endpoints -> require_tenant_admin (Super Admin provider
  management is a C4 follow-up with explicit tenant
  scoping — their user.tenant_id is the platform tenant,
  so provider_view would silently address the wrong
  tenant's row).
- `backend/app/routers/browser_test.py`: create ->
  require_tenant_admin. `telephony.py` UNTOUCHED (all
  webhook/answer/stream endpoints are provider-facing, no
  get_current_user — gating them would break provider
  callbacks).
- `backend/app/main.py`: registered `users.router` +
  `users.teams_router` + `queue.router`.
- `backend/app/orchestrator.py`: approval clock stamping
  in all three finalize paths (merge-upgrade with new
  disposition; review-flag upgrade branch; fresh creation
  incl. hangup-first rule): when review_required becomes
  True and approval_expires_at is None, set now +
  APPROVAL_WINDOW_HOURS.
- `scripts/provision_tenant_box.py`: no role references —
  backend seed handles roles; no change needed.

Role-matrix test (`scripts/test_roles.py`, runner
`~/workspace/run_c5_test.sh`): 110 passed, 0 failed on a
fresh SQLite backend (Super Admin bootstrapped via env +
INVITE_DEV_OTP). Covers: seed user is tenant_admin after
migration; Super Admin catalog writes + tenant creation
(invite shape, no password) + 403/404 on all customer-data
surfaces; Tenant Admin 403 on catalog/agents/tenants writes
+ settings PUT; invite flow for Caller, Team Leader (via
Super Admin — Tenant Admin invites Callers only),
SMS-channel invite; wrong token / wrong OTP / re-use all
401; team creation + Caller moved into the TL's team; TL
lists only teams they lead; allocation TL-only (Tenant
Admin + Caller 403; TL to an off-team Caller 422; audit
fields set); Caller sees only own leads/calls; upload
caller column resolves by name, reports unknown;
dispatch + queue approvals (Tenant Admin approves the
RPI item; settlement item Tenant Admin 403, TL 200;
expired approval 410); disposition PATCH audit (own
200, other's 404, bad bucket 422); prompt redaction for
TL/Caller on agent reads; Tenant Admin still reads prompt
history.

Smoke-script maintenance in this story (test-only): boot
env gains SUPER_ADMIN_* + INVITE_DEV_OTP (exported in
`run_smoke.sh` so start_app6 instances inherit them — the
first C5 smoke run failed super login on 6-instances
because the vars were uvicorn-prefix-only); migration
count assertions 1-13 -> 1-14; tenant-creation sites (1
main + 4 six-section) use the invite flow via
create_tenant_main / create_tenant6; agent-write sites
(1 main + 12 six-section) use the Super token +
explicit tenant_id via create_agent6 (the Concurrency Co
agent resolves against tokenK, its owning tenant, not
the seed token); list_calls keeps the list contract
(empty, not 404) for foreign campaign filters. C2 suite
also updated (catalog writes + agent PUT on the Super
token; reads/adoptions stay Tenant Admin): 64 passed, 0
failed. Full smoke after C5: 791 PASS, 5 FAIL — exactly
the known stale transcriber assertions — then the known
typing-bed crash at smoke_test.py:5272 (pre-existing
test-script bug, same as C1/C2 runs). No C5 regressions.
No stray uvicorn processes after any run.

Known limitations carried forward (not silently fixed):
invite delivery is log-mode — no Email/SMS provider is
wired, so the setup link is returned to the CREATOR who
forwards it (standard "copy invite link" pattern); the
OTP travels only over its channel + the INVITE_DEV_OTP
test gate. Without a provider AND without the dev gate,
invites cannot complete in production — provider wiring
is follow-up work. Approval window is env-configurable,
not yet a settings-UI control. Frontend role-gating
(hiding editor/catalog UI per role, handling 403s) is
NOT in C5 backend scope. Super Admin provider management
is a C4 follow-up (explicit tenant scoping needed).

C5 follow-up fix, found by the test-box deploy (not by the
local suite): migration 014's DDL used `DATETIME`, which
SQLite accepts but PostgreSQL rejects (`type "datetime"
does not exist`) — the box backend crashed on boot with
`ALTER TABLE leads ADD COLUMN allocated_at DATETIME`
failing. Fixed to the existing dialect helper
(`_ts(dialect)`: TIMESTAMP WITHOUT TIME ZONE on
PostgreSQL, TIMESTAMP on SQLite) in `_apply_014`, and the
014 SQL file of record now carries the PostgreSQL form.
Re-running is safe: `_ensure_column` skips columns the
crashed run already added, and the 014 marker was never
recorded (the step raised before it). Lesson carried
forward: every new migration's DDL must be proven on the
Postgres test box, not only on SQLite — the local suite
cannot catch dialect-only type errors.

C5 follow-up fix 2, found by the test-box verify (not by
the local suite): the box backend never received
SUPER_ADMIN_* / INVITE_DEV_OTP — `docker compose
--env-file` sets compose-level variables for
interpolation, but a container only sees what the
service's `environment:` block passes. The tenant compose
template passed seed + provider vars but not the C5 vars,
so the seed bootstrap saw empty SUPER_ADMIN_* and created
no Super Admin (super login 401'd while the tenant-admin
migration itself had worked). Fixed in
`docker-compose.tenant.yml`: SUPER_ADMIN_EMAIL /
SUPER_ADMIN_PASSWORD / INVITE_DEV_OTP /
APPROVAL_WINDOW_HOURS now pass through with empty/false
defaults — empty default = no Super Admin on a normal
tenant box (Super Admin lives on the platform box; the
test box sets these to prove the role gates). The
provision script deliberately does NOT generate super
creds for tenant boxes.

C5 verify-script hardening (test-only): the test-box
verifier first expected DELETE 200 (the API correctly
returns 204) and used fixed invite emails + a fixed
throwaway catalog slug, so a second run on the same box
correctly 409'd/422'd on duplicates. Fixed to expect 204
and to suffix invite emails + the throwaway slug with a
per-run random hex — re-runs are now clean (28/28). The
duplicate-email and duplicate-slug guards firing on
re-runs is itself evidence those guards work on Postgres.

## Fix — login 401 said 'Session expired' (2026-10-29, live user test)

`frontend/src/api.js` treated EVERY 401 as an expired
session (clear + reload + throw 'Session expired') — both
in `api()` and in `apiUpload()`. A failed login therefore
told the user his session had expired when his credentials
were simply wrong for that box (backend detail 'Invalid
email or password' was discarded, and the reload wiped the
login form's error state). Fixed: 'Session expired' +
clear + reload now happens ONLY for a non-auth call that
actually carried a token. Auth endpoints (`/api/auth/*`)
and token-less calls surface the backend's own detail
instead, with no clear and no reload — so a failed login
shows 'Invalid email or password' and a failed setup
shows 'Invalid or expired invite'. Verified with a node
harness importing the real api.js (stubbed localStorage /
window / fetch): 12/12, covering failed login with no
token, failed login with a stale token lingering,
setup 401, authenticated-call 401 (still session-expired
+ clear + reload), upload 401 with token, and login 200.
