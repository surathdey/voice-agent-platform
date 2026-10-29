"""Secure Box identity (Epic C, story C1).

One box = one process = one tenant = one database. That is the whole
isolation model, and it is structural, not per-query: this process
builds exactly ONE SQLAlchemy engine, bound once at import to the
box's own DATABASE_URL (see app.db), and there is no code path that
opens a second tenant's database in one request. The only engine
construction site in the application is app.db; the isolation test
(scripts/test_tenant_isolation.py) asserts that stays true.

A box knows who it is from the environment:

- BOX_TENANT_SLUG — explicit slug for this box (e.g. "qgbs", "na").
  When unset, the slug is derived from SEED_TENANT_NAME so the
  original single box keeps working with no configuration change
  ("QGBS" -> "qgbs").
- The database fingerprint reported by /api/health names the
  database only (scheme + database name, or the SQLite file
  basename). It never contains credentials, hosts, or paths, so
  operators can verify "this box = this tenant's database" from
  the health endpoint without leaking anything.

What C1 does NOT do (later stories): the catalog is the only
shared data in the design (§10) and it arrives with C2; per-tenant
telephony mode arrives with C3; roles arrive with C5. A C1 box is
fully independent: its own database holds its own tenant row,
agents, campaigns, leads, and calls.
"""
import re
from urllib.parse import urlparse

from .config import settings

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase, collapse every non-alphanumeric run to one hyphen."""
    slug = _NON_ALNUM.sub("-", (name or "").strip().lower()).strip("-")
    return slug or "tenant"


def box_tenant_slug() -> str:
    """This box's tenant slug: explicit env first, seed name fallback."""
    if settings.BOX_TENANT_SLUG.strip():
        return slugify(settings.BOX_TENANT_SLUG)
    return slugify(settings.SEED_TENANT_NAME)


def db_fingerprint(database_url: str) -> str:
    """Database identity without credentials: scheme + database name.

    Postgres: "postgres:<dbname>". SQLite: "sqlite:<file basename>".
    Anything else: the URL scheme only. Never user, password, host,
    or directory — a fingerprint must be safe to print in logs and
    on the health endpoint.
    """
    url = (database_url or "").strip()
    if url.startswith("sqlite"):
        path = url.split(":///", 1)[-1].split("?", 1)[0]
        base = path.rsplit("/", 1)[-1] or ":memory:"
        return f"sqlite:{base}"
    parsed = urlparse(url)
    scheme = parsed.scheme.split("+")[0] or "unknown"
    name = (parsed.path or "").lstrip("/") or "unknown"
    return f"{scheme}:{name}"


def box_info() -> dict:
    """Public box identity for /api/health (no secrets, no PII)."""
    return {
        "tenant_slug": box_tenant_slug(),
        "database": db_fingerprint(settings.DATABASE_URL),
        "telephony_mode": settings.TELEPHONY_MODE,
    }
