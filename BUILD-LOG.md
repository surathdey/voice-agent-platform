# BUILD LOG

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
