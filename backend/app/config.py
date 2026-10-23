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
    TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    PLIVO_AUTH_ID: str = os.getenv("PLIVO_AUTH_ID", "")
    PLIVO_AUTH_TOKEN: str = os.getenv("PLIVO_AUTH_TOKEN", "")
    EXOTEL_SID: str = os.getenv("EXOTEL_SID", "")
    EXOTEL_API_KEY: str = os.getenv("EXOTEL_API_KEY", "")
    EXOTEL_API_TOKEN: str = os.getenv("EXOTEL_API_TOKEN", "")
    EXOTEL_SUBDOMAIN: str = os.getenv("EXOTEL_SUBDOMAIN", "")
    SARVAM_API_KEY: str = os.getenv("SARVAM_API_KEY", "")

    # Seeded demo tenant / login (created on first boot if absent)
    SEED_TENANT_NAME: str = os.getenv("SEED_TENANT_NAME", "QGBS")
    SEED_USER_EMAIL: str = os.getenv("SEED_USER_EMAIL", "demo@qgbs.example")
    SEED_USER_PASSWORD: str = os.getenv("SEED_USER_PASSWORD", "demo-password")
    SEED_USER_NAME: str = os.getenv("SEED_USER_NAME", "Demo Operator")


settings = Settings()
