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
    # Provider API base URLs — overridable for staging / tests (the A6
    # smoke suite points these at a local fake provider; production uses
    # the defaults).
    VAPI_BASE_URL: str = os.getenv("VAPI_BASE_URL", "https://api.vapi.ai")
    PLIVO_BASE_URL: str = os.getenv("PLIVO_BASE_URL", "https://api.plivo.com")
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
    PLIVO_AUTH_ID: str = os.getenv("PLIVO_AUTH_ID", "")
    PLIVO_AUTH_TOKEN: str = os.getenv("PLIVO_AUTH_TOKEN", "")
    EXOTEL_SID: str = os.getenv("EXOTEL_SID", "")
    EXOTEL_API_KEY: str = os.getenv("EXOTEL_API_KEY", "")
    EXOTEL_API_TOKEN: str = os.getenv("EXOTEL_API_TOKEN", "")
    EXOTEL_SUBDOMAIN: str = os.getenv("EXOTEL_SUBDOMAIN", "")
    SARVAM_API_KEY: str = os.getenv("SARVAM_API_KEY", "")
    # Sarvam Saaras STT streaming (A7): WebSocket base of the Sarvam API.
    # The STT client appends /speech-to-text/ws plus the session's
    # language/model query params. Overridable so tests can point the STT
    # leg at a local mock Saaras server — same seam pattern as
    # VAPI_BASE_URL / PLIVO_BASE_URL (A6).
    SARVAM_WS_URL: str = os.getenv("SARVAM_WS_URL", "wss://api.sarvam.ai")
    SARVAM_STT_MODEL: str = os.getenv("SARVAM_STT_MODEL", "saaras:v2.5")
    # Sarvam HTTP API (A8): chat completions (the LLM leg) and Bulbul TTS
    # live under this base — {base}/chat/completions and
    # {base}/text-to-speech, authenticated with the same tenant Sarvam
    # key (api-subscription-key header). Overridable so tests can point
    # both legs at a local mock Sarvam HTTP server.
    SARVAM_BASE_URL: str = os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai")
    SARVAM_TTS_MODEL: str = os.getenv("SARVAM_TTS_MODEL", "bulbul:v2")

    # Seeded demo tenant / login (created on first boot if absent)
    SEED_TENANT_NAME: str = os.getenv("SEED_TENANT_NAME", "QGBS")
    SEED_USER_EMAIL: str = os.getenv("SEED_USER_EMAIL", "demo@qgbs.example")
    SEED_USER_PASSWORD: str = os.getenv("SEED_USER_PASSWORD", "demo-password")
    SEED_USER_NAME: str = os.getenv("SEED_USER_NAME", "Demo Operator")


settings = Settings()
