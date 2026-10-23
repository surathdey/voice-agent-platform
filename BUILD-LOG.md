# BUILD LOG

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
