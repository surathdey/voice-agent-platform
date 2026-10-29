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
  - telephony — rate × call duration: the Vapi rate for managed, the
    Plivo rate for self-hosted Plivo, the TWILIO rate for self-hosted
    Twilio (a Twilio SID on the session selects it — the old code
    priced every self-hosted call on the Plivo rate, wrong now that
    the live pilot transport is Twilio). The browser-test channel has
    no telephony leg: 0, labelled as such. The provider-reported
    Twilio price (app.twilio_cost, read path) is separate and needs
    no rate.
  - STT — rate × call duration (Saaras streams the call's audio for
    its whole duration), only when an STT session actually ran
    (session stt_status active/failed). Saaras returns no per-call
    usage, so duration is the honest basis — the breakdown says so.
  - LLM — TOKENS from the usage Sarvam streams (prompt / completion /
    cached, turns + classifier call), priced at the tenant's token
    rates; reasoning tokens bill as output inside completion. When
    no usage was captured for a call, the per-minute rate × call
    duration is the fallback, and the breakdown says so. (The old
    "API returns no token usage" claim was wrong — live-proven
    Oct 26, 2026: usage arrives on streamed chunks even without
    stream_options, and app.conversation now captures it.)
  - TTS — CHARACTERS in synthesis requests (Bulbul bills per
    character and returns no usage): the engine counts every request
    it makes, including sentences cut by barge-in — the full request
    was synthesized even when only part was pumped. When no
    character count was captured, the fallback is rate × synthesized
    audio ACTUALLY PUMPED (160-byte μ-law frames, 20 ms each).
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
    "twilio": {"telephony": "rate_telephony_per_min"},
    "sarvam": {"stt": "rate_stt_per_min", "llm": "rate_llm_per_min",
               "tts": "rate_tts_per_min",
               "llm_input": "rate_llm_input_per_1m",
               "llm_output": "rate_llm_output_per_1m",
               "llm_cached": "rate_llm_cached_per_1m",
               "tts_chars": "rate_tts_per_1k_chars"},
}


def _rate(raw) -> float:
    """A rate-card value as a float; empty/invalid honestly means 0 —
    no rate configured, no cost claimed."""
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def rate_card(db: Session, tenant_id: str) -> dict:
    """The tenant's effective rates — tenant values, else .env, else 0:
    per-minute telephony (vapi / plivo / twilio), per-minute STT / LLM /
    TTS fallbacks, and per-token / per-character LLM / TTS rates."""
    out = {"vapi_telephony": 0.0, "plivo_telephony": 0.0,
           "twilio_telephony": 0.0,
           "stt": 0.0, "llm": 0.0, "tts": 0.0,
           "llm_input": 0.0, "llm_output": 0.0, "llm_cached": 0.0,
           "tts_chars": 0.0}
    for provider, legs in _RATE_FIELDS.items():
        values, _sources, _row = prov.resolve_fields(db, tenant_id, provider)
        for leg, field in legs.items():
            key = f"{provider}_telephony" if leg == "telephony" else leg
            out[key] = _rate(values.get(field))
    return out


def _per_min(rate: float, seconds: float) -> float:
    return round(rate * (seconds / 60.0), 4)


def _sum_usage(metrics: dict) -> dict:
    """Total LLM token usage for a call: the engine's streamed turns
    plus the classifier call (merged at the finalize call site under
    llm_usage_classify). Missing keys count as 0; cached tokens are
    a subset of prompt tokens, priced separately, never double
    counted (see finalize_breakdown)."""
    total = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    for key in ("llm_usage", "llm_usage_classify"):
        usage = metrics.get(key) or {}
        if not isinstance(usage, dict):
            continue
        for field in total:
            try:
                total[field] += int(usage.get(field) or 0)
            except (TypeError, ValueError):
                pass
    return total


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
    # Self-hosted Twilio calls price on the Twilio rate, not Plivo's:
    # a Twilio Call SID on the session (starts with "CA") selects it.
    # The provider-reported Twilio price (app.twilio_cost, read path)
    # is separate and needs no rate — it lands as the "twilio" entry.
    if mode == "managed":
        tele_rate = rates["vapi_telephony"]
        tele_label = "Vapi rate card"
    elif (session.provider_call_id or "").startswith("CA"):
        tele_rate = rates["twilio_telephony"]
        tele_label = "Twilio rate card"
    else:
        tele_rate = rates["plivo_telephony"]
        tele_label = "Plivo rate card"
    if channel == "browser-test":
        legs["telephony"] = 0.0
        basis["telephony"] = "browser channel — no telephony leg, not costed"
    else:
        legs["telephony"] = _per_min(tele_rate, duration)
        basis["telephony"] = (f"{tele_label} × call duration ({duration}s)")

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
        usage = _sum_usage(metrics)
        token_rates = (rates["llm_input"], rates["llm_output"],
                       rates["llm_cached"])
        if llm_status == "active" and any(token_rates) and (
                usage["prompt_tokens"] or usage["completion_tokens"]):
            # Token metering: uncached prompt tokens at the input
            # rate, cached prompt tokens at the cached rate,
            # completion tokens (reasoning included) at the output
            # rate. Cached is a subset of prompt — never added twice.
            uncached = max(0, usage["prompt_tokens"]
                           - usage["cached_tokens"])
            legs["llm"] = round(
                uncached * rates["llm_input"] / 1_000_000
                + usage["cached_tokens"] * rates["llm_cached"] / 1_000_000
                + usage["completion_tokens"] * rates["llm_output"]
                / 1_000_000, 4)
            basis["llm"] = (
                f"provider token usage × token rate card "
                f"({usage['prompt_tokens']} prompt incl. "
                f"{usage['cached_tokens']} cached, "
                f"{usage['completion_tokens']} completion incl. reasoning)")
        elif llm_status == "active":
            legs["llm"] = _per_min(rates["llm"], duration)
            basis["llm"] = (f"rate card × call duration ({duration}s) — "
                            "no token usage was captured for this call, "
                            "so the LLM leg is metered on duration")
        elif llm_status == "failed":
            legs["llm"] = 0.0
            basis["llm"] = "the LLM leg failed — no completed usage, not costed"
        else:
            legs["llm"] = 0.0
            basis["llm"] = "no LLM leg ran on this call — not costed"

        tts_frames = int(metrics.get("tts_frames") or 0)
        tts_seconds = tts_frames * 0.02  # 160-byte μ-law frames = 20 ms
        tts_chars = int(metrics.get("tts_chars") or 0)
        if tts_chars > 0 and rates["tts_chars"] > 0:
            legs["tts"] = round(tts_chars * rates["tts_chars"] / 1000, 4)
            basis["tts"] = (
                f"Bulbul bills per character: rate card × {tts_chars} "
                f"characters in synthesis requests (a sentence cut by "
                f"barge-in counts in full — the whole request was "
                f"synthesized, even when only {tts_seconds:.1f}s was "
                f"pumped)")
        elif tts_frames > 0:
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
        "rates_usage": {
            "llm_input_per_1m": rates["llm_input"]
            if mode != "managed" else 0.0,
            "llm_output_per_1m": rates["llm_output"]
            if mode != "managed" else 0.0,
            "llm_cached_per_1m": rates["llm_cached"]
            if mode != "managed" else 0.0,
            "tts_per_1k_chars": rates["tts_chars"]
            if mode != "managed" else 0.0,
        },
        "basis": basis,
    }
