# BUILD LOG

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
