"""Application configuration — all values come from the environment (.env).

Provider keys (Vapi / Plivo / Sarvam) are placeholders in .env.example.
The platform boots and is fully testable WITHOUT real keys: telephony
runs in stub/log mode (TELEPHONY_MODE=stub) until real keys are supplied.
"""
import os


class Settings:
    # Database: PostgreSQL in Docker; SQLite file is the native-dev fallback.
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "sqlite:///./voice_platform.db"
    )
    # Auth (demo-grade JWT; replace SECRET_KEY in any real deployment)
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-only-change-me")
    JWT_ALG: str = "HS256"
    JWT_EXPIRE_MIN: int = int(os.getenv("JWT_EXPIRE_MIN", "720"))

    # Telephony / AI providers — stub mode until keys are provided
    TELEPHONY_MODE: str = os.getenv("TELEPHONY_MODE", "stub")  # stub | managed | self_hosted
    VAPI_API_KEY: str = os.getenv("VAPI_API_KEY", "")
    VAPI_DEFAULT_MODEL: str = os.getenv("VAPI_DEFAULT_MODEL", "")
    VAPI_DEFAULT_VOICE: str = os.getenv("VAPI_DEFAULT_VOICE", "")
    VAPI_PHONE_NUMBER_ID: str = os.getenv("VAPI_PHONE_NUMBER_ID", "")
    # Vapi server credential ID (webhook auth): the ID of a credential
    # in the tenant's Vapi account whose token equals WEBHOOK_SECRET.
    # When set, create-call payloads reference it on the assistant's
    # server object (credentialId) so Vapi signs server messages with
    # OUR credential — without it, Vapi falls back to the ORGANIZATION's
    # default server credential and our webhook rejects every genuine
    # message (live-proven 2026-10-23). An ID, not a secret.
    VAPI_SERVER_CREDENTIAL_ID: str = os.getenv("VAPI_SERVER_CREDENTIAL_ID", "")
    # Vapi voice profiles (A6): per-language TTS overrides of the registry
    # defaults in app.providers (11labs voice IDs — identifiers, not
    # secrets). Empty = use the shipped defaults.
    VAPI_EN_VOICE_PROVIDER: str = os.getenv("VAPI_EN_VOICE_PROVIDER", "")
    VAPI_EN_TTS_MODEL: str = os.getenv("VAPI_EN_TTS_MODEL", "")
    VAPI_EN_VOICE_ID_FEMALE: str = os.getenv("VAPI_EN_VOICE_ID_FEMALE", "")
    VAPI_EN_VOICE_ID_MALE: str = os.getenv("VAPI_EN_VOICE_ID_MALE", "")
    VAPI_HI_VOICE_PROVIDER: str = os.getenv("VAPI_HI_VOICE_PROVIDER", "")
    VAPI_HI_VOICE_ID_FEMALE: str = os.getenv("VAPI_HI_VOICE_ID_FEMALE", "")
    VAPI_HI_TTS_MODEL: str = os.getenv("VAPI_HI_TTS_MODEL", "")
    VAPI_HI_VOICE_ID_MALE: str = os.getenv("VAPI_HI_VOICE_ID_MALE", "")
    VAPI_BN_VOICE_PROVIDER: str = os.getenv("VAPI_BN_VOICE_PROVIDER", "")
    VAPI_BN_TTS_MODEL: str = os.getenv("VAPI_BN_TTS_MODEL", "")
    VAPI_BN_VOICE_ID_FEMALE: str = os.getenv("VAPI_BN_VOICE_ID_FEMALE", "")
    VAPI_BN_VOICE_ID_MALE: str = os.getenv("VAPI_BN_VOICE_ID_MALE", "")
    # Vapi transcriber profiles: per-language STT (Deepgram via Vapi)
    # overrides of the registry defaults in app.providers. Bengali ships
    # nova-3 — Vapi's nova-2 language enum has no Bengali (live-proven
    # 2026-10-23: bn placements were rejected HTTP 400). Empty = shipped
    # defaults.
    VAPI_EN_TRANSCRIBER_PROVIDER: str = os.getenv("VAPI_EN_TRANSCRIBER_PROVIDER", "")
    VAPI_EN_TRANSCRIBER_MODEL: str = os.getenv("VAPI_EN_TRANSCRIBER_MODEL", "")
    VAPI_HI_TRANSCRIBER_PROVIDER: str = os.getenv("VAPI_HI_TRANSCRIBER_PROVIDER", "")
    VAPI_HI_TRANSCRIBER_MODEL: str = os.getenv("VAPI_HI_TRANSCRIBER_MODEL", "")
    VAPI_BN_TRANSCRIBER_PROVIDER: str = os.getenv("VAPI_BN_TRANSCRIBER_PROVIDER", "")
    VAPI_BN_TRANSCRIBER_MODEL: str = os.getenv("VAPI_BN_TRANSCRIBER_MODEL", "")
    # Provider API base URLs — overridable for staging / tests (the A6
    # smoke suite points these at a local fake provider; production uses
    # the defaults).
    VAPI_BASE_URL: str = os.getenv("VAPI_BASE_URL", "https://api.vapi.ai")
    PLIVO_BASE_URL: str = os.getenv("PLIVO_BASE_URL", "https://api.plivo.com")
    TWILIO_BASE_URL: str = os.getenv("TWILIO_BASE_URL", "https://api.twilio.com")
    # Public base URL of THIS app as providers see it — used to build the
    # Vapi serverUrl and the Plivo answer / status callback URLs (A6).
    # Must be reachable from the provider (a tunnel / public host in a
    # live deployment); defaults to the local dev address.
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    # Shared secret for provider webhooks (§8.1): Vapi sends it back as
    # the `x-vapi-secret` header on every server message. Empty = the
    # webhook route fails closed (503) rather than accepting unauthenticated
    # result deliveries.
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
    TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    # .env fallback for the tenant self-hosted PSTN choice (provider
    # settings "pstn" row wins when set; empty = Plivo default).
    SELF_HOSTED_PSTN_PROVIDER: str = os.getenv("SELF_HOSTED_PSTN_PROVIDER", "")
    PLIVO_AUTH_ID: str = os.getenv("PLIVO_AUTH_ID", "")
    PLIVO_AUTH_TOKEN: str = os.getenv("PLIVO_AUTH_TOKEN", "")
    EXOTEL_SID: str = os.getenv("EXOTEL_SID", "")
    EXOTEL_API_KEY: str = os.getenv("EXOTEL_API_KEY", "")
    EXOTEL_API_TOKEN: str = os.getenv("EXOTEL_API_TOKEN", "")
    EXOTEL_SUBDOMAIN: str = os.getenv("EXOTEL_SUBDOMAIN", "")
    SARVAM_API_KEY: str = os.getenv("SARVAM_API_KEY", "")
    # Sarvam Saaras STT streaming (A7): WebSocket base of the Sarvam API.
    # The STT client appends /speech-to-text-realtime/ws plus the
    # session's query params (language_code, model, encoding=mulaw,
    # sample_rate=8000, endpointing=vad, mode=transcribe) — the official
    # realtime protocol (sarvamai SDK). Overridable so tests can point
    # the STT leg at a local mock Saaras server — same seam pattern as
    # VAPI_BASE_URL / PLIVO_BASE_URL (A6). NOTE: the legacy
    # /speech-to-text/ws endpoint takes WAV/PCM only and emits no
    # partials, and saaras:v2.5 is not a valid STT model on the current
    # API — the realtime client never uses SARVAM_STT_MODEL.
    SARVAM_WS_URL: str = os.getenv("SARVAM_WS_URL", "wss://api.sarvam.ai")
    SARVAM_STT_MODEL: str = os.getenv("SARVAM_STT_MODEL", "saaras:v2.5")
    SARVAM_STT_REALTIME_MODEL: str = os.getenv(
        "SARVAM_STT_REALTIME_MODEL", "saaras:v3-realtime")
    # Sarvam HTTP API (A8): the LLM leg lives under {base}/v1/
    # (chat completions are OpenAI-compatible); Bulbul TTS lives under
    # {base}/text-to-speech with NO /v1 — the two legs use different
    # paths deliberately, authenticated with the same tenant Sarvam
    # key (api-subscription-key header). Overridable so tests can point
    # both legs at a local mock Sarvam HTTP server.
    # TTS model: bulbul:v4-flash (Oct 26, 2026 voice switch: bn Roopa
    # + hi Kavya, V4 Flash only, never V3). bulbul:v2 was deprecated
    # by Sarvam and bulbul:v3's voices were rejected by Surath's ear
    # (v3 out entirely) — do not revert this default to v2 or v3.
    SARVAM_BASE_URL: str = os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai")
    SARVAM_TTS_MODEL: str = os.getenv("SARVAM_TTS_MODEL", "bulbul:v4-flash")
    # Bulbul TTS streaming (delay fix, Oct 26, 2026): the engine speaks
    # each sentence over the /text-to-speech/ws WebSocket (μ-law 8 kHz
    # chunks, live-proven) instead of waiting for the whole REST
    # synthesis — first audio ~0.95 s instead of ~1.7 s. Any streaming
    # failure falls back to the REST path for that sentence (and after
    # two consecutive pre-audio streaming failures the call stays on
    # REST). Default ON: streaming is the delay fix, the fallback is
    # the safety. SARVAM_TTS_WS_URL overrides the WS base for tests
    # (same seam pattern as SARVAM_WS_URL).
    SARVAM_TTS_STREAMING: bool = os.getenv(
        "SARVAM_TTS_STREAMING", "true").lower() in ("1", "true", "yes")
    SARVAM_TTS_WS_URL: str = os.getenv("SARVAM_TTS_WS_URL", "")
    # Typing bed (Oct 26, 2026 — Surath: the managed/Vapi-side calls
    # carry a background typing bed that reads as "call center"; the
    # self-hosted engine reproduces the feel with a loop synthesized
    # in code, never a downloaded sample — see app.conversation).
    # Default ON: Surath asked for the sound. Bed frames never count
    # as TTS frames and never touch cost (reported separately).
    TYPING_BED_ENABLED: bool = os.getenv(
        "TYPING_BED_ENABLED", "true").lower() in ("1", "true", "yes")
    # Sarvam LLM reasoning effort (delay + cost lever, measured Oct 26,
    # 2026: reasoning_effort=low cut the RPI kickoff from 2.48 s / 274
    # completion tokens to 1.72 s / 143 with TTFT unchanged — Sarvam
    # honours the parameter). UNSET by default (= current behaviour):
    # lower reasoning can change turn quality on less-scripted agents,
    # so it is flipped on deliberately, per call deployment, never
    # silently. Accepted values are the provider's (e.g. "low").
    SARVAM_REASONING_EFFORT: str = os.getenv("SARVAM_REASONING_EFFORT", "")
    # Saaras realtime VAD tuning (delay lever, UNSET by default =
    # Sarvam's own defaults, i.e. current behaviour). Query-param names
    # are the realtime endpoint's own, confirmed from two independent
    # clients of the same endpoint (LiveKit's Sarvam plugin source and
    # Pipecat's Sarvam service): threshold = VAD activation threshold
    # (0.0-1.0), min_speech_duration_ms, silence_duration_ms (the
    # end-of-turn silence — the knob that would shorten the
    # final-to-reply gap). Any value change needs a live A/B test:
    # too-low silence splits utterances into false turns.
    SARVAM_VAD_SOT_THRESHOLD: str = os.getenv("SARVAM_VAD_SOT_THRESHOLD", "")
    SARVAM_VAD_MIN_SPEECH_MS: str = os.getenv("SARVAM_VAD_MIN_SPEECH_MS", "")
    SARVAM_VAD_MIN_SILENCE_MS: str = os.getenv(
        "SARVAM_VAD_MIN_SILENCE_MS", "")

    # Seeded demo tenant / login (created on first boot if absent)
    SEED_TENANT_NAME: str = os.getenv("SEED_TENANT_NAME", "QGBS")
    SEED_USER_EMAIL: str = os.getenv("SEED_USER_EMAIL", "demo@qgbs.example")
    SEED_USER_PASSWORD: str = os.getenv("SEED_USER_PASSWORD", "demo-password")
    SEED_USER_NAME: str = os.getenv("SEED_USER_NAME", "Demo Operator")


settings = Settings()
