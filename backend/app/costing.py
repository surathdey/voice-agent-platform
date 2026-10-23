"""Per-call cost computation (A12) — the numbers inside §8.2's
cost_breakdown, computed at finalize time in the single-writer path
(orchestrator.finalize_attempt calls finalize_breakdown).

The design contract (§8.2) has always required a cost_breakdown on
every CallResult; until A12 the stub wrote labelled zeros, the managed
path stored whatever Vapi reported, and the self-hosted paths stored
zeros. A12 puts REAL figures in, from tenant-configured RATE CARDS —
never hardcoded prices:

- The rate cards live in Provider Settings, beside the credentials
  (app.providers registry): telephony per minute on the telephony
  provider (Vapi for managed — a fallback used only when a report
  carries no cost breakdown of its own; Plivo for self-hosted), and
  STT / LLM / TTS per minute on Sarvam. Rates ship EMPTY: a tenant
  enters their contracted rates, and an unset rate honestly costs 0.
- What is metered, per leg (the basis is written into every breakdown,
  so a figure can always be traced to its inputs):
  - telephony — rate × call duration. The browser-test channel has
    no telephony leg: 0, labelled as such.
  - STT — rate × call duration (Saaras streams the call's audio for
    its whole duration), only when an STT session actually ran
    (session stt_status active/failed).
  - LLM — rate × call duration, only when the conversation engine
    ran. The Sarvam API returns NO token usage to this integration
    (the clients in app.conversation read content/tool calls only),
    so duration is the honest basis — the breakdown says so.
  - TTS — rate × synthesized audio ACTUALLY PUMPED: the engine
    counts 160-byte μ-law frames sent (20 ms each); a sentence cut
    by barge-in counts only the frames that left, never the whole
    synthesis.
- Managed (Vapi) calls keep the provider's own reported figures when
  the end-of-call report carries a cost breakdown (cost_source
  "provider") — Vapi meters its own stack. Only when a report carries
  none does the rate card step in, and then for telephony alone: the
  other legs stay 0, marked "not reported by the provider".
- The stub stays exactly as labelled since A5: zeros, mode "stub",
  simulated — simulated money is never dressed up as real, and the
  aggregates in app.routers.calls exclude stub results entirely.

Currency follows the mode — INR self-hosted, USD managed — and is
never mixed inside one breakdown.
"""
from sqlalchemy.orm import Session

from . import providers as prov

_LEGS = ("telephony", "stt", "llm", "tts")

# The rate-card fields per provider (registry entries in app.providers).
_RATE_FIELDS = {
    "vapi": {"telephony": "rate_telephony_per_min"},
    "plivo": {"telephony": "rate_telephony_per_min"},
    "sarvam": {"stt": "rate_stt_per_min", "llm": "rate_llm_per_min",
               "tts": "rate_tts_per_min"},
}


def _rate(raw) -> float:
    """A rate-card value as a float; empty/invalid honestly means 0 —
    no rate configured, no cost claimed."""
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def rate_card(db: Session, tenant_id: str) -> dict:
    """The tenant's effective per-minute rates: {vapi_telephony,
    plivo_telephony, stt, llm, tts} — tenant values, else .env, else 0."""
    out = {"vapi_telephony": 0.0, "plivo_telephony": 0.0,
           "stt": 0.0, "llm": 0.0, "tts": 0.0}
    for provider, legs in _RATE_FIELDS.items():
        values, _sources, _row = prov.resolve_fields(db, tenant_id, provider)
        for leg, field in legs.items():
            key = f"{provider}_telephony" if leg == "telephony" else leg
            out[key] = _rate(values.get(field))
    return out


def _per_min(rate: float, seconds: float) -> float:
    return round(rate * (seconds / 60.0), 4)


def finalize_breakdown(db: Session, session, outcome: dict) -> dict:
    """The cost_breakdown to store for one finalized attempt.

    Decides, in order: keep the stub's labelled zeros; keep a
    provider-reported breakdown (Vapi's own metering, stamped
    cost_source="provider" by transports.map_vapi_report); keep a
    breakdown with real figures delivered through the §8.2 receiver;
    otherwise compute from the tenant's rate card.
    """
    existing = dict(outcome.get("cost_breakdown") or {})

    if existing.get("mode") == "stub":
        return existing
    if existing.get("cost_source") == "provider":
        return existing
    if any(existing.get(k) for k in _LEGS + ("total",)):
        # Real figures arrived with the delivery itself — keep them
        # verbatim; only the source label is added.
        existing.setdefault("cost_source", "delivered")
        return existing

    mode = session.provider_mode or existing.get("mode") or "self_hosted"
    currency = "USD" if mode == "managed" else "INR"
    channel = session.channel or ""
    duration = int(outcome.get("duration_s") or 0)
    metrics = outcome.get("cost_metrics") or {}
    rates = rate_card(db, session.tenant_id)

    basis: dict[str, str] = {}
    legs: dict[str, float] = {}

    # ── Telephony ────────────────────────────────────────────────────
    tele_rate = rates["vapi_telephony"] if mode == "managed" else rates["plivo_telephony"]
    if channel == "browser-test":
        legs["telephony"] = 0.0
        basis["telephony"] = "browser channel — no telephony leg, not costed"
    else:
        legs["telephony"] = _per_min(tele_rate, duration)
        basis["telephony"] = f"rate card × call duration ({duration}s)"

    # ── STT / LLM / TTS ──────────────────────────────────────────────
    if mode == "managed":
        # Vapi meters its own stack and reports it (handled above).
        # With no report breakdown, only telephony is estimable; the
        # rest are the provider's to report, and stay absent — 0 here
        # means "not reported", never "free".
        for leg in ("stt", "llm", "tts"):
            legs[leg] = 0.0
            basis[leg] = ("inside the managed provider's own cost — "
                          "not reported for this call")
    else:
        stt_status = session.stt_status or ""
        if stt_status in ("active", "failed"):
            legs["stt"] = _per_min(rates["stt"], duration)
            basis["stt"] = (f"rate card × call duration ({duration}s) of "
                            "streamed audio"
                            + (" — the STT leg failed mid-call; the audio "
                               "streamed before the failure is still metered"
                               if stt_status == "failed" else ""))
        else:
            legs["stt"] = 0.0
            basis["stt"] = "no STT leg ran on this call — not costed"

        llm_status = session.llm_status or ""
        if llm_status == "active":
            legs["llm"] = _per_min(rates["llm"], duration)
            basis["llm"] = (f"rate card × call duration ({duration}s) — the "
                            "provider API returns no token usage to this "
                            "integration, so the LLM leg is metered on "
                            "duration")
        elif llm_status == "failed":
            legs["llm"] = 0.0
            basis["llm"] = "the LLM leg failed — no completed usage, not costed"
        else:
            legs["llm"] = 0.0
            basis["llm"] = "no LLM leg ran on this call — not costed"

        tts_frames = int(metrics.get("tts_frames") or 0)
        tts_seconds = tts_frames * 0.02  # 160-byte μ-law frames = 20 ms
        if tts_frames > 0:
            legs["tts"] = _per_min(rates["tts"], tts_seconds)
            basis["tts"] = (f"rate card × synthesized audio actually pumped "
                            f"({tts_frames} frames × 20 ms = {tts_seconds:.1f}s; "
                            "a sentence cut by barge-in counts only the "
                            "frames that were sent)")
        else:
            legs["tts"] = 0.0
            basis["tts"] = "no synthesized audio was pumped — not costed"

    total = round(sum(legs.values()), 4)
    return {
        "telephony": legs["telephony"],
        "stt": legs["stt"],
        "llm": legs["llm"],
        "tts": legs["tts"],
        "total": total,
        "currency": currency,
        "mode": mode,
        "cost_source": "rate_card",
        "channel": channel or "phone",
        "rates_per_min": {
            "telephony": tele_rate,
            "stt": rates["stt"] if mode != "managed" else 0.0,
            "llm": rates["llm"] if mode != "managed" else 0.0,
            "tts": rates["tts"] if mode != "managed" else 0.0,
        },
        "basis": basis,
    }
