"""Conversation brain (A8) — the LLM → TTS legs beside the A7 STT leg.

Design §8.4 + §11 row A8: one Sarvam LLM request per conversation turn
(full history + the agent's prompt each time), one Bulbul synthesis per
reply sentence, audio forwarded to the call as it arrives. This module
is those two legs plus the turn loop that binds them to the STT leg
(app.stt) inside the Plivo media stream (routers/telephony.py):

- `render_prompt()` — the agent's SINGLE prompt with the lead variables
  bound ({{lead_name}}, {{bank_name}}, … — the same binding the managed
  Vapi leg performs via assistantOverrides.variableValues). The stored
  prompt itself is never mutated, and nothing is assembled from parts:
  the one text IS the system prompt.
- `stream_chat()` — Sarvam chat completions, streamed (HTTPS POST per
  turn, api-subscription-key auth). Yields reply tokens through a
  callback; returns (full_text, tool_name). The one tool offered is
  `end_call` — the design's end-call action: when the model calls it
  (its FLOW reached a closing), the engine speaks the turn's text, then
  ends the call (end_reason agent_end_call).
- `synthesize_tts()` — Bulbul TTS, one request per completed reply
  sentence; the returned audio is framed back into the Plivo stream as
  playAudio media frames by the engine.
- `ConversationEngine` — the per-call loop: kickoff turn at stream start
  (the agent speaks first, following its prompt's FLOW), then one reply
  turn per STT final, until the callee hangs up / the stream stops, the
  model calls end_call, or a guard hits — the agent's max_duration_s /
  max_turns limits, ending the call honestly (end_reason recorded).
- `classify_call()` — the Post-Call Processor (design §9): a second LLM
  call that classifies the finished transcript against the disposition
  definitions IN THE AGENT'S OWN PROMPT (the classifier's context is
  that same single text — one source of truth) and extracts the agent's
  captured fields as structured JSON. The disposition is validated
  against the prompt's bucket set: an unknown bucket is NEVER stored —
  the result gets disposition "" and review_required = True instead.
  A failed / unparseable classification degrades the same honest way.

Leg honesty, same pattern as A6/A7: each leg records
active | failed | not_configured on the session (llm_status / tts_status
beside stt_status) with a detail made of an exception class / short
label only — never a provider body, never a secret. The legs exist only
when the tenant's Sarvam key is configured (get_provider_config via
app.stt.stt_config — one Sarvam key serves STT / LLM / TTS, §8.4); with
no key the engine is never created and the stream behaves exactly like
A7. LLM/TTS HTTP goes to SARVAM_BASE_URL — the override seam the smoke
suite points at a local mock Sarvam server; no real provider is called
in tests.

A9 — barge-in + silence (design §5.2), inside this same loop:

- **Barge-in.** The STT leg reports every non-empty partial through
  `note_speech_activity()`. While a reply is being spoken, that signal
  IS an interruption: the engine stops pumping audio frames for the
  current reply at the next frame boundary, abandons the reply's
  unsent sentences (they are never synthesised or spoken), asks the
  transport to flush its buffered audio (the `clear_audio` callback —
  Plivo's clearAudio message, best-effort; the pump stop is the
  authoritative cut, and any audio the provider had already played
  out cannot be recalled), and records the agent turn with ONLY the
  words actually spoken, flagged `interrupted`. The model's end_call
  action in an interrupted turn is discarded — the customer took the
  floor, and their utterance (already queued by the STT leg) becomes
  the next turn's input. Interruption is a first-class event, never
  an error.
- **Silence.** While the engine waits for the customer, a silence
  deadline runs: `silence_prompt_after_s` past the later of the last
  agent turn's end and the last STT speech activity (partials count —
  a customer mid-utterance is never nudged). On a trip, the engine
  speaks a nudge (an LLM turn carrying a bracketed silence context,
  the same device as the kickoff); a customer answer resets the
  counter. Once `silence_max_prompts` nudges have gone unanswered,
  the next silence speaks the polite closing (an LLM turn carrying
  the closing context, so the wording is the prompt's own CLOSINGS)
  and the call ends with end_reason `no_input` — named ahead of the
  model's own end_call in that turn, because silence is the cause.
  The max_turns / max_duration guards still outrank the close when
  they are what actually fires.

Threading: the LLM stream and the TTS requests are blocking HTTP, so a
turn's producer runs in a worker thread and hands tokens to the event
loop through a queue; TTS runs via asyncio.to_thread per sentence. The
engine therefore never blocks the media stream's receive loop.
"""
import asyncio
import base64
import json
import re
import time
import urllib.error
import urllib.request

from . import stt as stt_module
from .config import settings

# The user-role kickoff that starts every call: the model generates the
# opening by following its own prompt's FLOW (recorded disclosure,
# greeting, first question) — no greeting text lives in code.
KICKOFF = ("[The call has just connected. Begin the conversation now, "
           "following your FLOW — speak your opening line.]")

# The two silence contexts (A9, design §5.2) — bracketed engine notes in
# the same style as KICKOFF: the nudge/closing WORDING stays the
# model's own, from the agent's prompt; these only name the situation.
SILENCE_NUDGE = ("[The customer has been silent since your last line. "
                 "Prompt them once — briefly and politely, following "
                 "your instructions.]")
SILENCE_CLOSING = ("[The customer has not responded after repeated "
                   "prompts. Close the call politely now, following "
                   "your CLOSINGS, and end the call.]")

END_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "end_call",
        "description": ("End the call. Call this when the conversation has "
                        "reached a closing per your instructions and your "
                        "closing line has been said."),
        "parameters": {"type": "object", "properties": {}},
    },
}

_CLASSIFIER_SYSTEM = (
    "POST-CALL CLASSIFIER. You classify ONE finished phone call and return "
    "structured data. You are given the agent's full prompt — its "
    "DISPOSITIONS section defines the ONLY allowed disposition buckets and "
    "what each means — followed by the call transcript. Decide the single "
    "disposition that matches the conversation, extract the agent's "
    "captured fields from what was actually said, and answer with ONLY a "
    "JSON object of this shape: {\"disposition\": \"<one bucket name>\", "
    "\"captured_fields\": {\"<field>\": <value>}, \"confidence\": <0..1>, "
    "\"summary\": \"<one or two sentences>\"}. No other text."
)

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_SENTENCE_RE = re.compile(r"[.!?।…]\s+")
_MAX_SENTENCE_CHARS = 400  # force-split an over-long run-on reply


def render_prompt(prompt: str, variables: dict) -> str:
    """The agent's single prompt with lead variables bound — the same
    {{variable}} binding the managed leg gets from Vapi. Unknown
    placeholders are left as written; the stored prompt is untouched."""
    def _sub(m):
        value = variables.get(m.group(1))
        return str(value) if value is not None else m.group(0)
    return _VAR_RE.sub(_sub, prompt or "")


def bulbul_speaker(tts_voice_id: str, language: str, voice_gender: str) -> str:
    """The Bulbul speaker for one call: the agent's configured voice id
    (tts_voice_id IS the speaker handed to Bulbul); when unset, a
    platform voice id derived from the call language + the agent's
    voice gender, matching the seeded naming convention."""
    if (tts_voice_id or "").strip():
        return tts_voice_id.strip()
    lang = (language or "hi-IN").split("-")[0].lower() or "hi"
    gender = (voice_gender or "female").strip().lower()
    if gender not in ("female", "male"):
        gender = "female"
    return f"bulbul-{lang}-{gender}"


# ── Sarvam HTTP seam (LLM + TTS) ─────────────────────────────────────────

def _sarvam_request(path: str, api_key: str, payload: dict,
                    timeout: int = 20):
    req = urllib.request.Request(
        f"{settings.SARVAM_BASE_URL.rstrip('/')}{path}",
        method="POST", data=json.dumps(payload).encode(),
        headers={"api-subscription-key": api_key,
                 "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def stream_chat(api_key: str, model: str, messages: list, on_token) -> tuple[str, str]:
    """One streamed chat completion. on_token(text) is invoked per
    content delta (from the calling thread); returns (full_text,
    tool_name) where tool_name is "end_call" when the model called it.
    Raises on transport / HTTP errors — the caller records the leg."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "tools": [END_CALL_TOOL],
        "tool_choice": "auto",
    }
    full: list[str] = []
    tool_name = ""
    with _sarvam_request("/chat/completions", api_key, payload) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                full.append(content)
                on_token(content)
            for tc in delta.get("tool_calls") or []:
                name = ((tc.get("function") or {}).get("name") or "")
                if name:
                    tool_name = name
    return "".join(full), tool_name


def chat_once(api_key: str, model: str, messages: list,
              timeout: int = 30) -> str:
    """One non-streamed chat completion; returns the message content.
    Raises on transport / HTTP errors."""
    payload = {"model": model, "messages": messages, "stream": False}
    with _sarvam_request("/chat/completions", api_key, payload,
                         timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    choices = body.get("choices") or []
    if not choices:
        raise ValueError("empty_completion")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        raise ValueError("empty_completion")
    return content


def synthesize_tts(api_key: str, text: str, language: str,
                   speaker: str) -> bytes:
    """One Bulbul synthesis (design §8.4: one per reply sentence).
    Returns the decoded audio bytes. Raises on transport / HTTP errors."""
    payload = {
        "inputs": [text],
        "target_language_code": language,
        "speaker": speaker,
        "model": settings.SARVAM_TTS_MODEL,
        "speech_sample_rate": 8000,
    }
    with _sarvam_request("/text-to-speech", api_key, payload) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    audios = body.get("audios") or []
    if not audios:
        raise ValueError("empty_tts_audio")
    return base64.b64decode(audios[0])


# ── Sentence chunking (reply stream → TTS-sized pieces) ─────────────────

class SentenceChunker:
    """Accumulates streamed reply tokens and emits each completed
    sentence the moment its terminator + following whitespace arrives,
    so TTS starts on sentence 1 while the LLM is still writing
    sentence 2. flush() emits whatever tail remains at stream end."""

    def __init__(self):
        self._buf = ""

    def feed(self, token: str) -> list[str]:
        self._buf += token
        out: list[str] = []
        while True:
            m = _SENTENCE_RE.search(self._buf)
            if m:
                cut = m.end()
            elif len(self._buf) > _MAX_SENTENCE_CHARS:
                cut = self._buf.rfind(" ", 0, _MAX_SENTENCE_CHARS)
                if cut <= 0:
                    break
                cut += 1
            else:
                break
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> list[str]:
        tail = self._buf.strip()
        self._buf = ""
        return [tail] if tail else []


# ── Post-call classification (Post-Call Processor, design §9) ───────────

def _coerce(value, ftype: str):
    if ftype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)
    if ftype == "number":
        try:
            num = float(value)
            return int(num) if num == int(num) else num
        except (TypeError, ValueError):
            return None
    if value is None:
        return None
    return str(value)


def classify_call(api_key: str, model: str, prompt: str,
                  captured_fields: list, turns: list) -> dict:
    """Classify one finished call against the agent's OWN prompt.

    Returns {disposition, captured_fields, confidence, summary,
    review_required, review_reason}. Every failure — the classifier
    call itself, unparseable output, a bucket outside the prompt's set
    — degrades to disposition "" + review_required True with the reason
    named; an untrusted bucket is never stored.
    """
    from .orchestrator import disposition_buckets  # deferred: import cycle

    failed = {"disposition": "", "captured_fields": {}, "confidence": 0.0,
              "summary": "", "review_required": True, "review_reason": ""}
    buckets = disposition_buckets(prompt)
    field_lines = "\n".join(
        f"- {f.get('name')} ({f.get('type', 'string')}): {f.get('description', '')}"
        for f in (captured_fields or []) if f.get("name")) or "(none)"
    transcript = "\n".join(
        f"{'Agent' if t.get('speaker') == 'agent' else 'Customer'}: {t.get('text', '')}"
        for t in (turns or []) if t.get("speaker") in ("agent", "customer"))
    messages = [
        {"role": "system", "content": _CLASSIFIER_SYSTEM},
        {"role": "user", "content":
            "AGENT PROMPT (the disposition definitions live in its "
            "DISPOSITIONS section):\n" + (prompt or "") +
            "\n\nCAPTURED FIELDS TO EXTRACT:\n" + field_lines +
            "\n\nTRANSCRIPT:\n" + (transcript or "(no speech was captured)")},
    ]
    try:
        content = chat_once(api_key, model, messages)
    except Exception as e:
        return {**failed,
                "review_reason": f"classifier call failed ({type(e).__name__})"}
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not_an_object")
    except Exception:
        return {**failed, "review_reason": "classifier output was not valid JSON"}
    disposition = data.get("disposition") or ""
    if disposition not in buckets:
        return {**failed,
                "review_reason": ("classifier returned a disposition outside "
                                  "the prompt's bucket set")}
    declared = {f.get("name"): f.get("type", "string")
                for f in (captured_fields or []) if f.get("name")}
    captured: dict = {}
    for name, value in (data.get("captured_fields") or {}).items():
        if name in declared:
            coerced = _coerce(value, declared[name])
            if coerced is not None:
                captured[name] = coerced
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "disposition": disposition,
        "captured_fields": captured,
        "confidence": min(1.0, max(0.0, confidence)),
        "summary": data.get("summary") or "",
        "review_required": False,
        "review_reason": "",
    }


# ── The conversation outcome (§8.2, self-hosted full loop) ──────────────

def conversation_outcome(duration_s: int, turns: list, end_reason: str,
                         classification: dict, stt_status: str = "",
                         stt_detail: str = "",
                         llm_status: str = "", tts_status: str = "") -> dict:
    """The §8.2 outcome for a call the full loop carried: the REAL
    interleaved transcript, the classified disposition + captured
    fields, and a summary that names what actually ran — including any
    leg that failed and any review flag. Mirrors the partial-transcript
    honesty of transports.plivo_stream_outcome when STT failed."""
    from .transports import _zero_cost  # deferred: import cycle

    out_turns = list(turns or [])
    if stt_status == "failed":
        reason = stt_detail or "unknown error"
        out_turns = [{
            "speaker": "system",
            "text": (f"[TRANSCRIPT PARTIAL — Sarvam STT failed ({reason}). "
                     "Only the segments captured before the failure are "
                     "shown.]"),
            "offset_s": 0.0,
        }] + out_turns
    agent_turns = sum(1 for t in out_turns if t.get("speaker") == "agent")
    notes = []
    if stt_status == "failed":
        notes.append(f"STT leg failed ({stt_detail or 'unknown error'}) — "
                     "the transcript is partial")
    if tts_status == "failed":
        notes.append("TTS leg failed — the callee may not have heard every reply")
    interruptions = sum(1 for t in out_turns if t.get("interrupted"))
    if interruptions:
        notes.append(
            f"the customer interrupted the agent {interruptions} time(s) — "
            "interrupted replies are recorded with the words actually spoken")
    if end_reason == "no_input":
        notes.append("the call ended after repeated customer silence "
                     "(no input), following the polite closing")
    summary = (
        "[SELF-HOSTED] Full conversation loop — Sarvam Saaras STT → "
        "Sarvam LLM → Bulbul TTS. "
        f"{agent_turns} agent turn(s) spoken; the transcript is the real "
        "conversation. "
    )
    if classification.get("summary"):
        summary += classification["summary"] + " "
    if classification.get("disposition"):
        summary += (f"Disposition {classification['disposition']} — classified "
                    "against the DISPOSITIONS in the agent's own prompt. ")
    if notes:
        summary += "Note: " + "; ".join(notes) + ". "
    if classification.get("review_required"):
        summary += (f"Flagged for review: "
                    f"{classification.get('review_reason') or 'classification unavailable'}.")
    return {
        "final_state": "COMPLETED",
        "end_reason": end_reason or "stream_closed",
        "disposition": classification.get("disposition") or "",
        "confidence": classification.get("confidence") or 0.0,
        "summary": summary.strip(),
        "captured_fields": classification.get("captured_fields") or {},
        "duration_s": int(duration_s or 0),
        "recording_url": "",
        "transcript": out_turns,
        "cost_breakdown": _zero_cost("self_hosted", "INR"),
        "review_required": bool(classification.get("review_required")),
    }


# ── The per-call conversation engine ────────────────────────────────────

class ConversationEngine:
    """The turn loop for one self-hosted call (see module doc).

    Constructed by the stream handler when the tenant's Sarvam key is
    configured; start() runs the kickoff turn, STT finals are fed via
    enqueue_final(), finish() drains at stream end. All agent config is
    snapshotted to plain values up front — worker threads never touch
    ORM objects.
    """

    def __init__(self, *, session_id: str, agent, campaign, lead,
                 language: str, api_key: str, send_audio, close_stream,
                 clear_audio=None):
        from .transports import lead_variables

        self.session_id = session_id
        self.language = language
        self._api_key = api_key
        self._send_audio = send_audio      # async: bytes -> one playAudio frame set
        self._close_stream = close_stream  # async: ends the Plivo stream (end-call)
        # A9: async flush of the provider's buffered outbound audio
        # (Plivo clearAudio) on barge-in; None when the transport has
        # no such message — stopping our own frame pump is the cut.
        self._clear_audio = clear_audio
        self._prompt = render_prompt(agent.prompt or "",
                                     lead_variables(campaign, lead))
        self._model = agent.llm_model or "sarvam-105b"
        self._speaker = bulbul_speaker(agent.tts_voice_id or "", language,
                                       agent.voice_gender or "female")
        self._max_duration_s = int(agent.max_duration_s or 300)
        self._max_turns = int(getattr(agent, "max_turns", 50) or 50)
        self._silence_after_s = int(
            getattr(agent, "silence_prompt_after_s", 8) or 8)
        _smp = getattr(agent, "silence_max_prompts", 1)
        self._silence_max_prompts = 1 if _smp is None else int(_smp)
        self.captured_fields = list(agent.captured_fields or [])

        self.turns: list[dict] = []        # the conversation, in order
        self.messages: list[dict] = [{"role": "system", "content": self._prompt}]
        # Legs start "active" = armed and serving (the engine exists only
        # when Sarvam is configured); the first error flips a leg to
        # "failed" with its detail, and it stays failed.
        self.llm_status = "active"
        self.llm_detail = ""
        self.tts_status = "active"
        self.tts_detail = ""
        # A12 cost metering: μ-law frames of synthesized audio actually
        # pumped into the stream, accumulated by _speak (a sentence cut
        # by barge-in contributes only the frames that left). 160-byte
        # frames at 8 kHz = 20 ms each — app.costing turns this into
        # the TTS leg's cost at finalize.
        self._tts_frames = 0
        self.end_reason = ""               # agent_end_call | max_turns | max_duration | no_input
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._t0: float | None = None
        self._agent_turns = 0
        self._ended = False
        self._send_dead = False
        # A9 state: playback + interruption + silence bookkeeping.
        self._speaking = False             # a reply turn is being spoken
        self._interrupt = asyncio.Event()  # set by barge-in mid-reply
        self._interruptions = 0
        self._idle_since = 0.0             # when the current wait began
        self._last_speech_at = 0.0         # last STT partial/final stamp
        self._silence_prompts_used = 0     # consecutive unanswered nudges

    # ── surface for the stream handler ──────────────────────────────
    @property
    def rendered_prompt(self) -> str:
        return self._prompt

    @property
    def model(self) -> str:
        return self._model

    def legs(self) -> dict:
        return {"llm_status": self.llm_status, "llm_detail": self.llm_detail,
                "tts_status": self.tts_status, "tts_detail": self.tts_detail}

    def metrics(self) -> dict:
        """A12 cost inputs for finalize: the synthesized-audio frames
        actually pumped across every sentence of this call."""
        return {"tts_frames": self._tts_frames}

    def enqueue_final(self, turn: dict) -> None:
        """STT leg → engine: one final customer segment. Runs on the
        event loop (the STT session's receive task); finals arriving
        after the engine ended are transcript-only and are drained into
        self.turns by finish(). A final is speech activity too — it
        stamps the silence clock (A9)."""
        self._last_speech_at = time.monotonic()
        if not self._ended:
            self._inbox.put_nowait(turn)

    def note_speech_activity(self, _partial_text: str = "") -> None:
        """STT leg → engine: the callee is speaking (a non-empty
        partial just landed, A9 — the session calls this with the
        partial text, mirroring on_final's contract; the engine needs
        only the signal). Always stamps the silence clock — a callee
        mid-utterance is never nudged. While a reply is being spoken
        the same signal is BARGE-IN: set the interrupt so the frame
        pump stops at the next frame boundary and the reply is
        abandoned (design §5.2 — a first-class event, never an error)."""
        self._last_speech_at = time.monotonic()
        if self._speaking and not self._ended:
            self._interrupt.set()

    async def start(self) -> None:
        self._t0 = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def finish(self, drain_s: float = 2.5) -> None:
        """Stream end: stop the loop, letting an in-flight turn complete
        within the drain budget; a turn stuck past it is cancelled (its
        worker thread is left to time out on its own — it only writes
        into a dead queue). Customer finals that arrived after the last
        reply are still folded into the transcript."""
        if self._task is not None:
            self._inbox.put_nowait(None)
            try:
                await asyncio.wait_for(self._task, timeout=drain_s)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except BaseException:
                    pass
            except BaseException:
                pass
            self._task = None
            if self._agent_turns == 0 and self.llm_status == "active":
                # The brain never completed a single turn before the
                # stream ended (e.g. the provider never answered and
                # the in-flight kickoff was cancelled by the drain):
                # an armed-but-silent leg is recorded as failed,
                # never left dressed up as "active".
                self.llm_status = "failed"
                self.llm_detail = "LLM did not respond before the call ended"
                if self.tts_status != "failed":
                    self.tts_status = "failed"
                    self.tts_detail = "no reply to speak (LLM leg failed)"
        while True:
            try:
                turn = self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if turn is not None:
                self.turns.append(turn)

    # ── the loop ──────────────────────────────────────────────────────
    def _elapsed(self) -> float:
        return time.monotonic() - (self._t0 or time.monotonic())

    def _end(self, reason: str) -> None:
        if not self.end_reason:
            self.end_reason = reason
        self._ended = True

    def _fail_llm(self, detail: str) -> None:
        self.llm_status = "failed"
        self.llm_detail = f"LLM {detail}"[:500]
        if self.tts_status != "failed":
            # Nothing to speak when the brain produced no reply.
            self.tts_status = "failed"
            self.tts_detail = "no reply to speak (LLM leg failed)"
        self._ended = True

    async def _run(self) -> None:
        try:
            await self._take_turn(context=KICKOFF)
            while not self._ended:
                got = await self._await_input()
                if got == "closed":
                    break  # stream is closing — finish() called
                if got == "duration":
                    self._end("max_duration")
                    break
                if got == "silence":
                    if await self._handle_silence():
                        break  # the call ended (polite close / guard / failure)
                    continue
                turn = got  # a customer final from the STT leg
                self._silence_prompts_used = 0
                self.turns.append(turn)
                self.messages.append({"role": "user",
                                      "content": turn.get("text", "")})
                await self._take_turn()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # leg failures are recorded on the legs, never raised

    async def _await_input(self):
        """Wait for the next thing that can happen while the customer
        holds the floor: a customer final (returned as the turn dict),
        "closed" (the stream is ending — finish() sent the sentinel),
        "duration" (the max-duration guard), or "silence" (a full
        silence window passed with no speech activity, A9).

        The silence deadline is `silence_prompt_after_s` past the later
        of (this wait's start, the last STT speech activity): partials
        landing mid-wait push the deadline out, so a customer who is
        mid-utterance when the deadline passes is never nudged — the
        wait simply re-arms from their last activity."""
        self._idle_since = time.monotonic()
        idle_basis = max(self._idle_since, self._last_speech_at)
        while True:
            if self._ended:
                return "closed"
            now = time.monotonic()
            remaining = self._max_duration_s - (now - (self._t0 or now))
            if remaining <= 0:
                return "duration"
            silence_in = idle_basis + self._silence_after_s - now
            if silence_in <= 0:
                return "silence"
            try:
                turn = await asyncio.wait_for(
                    self._inbox.get(), timeout=min(remaining, silence_in))
                return "closed" if turn is None else turn
            except asyncio.TimeoutError:
                new_basis = max(self._idle_since, self._last_speech_at)
                if new_basis > idle_basis:
                    idle_basis = new_basis  # speech raced the deadline
                    continue
                return "duration" if remaining <= silence_in else "silence"

    async def _handle_silence(self) -> bool:
        """One silence window passed unanswered (A9, design §5.2).

        While silence prompts remain, speak one (an LLM turn carrying
        the bracketed nudge context — the wording is the model's own,
        from the agent's prompt) and keep waiting. Once they are used
        up, speak the polite closing the same way and end the call:
        end_reason no_input is assigned over the model's own end_call
        in that turn, because the silence is the cause of the ending.
        A customer who barges in on the closing has answered after
        all — the call then continues normally. Returns True when the
        call is over."""
        if self._silence_prompts_used < self._silence_max_prompts:
            self._silence_prompts_used += 1
            await self._take_turn(context=SILENCE_NUDGE)
            return self._ended
        outcome = await self._take_turn(context=SILENCE_CLOSING)
        if outcome == "interrupted":
            return False  # the customer spoke during the closing
        if self._ended and self.end_reason in ("max_turns", "max_duration"):
            return True  # a guard, not the silence, ended this call
        self.end_reason = "no_input"
        self._ended = True
        try:
            await self._close_stream()
        except Exception:
            pass
        return True

    async def _take_turn(self, context: str | None = None) -> str:
        """Run one agent turn and speak it. `context`, when given, is a
        bracketed engine note appended as a user message first (the
        kickoff, a silence nudge, the silence closing — the wording the
        model speaks is always its own, from the prompt).

        Returns "completed" | "interrupted" | "failed" | "guarded".
        Barge-in (A9): if the customer starts speaking mid-reply, the
        frame pump stops at the next frame boundary, the reply's
        unsent sentences are abandoned, the transport is asked to
        flush its buffered audio, and the turn is recorded with ONLY
        the words actually spoken, flagged interrupted — the model's
        end_call action in such a turn is discarded, because the
        customer has taken the floor."""
        if self._agent_turns >= self._max_turns:
            self._end("max_turns")
            return "guarded"
        if self._elapsed() > self._max_duration_s:
            self._end("max_duration")
            return "guarded"
        if context:
            self.messages.append({"role": "user", "content": context})

        self._interrupt.clear()
        self._speaking = True
        loop = asyncio.get_running_loop()
        tokens: asyncio.Queue = asyncio.Queue()

        def _produce() -> None:
            try:
                full, tool = stream_chat(
                    self._api_key, self._model, list(self.messages),
                    lambda t: loop.call_soon_threadsafe(
                        tokens.put_nowait, ("token", t)))
                loop.call_soon_threadsafe(tokens.put_nowait, ("end", full, tool))
            except Exception as e:
                loop.call_soon_threadsafe(tokens.put_nowait,
                                          ("error", type(e).__name__))

        producer = asyncio.create_task(asyncio.to_thread(_produce))
        interrupt_wait = asyncio.ensure_future(self._interrupt.wait())
        chunker = SentenceChunker()
        full_text, tool_name, error = "", "", ""
        spoken: list[str] = []       # sentences delivered (pumped, or text-standing when TTS is dead)
        inflight_text, inflight_frames = "", 0  # the sentence the pump was cut inside
        interrupted = False
        try:
            while True:
                if self._interrupt.is_set():
                    interrupted = True
                    break
                get_task = asyncio.ensure_future(tokens.get())
                done, _pending = await asyncio.wait(
                    {get_task, interrupt_wait},
                    return_when=asyncio.FIRST_COMPLETED)
                if get_task not in done:
                    get_task.cancel()
                    interrupted = True
                    break
                event = get_task.result()
                if event[0] == "token":
                    for sentence in chunker.feed(event[1]):
                        status, frames = await self._speak(sentence)
                        if status == "interrupted":
                            inflight_text, inflight_frames = sentence, frames
                            interrupted = True
                            break
                        spoken.append(sentence)
                    if interrupted:
                        break
                elif event[0] == "end":
                    full_text, tool_name = event[1], event[2]
                    for sentence in chunker.flush():
                        status, frames = await self._speak(sentence)
                        if status == "interrupted":
                            inflight_text, inflight_frames = sentence, frames
                            interrupted = True
                            break
                        spoken.append(sentence)
                    break
                else:  # "error"
                    error = event[1]
                    break
        finally:
            if not producer.done():
                producer.cancel()
            interrupt_wait.cancel()
            self._speaking = False
        if error:
            self._fail_llm(error)
            return "failed"

        if interrupted:
            self._interruptions += 1
            parts = list(spoken)
            if inflight_text and inflight_frames > 0:
                parts.append(inflight_text)
            text = " ".join(parts).strip()
            if self._clear_audio is not None:
                try:
                    await self._clear_audio()
                except Exception:
                    pass  # best-effort flush; the pump stop is the cut
            if text:
                turn = {"speaker": "agent", "text": text,
                        "offset_s": round(self._elapsed(), 2),
                        "interrupted": True}
                self.turns.append(turn)
                stt_module.append_turn(self.session_id, turn)
                self.messages.append({"role": "assistant", "content": text})
                self._agent_turns += 1
            return "interrupted"

        text = (full_text or "").strip()
        if text:
            turn = {"speaker": "agent", "text": text,
                    "offset_s": round(self._elapsed(), 2)}
            self.turns.append(turn)
            stt_module.append_turn(self.session_id, turn)
            self.messages.append({"role": "assistant", "content": text})
            self._agent_turns += 1
        if tool_name == "end_call":
            self._end("agent_end_call")
            try:
                await self._close_stream()
            except Exception:
                pass
        return "completed"

    async def _speak(self, sentence: str) -> tuple[str, int]:
        """Synthesize one sentence and pump its audio into the Plivo
        stream frame by frame. Returns (status, frames_sent):

        - "spoken" — the whole sentence was pumped.
        - "interrupted" — barge-in landed: the pump stopped at a frame
          boundary (frames_sent says how much of the sentence the
          callee heard; 0 when the cut came before the first frame,
          e.g. during synthesis).
        - "failed" — the audio could not be pumped (TTS leg dead or
          the stream send died); per the A8 rule the sentence's TEXT
          still stands as delivered in the transcript — only the
          audio is missing, and the leg records say so.
        """
        if self.tts_status == "failed" or self._send_dead:
            return "failed", 0
        if self._interrupt.is_set():
            return "interrupted", 0
        try:
            audio = await asyncio.to_thread(
                synthesize_tts, self._api_key, sentence, self.language,
                self._speaker)
        except Exception as e:
            self.tts_status = "failed"
            self.tts_detail = f"TTS {type(e).__name__}"[:500]
            return "failed", 0
        if self._interrupt.is_set():
            return "interrupted", 0
        frames = 0
        try:
            for i in range(0, len(audio), 160):
                if self._interrupt.is_set():
                    self._tts_frames += frames
                    return "interrupted", frames
                await self._send_audio(audio[i:i + 160])
                frames += 1
        except Exception:
            self._send_dead = True
            self._tts_frames += frames
            return "failed", frames
        self._tts_frames += frames
        return "spoken", frames
