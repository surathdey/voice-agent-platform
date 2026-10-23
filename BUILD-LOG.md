# BUILD LOG

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
