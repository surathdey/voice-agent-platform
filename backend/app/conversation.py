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
  the one text IS the system prompt, plus one engine-side language note
  (below) — machine-readable config outside the prompt text, the same
  category as language_rule.
- `stream_chat()` — Sarvam chat completions, streamed (HTTPS POST per
  turn, api-subscription-key auth). Yields reply tokens through a
  callback; returns (full_text, tool_name, usage) where usage is the
  last usage object Sarvam streamed (prompt / completion / cached
  tokens — live-proven Oct 26, 2026: Sarvam sends usage on streamed
  chunks even without stream_options, which the old parser dropped).
  The one tool offered is `end_call` — the design's end-call action:
  when the model calls it (its FLOW reached a closing), the engine
  speaks the turn's text, then ends the call (end_reason
  agent_end_call).
- `synthesize_tts()` — Bulbul TTS, one request per completed reply
  sentence; the returned audio is framed back into the Plivo stream as
  playAudio media frames by the engine. The engine prefers the
  streaming TTS path (`_speak_streaming`, μ-law chunks over the
  /text-to-speech/ws WebSocket) and falls back to this REST call on
  any streaming failure before the first audio frame.
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
  `note_speech_activity()`. Once the current turn's audio has started
  pumping, that signal IS an interruption: the engine stops pumping
  audio frames for the current reply at the next frame boundary,
  abandons the reply's unsent sentences (they are never synthesised
  or spoken), asks the transport to flush its buffered audio (the
  `clear_audio` callback — Plivo's clearAudio message, best-effort;
  the pump stop is the authoritative cut, and any audio the provider
  had already played out cannot be recalled), and records the agent
  turn with ONLY the words actually spoken, flagged `interrupted`.
  The model's end_call action in an interrupted turn is discarded —
  the customer took the floor, and their utterance (already queued by
  the STT leg) becomes the next turn's input. Interruption is a
  first-class event, never an error. Barge-in cuts ONLY audio that is
  actually pumping right now: a partial arriving while the reply is
  still being generated (LLM streaming / TTS synthesis) or in a
  silent gap between sentences only resets the silence clock — cutting
  an unspoken turn only added delay and, on the Oct 26 Retest 4 call,
  silently killed the kickoff turn (and with it the kickoff-only
  holding line) before any audio existed to cut. A partial landing
  while frames are pumping still cuts at the next frame boundary.
- **Empty end_call.** If the model calls end_call with NO spoken text
  (Retest 4: the final "হ্যাঁ" was answered with a silent
  agent_end_call and no closing turn), the engine never ends silently:
  it retries once for the closing line, and if that also comes back
  empty it speaks a neutral fallback closing in the call's language
  before ending. A call never ends in silence after the customer has
  spoken.
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
import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
import wave

from . import stt as stt_module
from .audio_codec import mulaw_encode
from .config import settings

log = logging.getLogger(__name__)

# The user-role kickoff that starts every call: the model generates the
# opening by following its own prompt's FLOW (recorded disclosure,
# greeting, first question) — no greeting text lives in code.
KICKOFF = ("[The call has just connected. Begin the conversation now, "
           "following your FLOW — speak your opening line.]")

# Holding line (approved Oct 26, 2026; extended to every turn Oct 26,
# 2026, stall fix): if a turn's first audio is not ready within
# HOLDING_LINE_AFTER_S of THAT TURN's start, the engine speaks ONE
# short acknowledgement in the call's language so a slow turn never
# sounds like a dead line. It is filler audio only — never recorded
# as a turn, never part of the agent's prompt, never spoken twice
# per call, and it never delays a reply that is already ready (the
# timer only fires while no audio has been pumped yet on this turn).
# The per-turn clock (not the call clock) matters: the original
# kickoff-only version measured from engine start, which would fire
# instantly on any later turn.
# Threshold justification: a healthy kickoff reaches first audio in
# ~6 s (LLM first token ~4.1 s + Bulbul ~1.7 s, measured live Oct 26),
# so 5 s sits just under healthy — the dead-air gap never exceeds
# ~5 s, while a pathological ~21 s kickoff gets audio at ~5-7 s
# instead of silence until the caller hangs up.
HOLDING_LINE_AFTER_S = 5.0
HOLDING_LINES = {
    "bn": "এক মুহূর্ত অপেক্ষা করুন, দেখছি।",
    "hi": "एक क्षण रुकिए, देख रहा हूँ।",
    "en": "One moment please, let me check that.",
}


def holding_line(language: str) -> str:
    """The holding acknowledgement for a call language ("bn-IN" /
    "hi-IN" / "en-IN" — the same codes the engine already speaks).
    Unknown codes fall back to English; the wording is fixed engine
    text, not prompt content, and is always synthesized through the
    existing Bulbul path (bulbul_speaker mapping included)."""
    return HOLDING_LINES.get((language or "")[:2].lower(),
                             HOLDING_LINES["en"])


# ── Greeting fast-path (Oct 26, 2026) ─────────────────────────────────
# A bare greeting on a call whose greeting never completed (the
# kickoff was cut by barge-in before any completed agent turn) is
# answered at once with a fixed line — greeting + bank identification
# + recorded-call note + the identity question every agent asks
# first — via TTS only, no LLM round trip; the call then waits for
# the customer's answer normally. The line is the prompts' OPENING
# section, word for word (identical across all five agents), so the
# persona name here is Priya; a persona rename must update these
# lines (ideally the name moves into agent config — a separate
# change, not this one). The matcher is deliberately strict: anything
# that is not exactly a bare greeting — a question ("who are
# you?"), a denial, a confirmation, any content words — falls
# through to the LLM path. Ambiguous always means LLM, never
# fast-path.
_BARE_GREETINGS = {
    "hello", "hi", "hey",
    "good morning", "good afternoon", "good evening",
    "namaste", "namaskar",
    "नमस्ते", "नमस्कार", "हैलो", "हेलो", "हाय",
    "নমস্কার", "হ্যালো", "হেলো", "হাই",
}
_BARE_GREETING_STRIP = "।.!?,;: \t\n\r"


def _normalize_bare(text: str) -> str:
    """Normalize a customer final for the bare-greeting test: trim,
    lowercase Latin letters, drop trailing sentence punctuation, and
    collapse inner whitespace. Nothing else about the text is
    changed — the matcher below demands an exact set hit."""
    t = (text or "").strip().lower()
    t = t.strip(_BARE_GREETING_STRIP)
    return " ".join(t.split())


def is_bare_greeting(text: str) -> bool:
    """True ONLY for an exact bare greeting (see _BARE_GREETINGS).
    Questions, denials, confirmations, and any multi-word content
    return False — they always take the LLM path."""
    norm = _normalize_bare(text)
    if not norm or len(norm) > 30:
        return False
    return norm in _BARE_GREETINGS


def fast_greeting_line(language: str, voice_gender: str,
                       bank_name: str, lead_name: str) -> str | None:
    """The fast-path greeting line for a call language ("hi-IN" /
    "bn-IN" / "en-IN" codes; unknown codes fall back to English).
    Returns None when no identity question can be formed (no lead
    name) — the caller then falls back to the LLM path. The bank
    clause is omitted when no bank name is on the lead."""
    lang = (language or "")[:2].lower()
    name = (lead_name or "").strip()
    if not name:
        return None
    bank = (bank_name or "").strip()
    female = (voice_gender or "female").strip().lower() != "male"
    if lang == "hi":
        verb = "रही हूँ" if female else "रहा हूँ"
        if bank:
            return (f"नमस्ते, मैं {bank} से प्रिया बोल {verb}। "
                    f"यह एक रिकॉर्डेड कॉल है। "
                    f"क्या मैं {name} जी से बात कर {verb}?")
        return (f"नमस्ते, मैं प्रिया बोल {verb}। "
                f"यह एक रिकॉर्डेड कॉल है। "
                f"क्या मैं {name} जी से बात कर {verb}?")
    if lang == "bn":
        if bank:
            return (f"নমস্কার, আমি {bank} থেকে প্রিয়া বলছি। "
                    f"এটি একটি রেকর্ডেড কল। "
                    f"আমি কি {name} জি-এর সাথে কথা বলছি?")
        return (f"নমস্কার, আমি প্রিয়া বলছি। "
                f"এটি একটি রেকর্ডেড কল। "
                f"আমি কি {name} জি-এর সাথে কথা বলছি?")
    if bank:
        return (f"Namaste, I am Priya calling from {bank}. "
                f"This is a recorded call. "
                f"Am I speaking with {name} ji?")
    return (f"Namaste, I am Priya. This is a recorded call. "
            f"Am I speaking with {name} ji?")

# Deterministic last-resort closing (Oct 26, 2026, Retest 4 fix): when
# the model calls end_call with NO spoken text, the retry below usually
# produces the prompt's own closing; if it does not, this neutral,
# language-matched line is spoken so a call never ends in silence
# after the customer has spoken. It claims no outcome — the model's
# end_call decision already fixed that — it only guarantees audible
# final words.
FALLBACK_CLOSINGS = {
    "bn": "ধন্যবাদ, ভালো থাকবেন।",
    "hi": "धन्यवाद, आपका दिन शुभ हो।",
    "en": "Thank you, have a good day.",
}


def fallback_closing(language: str) -> str:
    """The neutral fallback closing for a call language (same codes
    as holding_line; unknown codes fall back to English)."""
    return FALLBACK_CLOSINGS.get((language or "")[:2].lower(),
                                 FALLBACK_CLOSINGS["en"])


def _norm_text(text: str) -> str:
    """Closing-match normalisation: lowercase, collapsed whitespace.
    Punctuation is kept — closings are matched on their full wording,
    not on keywords, so a thanks mid-conversation never matches."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def prompt_closings(prompt: str) -> list[str]:
    """The approved closing lines from an agent prompt's CLOSINGS
    section: every double-quoted span in that section (each closing
    is one quoted multi-line string, per language, per outcome).
    CLOSINGS is the last section of every agent prompt, so the section
    runs to the end of the prompt. Returns [] when the prompt has no
    CLOSINGS section — the end-call net then matches only the
    deterministic fallback closings, never free text."""
    lines = (prompt or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("CLOSINGS"):
            start = i
            break
    if start is None:
        return []
    section = "\n".join(lines[start:])
    return [m.group(1) for m in re.finditer(r'"([^"]+)"', section)]


def is_closing_text(text: str, closings: list[str]) -> str:
    """End-call net match: "" when the turn text is not a closing,
    else the matched closing. A turn matches when its normalised text
    EQUALS a closing, or ENDS WITH one (the model may prepend a short
    acknowledgement to the closing line — the call is still over).
    A match must be at least 10 characters, so a bare "thanks" never
    fires the net. Mid-turn closings (more text after the closing
    line) do NOT match — the turn is still going."""
    norm = _norm_text(text)
    for closing in closings:
        norm_closing = _norm_text(closing)
        if len(norm_closing) < 10:
            continue
        if norm == norm_closing or norm.endswith(norm_closing):
            return closing
    return ""


# The retry note for an empty end_call: the model decided to end but
# spoke no closing. One retry turn asks for the closing line itself;
# the deterministic fallback above covers a second empty answer.
CLOSING_RETRY_NOTE = ("[You called end_call, but you have not spoken your "
                      "closing line yet. Speak your complete closing line "
                      "now, in full, and then end the call.]")

# The call-language note (delay + correctness fix, Oct 26, 2026):
# the engine appends this bracketed note to the SYSTEM message (never
# to the stored prompt, never to DISPOSITIONS) naming the call's
# resolved language. Retest 5 proved the kickoff language is otherwise
# a model guess: the same RPI prompt + lead spoke Bengali on Retest 4
# and English on Retest 5, and the prompt's "speak the language of the
# call" cannot ground a language the model was never told. The note is
# engine-side, config-driven (from the resolved language code), and
# persists across turns — the mid-call flip has the same fix as the
# kickoff guess. rendered_prompt stays the clean prompt; the
# classifier keeps classifying against the prompt alone.
_LANGUAGE_NAMES = {"bn": "Bengali", "hi": "Hindi", "en": "English"}


def language_note(language: str) -> str:
    """The bracketed language note for a call language ("bn-IN" /
    "hi-IN" / "en-IN" — the same codes the engine already speaks).
    Unknown codes fall back to English; the note is fixed engine text,
    not prompt content."""
    name = _LANGUAGE_NAMES.get((language or "")[:2].lower(), "English")
    return (f"\n\n[The call's language is {name}. Speak {name} from "
            f"start to finish, including your greeting, questions, and "
            f"closing.]")


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


# Bulbul v4-flash speakers are PER-LANGUAGE persona ids (Oct 26, 2026
# voice switch: bn Roopa + hi Kavya, V4 Flash only, never V3). The
# legacy platform voice ids (bulbul-<lang>-<gender>-<n>) and the v3
# short names (priya, aditya, ...) are NOT valid v4 speakers: Sarvam
# rejects them HTTP 400. They map through this table by call language
# + voice gender instead of being passed through. A tts_voice_id that
# already looks like a v4 persona id (<name>_<lang>_<style>, e.g.
# roopa_bn_conversational) passes through untouched. Every cell below
# is an id from Sarvam's own v4-flash list (API-confirmed Oct 26,
# 2026); bn has exactly 3 personas, so its male cell is the only
# conversational male available (arnab_bn_conversation).
_V4_PERSONA_RE = re.compile(r"^[a-z0-9]+_[a-z]{2}_[a-z_]+$")
_V4_SPEAKER_BY_LANG_GENDER = {
    "bn": {"female": "roopa_bn_conversational",
           "male": "arnab_bn_conversation"},
    "hi": {"female": "kavya_hi_conversational",
           "male": "aditya_hi_conversational"},
    "en": {"female": "roopa_en_conversational",
           "male": "dev_en_conversational"},
}


def bulbul_speaker(tts_voice_id: str, language: str, voice_gender: str) -> str:
    """The Bulbul speaker for one call, on bulbul:v4-flash: the
    agent's configured voice id when it is already a v4 persona id;
    otherwise the per-language table above (call language + voice
    gender). Female bn = roopa_bn_conversational and female hi =
    kavya_hi_conversational are Surath's Oct 26, 2026 picks; female en
    = roopa_en_conversational (same persona family as the approved
    Bengali Roopa — Surath may veto it for another en persona).
    Unknown languages fall back to the en row."""
    vid = (tts_voice_id or "").strip()
    if vid and _V4_PERSONA_RE.match(vid):
        return vid
    lang = (language or "")[:2].lower()
    if lang not in _V4_SPEAKER_BY_LANG_GENDER:
        lang = "en"
    gender = (voice_gender or "female").strip().lower()
    if gender not in ("female", "male"):
        gender = "female"
    return _V4_SPEAKER_BY_LANG_GENDER[lang][gender]


# ── Sarvam HTTP seam (LLM + TTS) ─────────────────────────────────────────

def _sarvam_request(path: str, api_key: str, payload: dict,
                    timeout: int = 20):
    req = urllib.request.Request(
        f"{settings.SARVAM_BASE_URL.rstrip('/')}{path}",
        method="POST", data=json.dumps(payload).encode(),
        headers={"api-subscription-key": api_key,
                 "Content-Type": "application/json",
                 # Product User-Agent — some provider edges 403 the
                 # urllib default UA before auth (Vapi, proven live).
                 "User-Agent": "VoiceAgentPlatform/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)


def stream_chat(api_key: str, model: str, messages: list, on_token) -> tuple[str, str, dict]:
    """One streamed chat completion. on_token(text) is invoked per
    content delta (from the calling thread); returns (full_text,
    tool_name, usage) where tool_name is "end_call" when the model
    called it and usage is the last usage object Sarvam streamed
    ({} when the provider sent none — the engine then falls back to
    duration metering for the LLM leg, as before). Reasoning deltas
    carry no content and are never yielded; their tokens still count
    in usage.completion_tokens (Sarvam bills reasoning as output).
    Raises on transport / HTTP errors — the caller records the leg."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "tools": [END_CALL_TOOL],
        "tool_choice": "auto",
    }
    if settings.SARVAM_REASONING_EFFORT:
        # Deliberate delay + cost lever (default unset = provider
        # default): measured Oct 26, 2026, reasoning_effort=low cut
        # the RPI kickoff from 2.48 s / 274 completion tokens to
        # 1.72 s / 143, TTFT unchanged.
        payload["reasoning_effort"] = settings.SARVAM_REASONING_EFFORT
    full: list[str] = []
    tool_name = ""
    usage: dict = {}
    with _sarvam_request("/v1/chat/completions", api_key, payload) as resp:
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
            if isinstance(chunk.get("usage"), dict):
                # Sarvam streams usage on chunks (live-proven, with
                # and without stream_options); the last one is the
                # call's cumulative usage.
                usage = chunk["usage"]
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
    return "".join(full), tool_name, usage


def chat_once(api_key: str, model: str, messages: list,
              timeout: int = 30) -> tuple[str, dict]:
    """One non-streamed chat completion; returns (content, usage)
    where usage is the response body's usage object ({} when absent).
    Raises on transport / HTTP errors."""
    payload = {"model": model, "messages": messages, "stream": False}
    with _sarvam_request("/v1/chat/completions", api_key, payload,
                         timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    choices = body.get("choices") or []
    if not choices:
        raise ValueError("empty_completion")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        raise ValueError("empty_completion")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return content, usage


def synthesize_tts(api_key: str, text: str, language: str,
                   speaker: str) -> bytes:
    """One Bulbul synthesis (design §8.4: one per reply sentence).
    Returns μ-law 8 kHz bytes — the one format the stream pump speaks.
    Bulbul returns a WAV file (RIFF header, PCM16, 8000 Hz, mono);
    pumping those bytes raw as μ-law produced full static on the
    Oct 26 Retest 3 call, so this function parses the WAV and converts
    PCM16 → μ-law here, at the provider boundary. Raises on transport /
    HTTP errors, and raises ValueError (fail loudly, never silently
    pass garbage) when the response is not a WAV in the expected
    format. Single caller: ConversationEngine._speak."""
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
    raw = base64.b64decode(audios[0])
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            channels = wav.getnchannels()
            sampwidth = wav.getsampwidth()
            framerate = wav.getframerate()
            if channels != 1 or sampwidth != 2 or framerate != 8000:
                raise ValueError(
                    f"unexpected_tts_wav_format channels={channels} "
                    f"sampwidth={sampwidth} framerate={framerate}")
            pcm = wav.readframes(wav.getnframes())
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"invalid_tts_wav: {type(e).__name__}") from e
    if not pcm:
        raise ValueError("empty_tts_audio")
    return mulaw_encode(pcm)


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
                  captured_fields: list, turns: list) -> tuple[dict, dict]:
    """Classify one finished call against the agent's OWN prompt.

    Returns ({disposition, captured_fields, confidence, summary,
    review_required, review_reason}, usage) where usage is the
    classifier call's token usage ({} when the provider sent none —
    it joins the call's LLM leg cost). Every failure — the classifier
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
        content, usage = chat_once(api_key, model, messages)
    except Exception as e:
        return ({**failed,
                 "review_reason": f"classifier call failed ({type(e).__name__})"},
                {})
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
        return ({**failed,
                 "review_reason": "classifier output was not valid JSON"},
                usage)
    disposition = data.get("disposition") or ""
    if disposition not in buckets:
        return ({**failed,
                 "review_reason": ("classifier returned a disposition outside "
                                   "the prompt's bucket set")},
                usage)
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
    return ({
        "disposition": disposition,
        "captured_fields": captured,
        "confidence": min(1.0, max(0.0, confidence)),
        "summary": data.get("summary") or "",
        "review_required": False,
        "review_reason": "",
    }, usage)


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


async def _async_one(data: bytes):
    """An async iterable yielding one bytes object — lets the REST
    TTS path (whole audio already in hand) reuse _pump_audio."""
    if data:
        yield data


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
        _lv = lead_variables(campaign, lead)
        # C2 pinning: the campaign's pinned prompt is what the engine
        # speaks AND what the classifier classifies against (via
        # rendered_prompt) — pre-C2 campaigns fall back to live.
        _pinned = getattr(campaign, "pinned_prompt", "") or ""
        self._prompt = render_prompt(_pinned or agent.prompt or "", _lv)
        self._model = agent.llm_model or "sarvam-105b"
        self._speaker = bulbul_speaker(agent.tts_voice_id or "", language,
                                       agent.voice_gender or "female")
        self._voice_gender = (agent.voice_gender or "female")
        # Fast-path greeting inputs (plain values, like the rest of
        # the snapshotted config): the lead's name is required to
        # form the identity question — without it the fast-path
        # never fires (see _run / _maybe_fast_greet).
        self._lead_name = _lv.get("lead_name", "") or ""
        self._bank_name = _lv.get("bank_name", "") or ""
        self._fast_greeted = False
        self._max_duration_s = int(agent.max_duration_s or 300)
        self._max_turns = int(getattr(agent, "max_turns", 50) or 50)
        self._silence_after_s = int(
            getattr(agent, "silence_prompt_after_s", 8) or 8)
        _smp = getattr(agent, "silence_max_prompts", 1)
        self._silence_max_prompts = 1 if _smp is None else int(_smp)
        self.captured_fields = list(agent.captured_fields or [])

        self.turns: list[dict] = []        # the conversation, in order
        # Turn-loop tracing (stall fix, Oct 26, 2026): per-turn stage
        # timings, kept OFF the customer transcript and off the cost
        # record — container logs only, plus this in-memory list the
        # finalize path logs as one compact summary. A stall's stage
        # is read off these numbers, never guessed: turn_start,
        # llm_first_token, llm_end, per-sentence tts_start /
        # tts_first_frame / tts_end / frames / status, turn_end,
        # outcome. Barge-in and inbox events trace the same way.
        self.turn_timings: list[dict] = []
        # End-call net (stall fix): the approved closings for this
        # agent (its prompt's CLOSINGS section) plus the deterministic
        # fallback closings in every language. A completed turn whose
        # text IS one of these ends the call even when the model
        # forgets the end_call tool — the self-hosted equivalent of
        # Vapi's endCallPhrases. Never matched on free text.
        self._closings = (prompt_closings(self._prompt)
                          + list(FALLBACK_CLOSINGS.values()))
        # The system message is the single prompt PLUS the engine-side
        # language note (language_note above): the stored prompt is
        # untouched, and rendered_prompt still returns it clean.
        self.messages: list[dict] = [{"role": "system",
                                      "content": self._prompt
                                      + language_note(language)}]
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
        # Cost metering, token/character basis (Oct 26, 2026 — the
        # provider returns usage, so duration metering is the fallback,
        # not the truth): LLM tokens summed across every streamed turn
        # (prompt / completion / cached — reasoning bills as output
        # inside completion), and TTS characters summed across every
        # synthesis REQUEST _speak makes (streaming or REST, including
        # holding lines and fallback closings, and including sentences
        # cut by barge-in — Bulbul bills the full request, not the
        # pumped audio). app.costing prices both from the rate card.
        self._llm_usage = {"prompt_tokens": 0, "completion_tokens": 0,
                           "cached_tokens": 0}
        self._tts_chars = 0
        # Streaming-TTS health: consecutive pre-audio streaming
        # failures. Two in a row disables streaming for the rest of
        # the call (REST thereafter) — a dead WS endpoint must not
        # add a connect attempt to every sentence. A mid-stream
        # failure disables it at once (the sentence is already
        # half-heard; replaying it via REST would double-speak it).
        self._tts_stream_failures = 0
        self._tts_streaming_dead = False
        # Elapsed seconds at the first pumped audio frame; None means
        # no audio ever left the engine (diagnoses silent calls: a
        # completed turn with first_audio_s None = the pump never
        # delivered, not a slow brain). A holding line counts here —
        # the caller did hear something — and metrics() separately
        # reports holding_played so the two are distinguishable.
        self._first_audio_s: float | None = None
        # Holding line state (kickoff only): set the moment the
        # acknowledgement is spoken (or suppressed by barge-in) so it
        # can never play twice on one call.
        self._holding_played = False
        self.end_reason = ""               # agent_end_call | max_turns | max_duration | no_input
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._t0: float | None = None
        self._agent_turns = 0
        self._ended = False
        self._send_dead = False
        # A9 state: playback + interruption + silence bookkeeping.
        self._speaking = False             # a reply turn is being spoken
        # Barge-in arming (Retest 4 fix): True only while _speak is
        # actively pumping frames. Partials outside a pump window must
        # not kill the turn — there is no audio playing to cut, and
        # killing the turn then only delays the reply.
        self._pumping = False
        self._interrupt = asyncio.Event()  # set by barge-in mid-reply
        self._interruptions = 0
        self._idle_since = 0.0             # when the current wait began
        self._last_speech_at = 0.0         # last STT partial/final stamp
        self._silence_prompts_used = 0     # consecutive unanswered nudges
        # Elapsed seconds at the first pumped frame OF THE CURRENT
        # TURN (reset per turn; _first_audio_s above is per call).
        # Drives the per-turn holding-line clock and the tracing.
        self._turn_first_frame_at: float | None = None
        # Elapsed seconds at the first pumped frame OF THE CURRENT
        # PUMP (one sentence; reset per _speak by the tracing).
        self._pump_first_frame_at: float | None = None
        # Persistent Bulbul streaming connection (stall fix, Oct 26,
        # 2026): one WS per call, reused across sentences — live
        # probe proved a second text+flush on the same connection
        # returns audio + a completion event, so sentences after the
        # first skip the ~0.7 s connect. Dropped (never reused) the
        # moment a sentence ends cut or broken: the connection's
        # queue would otherwise hand the cut sentence's tail to the
        # next sentence as if it were new audio. See _speak_streaming.
        self._tts_ws = None

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
        actually pumped across every sentence of this call, plus the
        token/character usage app.costing prices the LLM/TTS legs
        from (the classifier's own usage joins under
        llm_usage_classify at the finalize call site, not here —
        classify_call runs outside the engine)."""
        return {"tts_frames": self._tts_frames,
                "tts_chars": self._tts_chars,
                "llm_usage": dict(self._llm_usage),
                "first_audio_s": self._first_audio_s,
                "holding_played": self._holding_played}

    def enqueue_final(self, turn: dict) -> None:
        """STT leg → engine: one final customer segment. Runs on the
        event loop (the STT session's receive task); finals arriving
        after the engine ended are transcript-only and are drained into
        self.turns by finish(). A final is speech activity too — it
        stamps the silence clock (A9)."""
        self._last_speech_at = time.monotonic()
        if not self._ended:
            self._inbox.put_nowait(turn)
            self._trace("final_enqueued",
                        queue=self._inbox.qsize(),
                        text=(turn.get("text", "") or "")[:40])

    def note_speech_activity(self, _partial_text: str = "") -> None:
        """STT leg → engine: the callee is speaking (a non-empty
        partial just landed, A9 — the session calls this with the
        partial text, mirroring on_final's contract; the engine needs
        only the signal). Always stamps the silence clock — a callee
        mid-utterance is never nudged. While the current turn's audio
        is actively pumping, the same signal is BARGE-IN: set the
        interrupt so the frame pump stops at the next frame boundary
        and the reply is abandoned (design §5.2 — a first-class event,
        never an error). Outside a pump window — while the reply is
        still being generated, or in a silent gap between sentences —
        the signal is NOT an interruption: there is nothing playing to
        cut, and killing the turn then only delays the reply (Retest 4:
        hello partials during the kickoff's LLM wait silently killed
        the kickoff turn and its holding line)."""
        self._last_speech_at = time.monotonic()
        if self._speaking and self._pumping and not self._ended:
            self._interrupt.set()
            self._trace("barge_in",
                        turn=self._agent_turns,
                        pumping=self._pumping)

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
        # The call's streaming TTS connection ends with the call —
        # a leaked WS per call would accumulate on the long-lived
        # backend process.
        await self._tts_ws_drop()

    # ── the loop ──────────────────────────────────────────────────────
    def _elapsed(self) -> float:
        return time.monotonic() - (self._t0 or time.monotonic())

    def _trace(self, event: str, **fields) -> None:
        """One turn-loop trace line (container logs, never the
        transcript): session + elapsed + event + compact fields.
        No customer text is logged beyond a short prefix — timings
        are the point, not content."""
        try:
            detail = " ".join(f"{k}={v}" for k, v in fields.items())
            log.info("trace session=%s t=%.2f %s %s",
                     self.session_id, self._elapsed(), event, detail)
        except Exception:
            pass

    def _trace_turn_end(self, timing: dict, outcome: str,
                        prev_timing) -> None:
        """Close one turn's tracing record: append to turn_timings
        and log the one-line summary (stage budget in one place).
        Restores the enclosing turn's record after a nested retry."""
        try:
            timing["end"] = round(self._elapsed(), 2)
            timing["outcome"] = outcome
            timing["duration"] = round(timing["end"] - timing["start"], 2)
            self.turn_timings.append(timing)
            sents = ";".join(
                f"{s.get('tts', '?')}s/{s.get('status', '?')}"
                for s in timing.get("sentences", []))
            log.info("trace session=%s turn=%s outcome=%s dur=%.2f "
                     "llm_first=%s llm_end=%s tool=%s sents=[%s]",
                     self.session_id, timing["turn"], outcome,
                     timing["duration"],
                     timing.get("llm_first_token"),
                     timing.get("llm_end"), timing.get("tool", ""),
                     sents)
        except Exception:
            pass
        finally:
            self._cur_timing = prev_timing

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
                self._trace("final_dequeued",
                            queue=self._inbox.qsize(),
                            text=(turn.get("text", "") or "")[:40])
                self._silence_prompts_used = 0
                self.turns.append(turn)
                self.messages.append({"role": "user",
                                      "content": turn.get("text", "")})
                if await self._maybe_fast_greet(turn):
                    continue
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

    async def _maybe_fast_greet(self, turn: dict) -> bool:
        """Greeting fast-path (Oct 26, 2026): a BARE greeting on a
        call whose greeting never completed (kickoff interrupted
        before any completed agent turn) is answered at once with
        the fixed greeting + identity question — TTS only, no LLM
        round trip — and the call then waits for the customer's
        answer normally. Returns True when the fast-path handled
        the turn (the caller continues the loop); False means "take
        the normal LLM turn". Never fires on questions, denials,
        confirmations, or content (the matcher decides); never fires
        twice; never fires after any completed agent turn (a
        mid-call hello must not re-ask the identity question);
        never fires past the turn/duration guards; never fires
        without a lead name to ask about. A barge-in during the
        fast greeting abandons it and waits for the interrupting
        final (no LLM turn on stale context); a TTS failure falls
        through to the normal turn (whose own TTS-dead handling
        applies)."""
        if self._fast_greeted or self._ended:
            return False
        if self._agent_turns >= self._max_turns:
            return False
        if self._elapsed() > self._max_duration_s:
            return False
        if any(t.get("speaker") == "agent" and not t.get("interrupted")
               for t in self.turns):
            return False
        text = turn.get("text", "") or ""
        if not is_bare_greeting(text):
            return False
        line = fast_greeting_line(self.language, self._voice_gender,
                                  self._bank_name, self._lead_name)
        if not line:
            return False
        self._interrupt.clear()
        self._speaking = True
        try:
            status, frames = await self._speak(line)
        finally:
            self._speaking = False
        self._trace("fast_greeting", status=status, frames=frames,
                    lang=(self.language or "")[:2])
        if status == "spoken":
            self._fast_greeted = True
            agent_turn = {"speaker": "agent", "text": line,
                          "offset_s": round(self._elapsed(), 2)}
            self.turns.append(agent_turn)
            stt_module.append_turn(self.session_id, agent_turn)
            self.messages.append({"role": "assistant", "content": line})
            self._agent_turns += 1
            return True
        if status == "interrupted":
            # The customer took the floor mid-greeting: wait for
            # their final instead of answering stale context.
            if self._clear_audio is not None:
                try:
                    await self._clear_audio()
                except Exception:
                    pass
            return True
        return False

    async def _take_turn(self, context: str | None = None,
                         _closing_retry: bool = False) -> str:
        """Run one agent turn and speak it. `context`, when given, is a
        bracketed engine note appended as a user message first (the
        kickoff, a silence nudge, the silence closing — the wording the
        model speaks is always its own, from the prompt).

        Returns "completed" | "interrupted" | "failed" | "guarded".
        Barge-in (A9): if the customer starts speaking while the turn's
        audio is pumping, the frame pump stops at the next frame
        boundary, the reply's unsent sentences are abandoned, the
        transport is asked to flush its buffered audio, and the turn
        is recorded with ONLY the words actually spoken, flagged
        interrupted — the model's end_call action in such a turn is
        discarded, because the customer has taken the floor. Partials
        outside a pump window do not interrupt (see
        note_speech_activity).
        Empty end_call: if the model calls end_call with no spoken
        text, the turn does not end silently — `_closing_retry`
        runs one retry turn for the closing line, and if that also
        comes back empty the deterministic fallback closing is spoken
        before the call ends (see the end_call branch below).
        End-call net (stall fix, Oct 26, 2026): a completed normal
        turn whose text IS an approved closing (prompt CLOSINGS, or a
        deterministic fallback closing) ends the call even when the
        model forgets the end_call tool — calls 1-2 of Oct 26 ended as
        call_completed after a spoken closing because nothing ended
        them. The net never fires on interrupted turns, on turns with
        a context (kickoff / silence / retry), or on non-closing text.
        Turn-loop tracing: every turn records stage timings into
        self.turn_timings (see __init__) and logs a one-line summary
        at turn end, so the next stall's stage is measured, not
        guessed."""
        if self._agent_turns >= self._max_turns:
            self._end("max_turns")
            return "guarded"
        if self._elapsed() > self._max_duration_s:
            self._end("max_duration")
            return "guarded"
        if context:
            self.messages.append({"role": "user", "content": context})

        # Tracing record for THIS turn (the closing retry below is a
        # nested _take_turn: it gets its own record, and this one
        # resumes when the retry returns).
        timing: dict = {"turn": self._agent_turns,
                        "context": context or "reply",
                        "start": round(self._elapsed(), 2),
                        "sentences": []}
        _prev_timing = getattr(self, "_cur_timing", None)
        self._cur_timing = timing
        self._turn_first_frame_at = None
        _turn_start_mono = time.monotonic()
        self._trace("turn_start", turn=timing["turn"],
                    context=timing["context"])

        self._interrupt.clear()
        self._speaking = True
        loop = asyncio.get_running_loop()
        tokens: asyncio.Queue = asyncio.Queue()

        def _produce() -> None:
            try:
                full, tool, usage = stream_chat(
                    self._api_key, self._model, list(self.messages),
                    lambda t: loop.call_soon_threadsafe(
                        tokens.put_nowait, ("token", t)))
                loop.call_soon_threadsafe(
                    tokens.put_nowait, ("end", full, tool, usage))
            except Exception as e:
                loop.call_soon_threadsafe(tokens.put_nowait,
                                          ("error", type(e).__name__))

        producer = asyncio.create_task(asyncio.to_thread(_produce))
        interrupt_wait = asyncio.ensure_future(self._interrupt.wait())
        chunker = SentenceChunker()

        async def _speak_timed(sentence: str):
            """_speak with tracing: per-sentence TTS stage timings
            on this turn's record (start, first pumped frame, end,
            frames, status). The pump reports its first frame via
            _pump_first_frame_at, reset here per sentence."""
            s_start = self._elapsed()
            self._pump_first_frame_at = None
            status, frames = await self._speak(sentence)
            timing["sentences"].append({
                "tts": round(s_start, 2),
                "first_frame": self._pump_first_frame_at,
                "end": round(self._elapsed(), 2),
                "frames": frames, "status": status})
            return status, frames
        full_text, tool_name, error = "", "", ""
        spoken: list[str] = []       # sentences delivered (pumped, or text-standing when TTS is dead)
        inflight_text, inflight_frames = "", 0  # the sentence the pump was cut inside
        interrupted = False
        # Holding line arms on any turn that has not yet played it
        # this call (once per call): a slow later turn sounds just as
        # dead as a slow kickoff, and the Oct 26 stall was a later
        # turn. The per-turn clock below keeps it from firing
        # instantly on later turns.
        holding_armed = (not self._holding_played)
        try:
            while True:
                if self._interrupt.is_set():
                    interrupted = True
                    break
                # While no audio has been pumped yet, the token wait
                # carries the holding threshold as its timeout. The
                # timer firing means the greeting is not ready, so the
                # acknowledgement is spoken instead of dead air. It
                # never overlaps the greeting (this loop speaks
                # strictly in sequence) and never delays a ready one
                # (the timer only fires while nothing was pumped).
                _hold_timeout = None
                if (holding_armed and not spoken
                        and self._turn_first_frame_at is None
                        and self.tts_status != "failed"
                        and not self._send_dead):
                    _hold_timeout = max(
                        0.0, HOLDING_LINE_AFTER_S
                        - (time.monotonic() - _turn_start_mono))
                get_task = asyncio.ensure_future(tokens.get())
                done, _pending = await asyncio.wait(
                    {get_task, interrupt_wait},
                    timeout=_hold_timeout,
                    return_when=asyncio.FIRST_COMPLETED)
                if _hold_timeout is not None and not done:
                    # Threshold reached, no token and no barge-in: speak
                    # the holding line once (tokens stay queued for the
                    # next wait), then resume waiting for the greeting.
                    get_task.cancel()
                    try:
                        await get_task
                    except asyncio.CancelledError:
                        pass
                    _h_status, _h_frames = await self._speak(
                        holding_line(self.language))
                    self._holding_played = True
                    holding_armed = False
                    if _h_status == "interrupted":
                        interrupted = True
                        break
                    continue
                if get_task not in done:
                    get_task.cancel()
                    interrupted = True
                    break
                event = get_task.result()
                if event[0] == "token":
                    if "llm_first_token" not in timing:
                        timing["llm_first_token"] = round(
                            self._elapsed(), 2)
                        self._trace("llm_first_token",
                                    turn=timing["turn"])
                    for sentence in chunker.feed(event[1]):
                        status, frames = await _speak_timed(sentence)
                        if status == "interrupted":
                            inflight_text, inflight_frames = sentence, frames
                            interrupted = True
                            break
                        spoken.append(sentence)
                    if interrupted:
                        break
                elif event[0] == "end":
                    full_text, tool_name = event[1], event[2]
                    timing["llm_end"] = round(self._elapsed(), 2)
                    timing["tool"] = tool_name
                    self._trace("llm_end", turn=timing["turn"],
                                tool=tool_name)
                    # Accumulate this turn's token usage for the LLM
                    # leg cost (cached tokens are a subset of prompt
                    # tokens — priced at the cached rate, not double
                    # counted; see app.costing).
                    _usage = event[3] if len(event) > 3 else {}
                    if isinstance(_usage, dict):
                        try:
                            self._llm_usage["prompt_tokens"] += int(
                                _usage.get("prompt_tokens") or 0)
                            self._llm_usage["completion_tokens"] += int(
                                _usage.get("completion_tokens") or 0)
                            _details = _usage.get(
                                "prompt_tokens_details") or {}
                            self._llm_usage["cached_tokens"] += int(
                                _details.get("cached_tokens") or 0)
                        except (TypeError, ValueError):
                            pass
                    for sentence in chunker.flush():
                        status, frames = await _speak_timed(sentence)
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
            self._trace_turn_end(timing, "failed", _prev_timing)
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
            self._trace_turn_end(timing, "interrupted", _prev_timing)
            return "interrupted"

        text = (full_text or "").strip()
        if text:
            turn = {"speaker": "agent", "text": text,
                    "offset_s": round(self._elapsed(), 2)}
            self.turns.append(turn)
            stt_module.append_turn(self.session_id, turn)
            self.messages.append({"role": "assistant", "content": text})
            self._agent_turns += 1
        # End-call net (see docstring): a completed normal turn whose
        # text IS an approved closing ends the call even when the
        # model forgot the end_call tool. Context turns (kickoff /
        # silence / retry) never fire it — their endings are owned by
        # their own paths.
        if context is None and tool_name != "end_call" and text:
            matched = is_closing_text(text, self._closings)
            if matched:
                self._trace("end_call_net", matched=matched[:60])
                self._end("agent_end_call")
                try:
                    await self._close_stream()
                except Exception:
                    pass
                self._trace_turn_end(timing, "completed", _prev_timing)
                return "completed"
        if tool_name == "end_call":
            if not text:
                # The model decided to end but spoke no closing: never
                # cut silently after the customer has spoken.
                if not _closing_retry:
                    turns_before = len(self.turns)
                    rec = await self._take_turn(
                        context=CLOSING_RETRY_NOTE, _closing_retry=True)
                    if rec == "interrupted":
                        self._trace_turn_end(timing, "interrupted",
                                             _prev_timing)
                        return "interrupted"
                    if self._ended:
                        self._trace_turn_end(timing, "completed",
                                             _prev_timing)
                        return "completed"
                    if len(self.turns) > turns_before:
                        # The retry spoke a closing without the tool:
                        # the closing was spoken, so end now.
                        self._end("agent_end_call")
                        try:
                            await self._close_stream()
                        except Exception:
                            pass
                        self._trace_turn_end(timing, "completed",
                                             _prev_timing)
                        return "completed"
                # The retry also came back empty (or this IS the
                # retry): speak the deterministic fallback closing.
                # If the customer barges in on it, they have the
                # floor — the call continues instead of ending.
                if await self._speak_deterministic_closing() == "interrupted":
                    self._trace_turn_end(timing, "interrupted",
                                         _prev_timing)
                    return "interrupted"
                # "spoken", or "failed" (TTS dead — nothing more can
                # be spoken): end the call either way.
            self._end("agent_end_call")
            try:
                await self._close_stream()
            except Exception:
                pass
        self._trace_turn_end(timing, "completed", _prev_timing)
        return "completed"

    async def _speak_deterministic_closing(self) -> str:
        """Speak the neutral fallback closing and record it as a turn.
        Returns "spoken" | "interrupted" | "failed". Only the
        last-resort path uses it (an empty end_call whose LLM retry
        also produced nothing speakable); the prompt's own closing
        always wins when the model speaks one. An interrupted
        fallback records no turn — the customer took the floor mid
        -closing, so the words were not heard in full."""
        text = fallback_closing(self.language)
        status, _frames = await self._speak(text)
        if status != "spoken":
            return status
        turn = {"speaker": "agent", "text": text,
                "offset_s": round(self._elapsed(), 2)}
        self.turns.append(turn)
        stt_module.append_turn(self.session_id, turn)
        self.messages.append({"role": "assistant", "content": text})
        self._agent_turns += 1
        return "spoken"

    async def _speak(self, sentence: str) -> tuple[str, int]:
        """Speak one sentence into the stream, frame by frame.
        Returns (status, frames_sent):

        - "spoken" — the whole sentence was pumped.
        - "interrupted" — barge-in landed: the pump stopped at a frame
          boundary (frames_sent says how much of the sentence the
          callee heard; 0 when the cut came before the first frame,
          e.g. during synthesis).
        - "failed" — the audio could not be pumped (TTS leg dead or
          the stream send died); per the A8 rule the sentence's TEXT
          still stands as delivered in the transcript — only the
          audio is missing, and the leg records say so.

        Path (delay fix, Oct 26, 2026): streaming TTS first when
        enabled — μ-law chunks arrive while the sentence is still
        being generated, so first audio lands in ~0.95 s instead of
        waiting for the whole REST synthesis (~1.7 s). A streaming
        failure BEFORE any frame falls back to the REST synthesis for
        this sentence (invisible to the caller); after two consecutive
        pre-audio streaming failures the call stays on REST. A
        streaming failure AFTER audio was heard returns "interrupted"
        (the turn is flagged, not silently completed) and disables
        streaming for the call — replaying the sentence via REST
        would double-speak its heard head.
        """
        if self.tts_status == "failed" or self._send_dead:
            return "failed", 0
        if self._interrupt.is_set():
            return "interrupted", 0
        # Bulbul bills the full synthesis request, pumped or cut.
        self._tts_chars += len(sentence)
        if settings.SARVAM_TTS_STREAMING and not self._tts_streaming_dead:
            status, frames = await self._speak_streaming(sentence)
            if status == "spoken":
                self._tts_stream_failures = 0
                return status, frames
            if status == "interrupted":
                return status, frames
            if status == "failed":
                # The transport send died mid-pump: nothing more can
                # be spoken on this call.
                return status, frames
            # "source_failed": the WS path broke. No frame yet means
            # REST can still speak the whole sentence; frames already
            # heard mean the turn is cut, not replayed.
            if frames == 0:
                self._tts_stream_failures += 1
                if self._tts_stream_failures >= 2:
                    self._tts_streaming_dead = True
            else:
                self._tts_streaming_dead = True
                return "interrupted", frames
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
        return await self._pump_audio(_async_one(audio))

    async def _tts_ws_ensure(self):
        """The call's Bulbul streaming connection, opened on first
        use and configured once (language + speaker + mulaw). Raises
        on connect/config failure — nothing is stored then, so the
        next sentence retries from scratch (and the caller falls
        back to REST for this one)."""
        if self._tts_ws is not None:
            return self._tts_ws
        from websockets.asyncio.client import connect as _ws_connect

        base = (settings.SARVAM_TTS_WS_URL
                or settings.SARVAM_BASE_URL.replace("https://", "wss://")
                .replace("http://", "ws://")).rstrip("/")
        url = (f"{base}/text-to-speech/ws"
               f"?model={settings.SARVAM_TTS_MODEL}"
               f"&send_completion_event=true")
        ws = await _ws_connect(
            url,
            additional_headers={"api-subscription-key": self._api_key},
            open_timeout=10)
        try:
            await ws.send(json.dumps({"type": "config", "data": {
                "language_code": self.language, "speaker": self._speaker,
                "pace": 1.0, "speech_sample_rate": 8000,
                "min_buffer_size": 50, "output_audio_codec": "mulaw"}}))
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass
            raise
        self._tts_ws = ws
        return ws

    async def _tts_ws_drop(self) -> None:
        """Drop the call's streaming connection (best-effort close).
        Called whenever a sentence ends cut or broken — never on a
        normally completed sentence, which is what makes reuse safe."""
        ws, self._tts_ws = self._tts_ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def _speak_streaming(self, sentence: str) -> tuple[str, int]:
        """Speak one sentence via the streaming TTS path. Returns
        ("spoken" | "interrupted" | "failed" | "source_failed",
        frames) — see _pump_audio for the status contract. Any WS
        protocol failure surfaces as "source_failed" with the frames
        pumped so far (0 when nothing was heard yet). A sentence that
        does not complete normally drops the shared connection here
        (see _tts_ws) — the pump's own aclose never touches it, so
        the drop decision lives in exactly this one place."""
        status, frames = await self._pump_audio(
            self._stream_tts_chunks(sentence))
        if status != "spoken":
            await self._tts_ws_drop()
        return status, frames

    async def _stream_tts_chunks(self, sentence: str):
        """Yield μ-law 8 kHz chunks for one sentence from the call's
        shared Bulbul streaming WebSocket (live-proven Oct 26, 2026:
        config with output_audio_codec "mulaw" returns audio/mulaw
        chunks, first chunk ~0.95 s after connect; a second text on
        the same connection returns audio + its own completion
        event, so reuse is safe for completed sentences). Raises on
        any protocol failure (unknown content type, server error
        event, connect failure, quiet-timeout before the first chunk,
        absolute timeout) — the caller falls back to REST. Ends on
        the server's completion event (send_completion_event=true),
        or on a 3 s quiet gap after audio started (the backstop:
        chunks stream continuously once generation starts, so a 3 s
        gap means done or stalled — stalling the turn indefinitely
        is worse than ending the sentence). μ-law is 1 byte per
        sample, so chunk boundaries are always sample-clean — no
        framing work. This generator never closes the connection
        itself — _speak_streaming owns the drop decision."""
        ws = await self._tts_ws_ensure()
        await ws.send(json.dumps({"type": "text", "data": {
            "text": sentence}}))
        await ws.send(json.dumps({"type": "flush"}))
        deadline = time.monotonic() + 30.0
        got_audio = False
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("tts_stream_timeout")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            except asyncio.TimeoutError:
                if got_audio:
                    return
                raise
            try:
                msg = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype == "audio":
                data = msg.get("data") or {}
                if data.get("content_type") != "audio/mulaw":
                    raise ValueError(
                        "unexpected_tts_stream_codec: "
                        f"{data.get('content_type')}")
                chunk = base64.b64decode(data.get("audio") or "")
                if chunk:
                    got_audio = True
                    yield chunk
            elif mtype == "event":
                # Completion event: generation finished.
                return
            elif mtype == "error":
                raise ValueError("tts_stream_error")

    async def _pump_audio(self, chunks) -> tuple[str, int]:
        """Pump μ-law 8 kHz bytes from an async iterable of chunks,
        160 bytes (20 ms) per frame — the pacing _send_audio assumes.
        Returns (status, frames):

        - "spoken" — every chunk pumped (plus a short tail frame).
        - "interrupted" — barge-in landed: the pump stopped at a frame
          boundary (frames says how much the callee heard).
        - "failed" — the transport send died (_send_dead set).
        - "source_failed" — the CHUNK SOURCE broke (streaming TTS
          failure); frames already pumped are counted, the source is
          closed best-effort, and the transport is NOT marked dead —
          the caller decides the fallback.

        Barge-in arms only inside this pump window (see
        note_speech_activity): partials outside it cannot cut.
        """
        frames = 0
        buf = bytearray()
        self._pumping = True
        self._pump_first_frame_at = None
        try:
            try:
                async for chunk in chunks:
                    if self._interrupt.is_set():
                        self._tts_frames += frames
                        return "interrupted", frames
                    if chunk:
                        buf += chunk
                    while len(buf) >= 160:
                        if self._interrupt.is_set():
                            self._tts_frames += frames
                            return "interrupted", frames
                        try:
                            await self._send_audio(bytes(buf[:160]))
                        except Exception:
                            self._send_dead = True
                            self._tts_frames += frames
                            return "failed", frames
                        del buf[:160]
                        frames += 1
                        if frames == 1:
                            _now = round(self._elapsed(), 2)
                            if self._first_audio_s is None:
                                self._first_audio_s = _now
                            if self._pump_first_frame_at is None:
                                self._pump_first_frame_at = _now
                            if self._turn_first_frame_at is None:
                                self._turn_first_frame_at = _now
            except Exception:
                # Chunk-source failure (streaming TTS broke): count
                # what was pumped, close the source, report it — the
                # transport itself is fine.
                try:
                    await chunks.aclose()
                except Exception:
                    pass
                self._tts_frames += frames
                return "source_failed", frames
            if buf:
                if self._interrupt.is_set():
                    self._tts_frames += frames
                    return "interrupted", frames
                try:
                    await self._send_audio(bytes(buf))
                except Exception:
                    self._send_dead = True
                    self._tts_frames += frames
                    return "failed", frames
                frames += 1
                if frames == 1:
                    _now = round(self._elapsed(), 2)
                    if self._first_audio_s is None:
                        self._first_audio_s = _now
                    if self._pump_first_frame_at is None:
                        self._pump_first_frame_at = _now
                    if self._turn_first_frame_at is None:
                        self._turn_first_frame_at = _now
        finally:
            self._pumping = False
        self._tts_frames += frames
        return "spoken", frames
