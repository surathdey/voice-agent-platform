"""STT leg (A7) — Sarvam Saaras streaming inside the Plivo media stream.

Design §11 row A7 + the Sarvam mechanics from the design doc: Saaras
STT is ONE WebSocket streaming connection per call — audio flows in,
partial + final transcripts flow out. This module is that leg, isolated
behind a small surface so A8 can add the LLM/TTS legs beside it:

- `saaras_language()` — the language the session is opened with,
  resolved from the agent's language rule for the lead (transports
  .resolve_language) and normalised to the xx-IN codes Saaras takes
  (bn-IN / hi-IN / en-IN). Mid-call language handling is the LLM leg's
  job (A8) — here the opening language is opened and RECORDED.
- `SaarasSttSession` — the per-call client. Plivo media frames (base64
  μ-law 8 kHz payloads, decoded by the stream handler) are forwarded as
  Saaras audio messages; partial transcripts update the live view;
  final segments become transcript turns ({speaker: customer, text,
  offset_s}) and are persisted to the CallSession AS THEY ARRIVE, so
  the Calls UI shows the transcript growing during the call.
- `LIVE_STREAMS` — the in-memory live view per in-flight session
  (current partial + finals + status). Volatile by design: the durable
  record is the session row (stt_status / stt_language / stt_detail +
  the persisted final turns).

The Sarvam endpoint comes from configuration: the tenant's A4 Sarvam
API key (get_provider_config — never logged, never serialised) and the
SARVAM_WS_URL base (config) — the same override seam the A6 transports
use, which is how the smoke suite points this leg at a local mock
Saaras server. No key → the leg does not exist for the call:
stt_status = not_configured and the stream behaves exactly like the
A6 spike.

Failure honesty: if the STT session fails to open or drops mid-call,
the call CONTINUES — audio is dropped, the failure is recorded on the
session (stt_status = failed + a reason made of an exception class /
short label only, never a provider body or a secret), and the finalize
outcome marks the transcript partial. The stream handler never crashes
because STT did.
"""
import asyncio
import base64
import json
import time

from .config import settings

try:  # websockets is a declared backend dependency (A7)
    from websockets.asyncio.client import connect as _ws_connect
except Exception:  # pragma: no cover — import shape fallback
    from websockets import connect as _ws_connect

# Cap on frames buffered while the Saaras connection is still opening
# (Plivo media starts immediately; a frame is ~20 ms of audio, so this
# is roughly 8 s — anything older is dropped, never queued forever).
_BUFFER_CAP = 400
_CONNECT_TIMEOUT_S = 15


def saaras_language(resolved: str) -> str:
    """Normalise a resolved language (the agent rule's value, e.g.
    "bn-IN" / "hi" / "en-IN") to the code Saaras sessions open with."""
    code = (resolved or "").strip() or "hi-IN"
    if "-" not in code:
        code = f"{code}-IN"
    lang, _, region = code.partition("-")
    return f"{lang.lower()}-{region.upper()}"


def stt_config(tenant_id: str, db) -> tuple[bool, str]:
    """(configured, api_key) for the tenant's Sarvam provider (A4).
    INTERNAL — the key is used to open the WebSocket and is never
    logged or returned to any API caller."""
    from . import providers as prov

    cfg = prov.get_provider_config(tenant_id, "self_hosted", db=db)
    sarvam = cfg["providers"].get("sarvam") or {}
    fields = sarvam.get("fields") or {}
    return bool(sarvam.get("configured")), fields.get("api_key", "") or ""


# ── Live view (in-memory, per in-flight session) ─────────────────────────

LIVE_STREAMS: dict[str, dict] = {}


def new_live_view(session_id: str, language: str, status: str) -> dict:
    view = {"session_id": session_id, "language": language, "status": status,
            "detail": "", "partial": "", "finals": []}
    LIVE_STREAMS[session_id] = view
    return view


def end_live_view(session_id: str) -> None:
    LIVE_STREAMS.pop(session_id, None)


def live_partial(session_id: str) -> str:
    view = LIVE_STREAMS.get(session_id)
    return (view or {}).get("partial", "") or ""


# ── Persistence (short-lived sessions; the durable STT record) ──────────

def _write(session_id: str, mutate) -> None:
    """Apply `mutate(session)` to the CallSession row and commit, in a
    fresh DB session. Best-effort with small retries (SQLite writer
    contention in dev): a lost incremental write never loses the call —
    the finalize outcome carries the in-memory finals regardless."""
    from .db import SessionLocal
    from .models import CallSession

    for attempt in range(5):
        db = SessionLocal()
        try:
            s = db.get(CallSession, session_id)
            if s is None:
                return
            mutate(s)
            db.commit()
            return
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            if attempt == 4:
                return
            time.sleep(0.05)
        finally:
            db.close()


def record_stt_status(session_id: str, status: str, language: str = "",
                      detail: str = "") -> None:
    def _m(s):
        s.stt_status = status
        if language:
            s.stt_language = language
        s.stt_detail = detail[:500]
    _write(session_id, _m)


def append_turn(session_id: str, turn: dict) -> None:
    """Persist one transcript turn as it happens (the Calls UI reads
    session.transcript — turns become visible mid-call). Used by the
    STT leg for customer finals and by the A8 conversation engine for
    agent turns, so the stored transcript interleaves in real order."""
    def _m(s):
        turns = list(s.transcript or [])
        turns.append(turn)
        s.transcript = turns
    _write(session_id, _m)


def append_final_turn(session_id: str, turn: dict) -> None:
    """Persist one final STT segment as it arrives (A7 name for the
    customer side of append_turn)."""
    append_turn(session_id, turn)


# ── The Saaras streaming client (one connection per call) ───────────────

def _audio_message(mulaw: bytes) -> str:
    """One Saaras audio message: the Plivo frame (μ-law, 8 kHz) base64'd
    into the streaming envelope, format declared alongside."""
    return json.dumps({"audio": {
        "data": base64.b64encode(mulaw).decode(),
        "encoding": "audio/x-mulaw",
        "sample_rate": 8000,
    }})


def _extract_transcript(raw) -> tuple[str, bool] | None:
    """Pull (text, is_final) out of one Saaras message, tolerating the
    documented envelope ({"type": "data", "data": {transcript, is_final}})
    and a flat shape. Returns None for non-transcript messages; raises
    ValueError on a provider error message."""
    try:
        msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except Exception:
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("type") == "error":
        data = msg.get("data") or {}
        raise ValueError(data.get("error_code") or "provider_error")
    data = msg.get("data") if isinstance(msg.get("data"), dict) else msg
    text = data.get("transcript")
    if not isinstance(text, str):
        return None
    return text, bool(data.get("is_final"))


class SaarasSttSession:
    """One Saaras streaming session for one call.

    Lifecycle: construct → start() (spawns the connection task, returns
    immediately) → feed() per media frame → stop() at stream end.
    Status transitions land in the live view AND on the session row via
    the record_stt_status callback: connecting → active | failed.
    """

    def __init__(self, session_id: str, api_key: str, language: str,
                 view: dict, on_final=None):
        self.session_id = session_id
        self.language = language
        self.view = view
        # A8: the conversation engine registers here to be told about
        # each final segment the moment it lands (it drives the reply
        # loop). Called with the turn dict, on the event loop, guarded —
        # a consumer bug must never break the STT session.
        self.on_final = on_final
        self._api_key = api_key
        base = (settings.SARVAM_WS_URL or "wss://api.sarvam.ai").rstrip("/")
        self._url = (f"{base}/speech-to-text/ws"
                     f"?language-code={language}&model={settings.SARVAM_STT_MODEL}")
        self._ws = None
        self._task: asyncio.Task | None = None
        self._buffer: list[bytes] = []
        self._t0: float | None = None
        self._stopping = False

    # status plumbing -------------------------------------------------
    def _set_status(self, status: str, detail: str = "") -> None:
        self.view["status"] = status
        self.view["detail"] = detail
        record_stt_status(self.session_id, status, self.language, detail)

    def _fail(self, detail: str) -> None:
        if self._stopping or self.view["status"] == "failed":
            return
        self._set_status("failed", detail)

    # lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self._t0 = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with _ws_connect(
                    self._url,
                    additional_headers={"api-subscription-key": self._api_key},
                    open_timeout=_CONNECT_TIMEOUT_S) as ws:
                self._ws = ws
                self._set_status("active")
                buffered, self._buffer = self._buffer, []
                for frame in buffered:
                    await ws.send(_audio_message(frame))
                async for raw in ws:
                    self._handle(raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # connect refused / TLS / provider close…
            self._fail(type(e).__name__)
        finally:
            self._ws = None

    def _handle(self, raw) -> None:
        try:
            got = _extract_transcript(raw)
        except ValueError as e:
            self._fail(f"provider error ({e})")
            return
        if got is None:
            return
        text, is_final = got
        if not text.strip():
            if is_final:
                self.view["partial"] = ""
            return
        if is_final:
            offset = round(time.monotonic() - (self._t0 or time.monotonic()), 2)
            turn = {"speaker": "customer", "text": text, "offset_s": offset}
            self.view["finals"].append(turn)
            self.view["partial"] = ""
            append_final_turn(self.session_id, turn)
            if self.on_final is not None:
                try:
                    self.on_final(turn)
                except Exception:
                    pass  # a consumer must never break the STT session
        else:
            self.view["partial"] = text

    async def feed(self, mulaw: bytes) -> None:
        """Forward one decoded Plivo media frame. While the session is
        still opening the frame is buffered; after a failure it is
        dropped — STT must never back-pressure the media stream."""
        if self.view["status"] == "active" and self._ws is not None:
            try:
                await self._ws.send(_audio_message(mulaw))
            except Exception as e:
                self._fail(f"send {type(e).__name__}")
        elif self.view["status"] == "connecting":
            if len(self._buffer) < _BUFFER_CAP:
                self._buffer.append(mulaw)

    async def stop(self) -> None:
        """End the session: cancel the connection task, close the socket.
        The recorded status is left as it stands (active stays active —
        it records that STT ran; connecting is resolved by the caller
        into 'did not open' at finalize)."""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3)
            except BaseException:
                # The cancelled connection task surfaces its own
                # CancelledError here — expected during cleanup, and it
                # must not propagate into the stream's finalize flow.
                pass
        self._ws = None
