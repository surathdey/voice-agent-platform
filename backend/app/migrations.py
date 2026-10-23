"""Boot-time migration runner — the upgrade path.

WHY THIS EXISTS (the defect): until this module landed, the schema was
created by ``Base.metadata.create_all`` (db.init_db), which creates
MISSING tables but never alters existing ones, and the numbered SQL
files in backend/migrations/ were "DDL of record" that nothing ever
executed. A database volume created by an early build therefore kept
its early shape forever: after a code upgrade the app queried columns
the old volume did not have and startup died — first seen by the
owner on his first real Docker run of the full Phase A code against
his chunk-1-era volume: ``psycopg2.errors.UndefinedColumn: column
tenants.timezone does not exist`` (migration 008). Every prior
verification had used FRESH databases (create_all makes those in the
final shape), so the upgrade path was never exercised. This runner
closes the hole: an existing database is migrated IN PLACE at boot.

APPROACH — marker introspection + bookkeeping, never blind replay:

* Each step mirrors one numbered file in backend/migrations/ (the
  files remain the DDL of record; the steps here are their executable
  twins, with identical numbering and intent).
* ``schema_migrations`` records what has been applied (version PK,
  name, applied_at).
* A database WITHOUT that table can be at ANY historical shape, so a
  step is never applied just because it is unrecorded: first its
  MARKER is checked against the live schema (the objects the step is
  responsible for — e.g. 008's marker is "tenants.timezone AND
  leads.next_attempt_at exist"). Marker present → the step is
  BACKFILLED into schema_migrations with no DDL. Marker absent → the
  step APPLIES.
* Every apply is itself per-object idempotent (each column is added
  only if missing), so a database in a partial state WITHIN one step
  (say timezone added by hand, next_attempt_at not) converges, and a
  step interrupted mid-way is safely resumed on the next boot.
* Steps are dialect-aware (PostgreSQL in Docker, SQLite natively):
  type spellings differ where the dialects differ, and 009's
  DROP NOT NULL is PostgreSQL-only — exactly as 009's own header
  notes, SQLite dev databases keep the legacy NOT NULL on
  call_sessions.campaign_id/lead_id (recreate those instead); the
  production dialect enforces the final shape.

SOURCE OF TRUTH: the SQLAlchemy models. On an empty database step
001 creates the initial tables FROM THE MODELS (final shape), later
markers then verify true and backfill — so create_all (which runs
right after this runner in db.init_db) and the migrations can never
fight: both end at the model schema, on fresh and ancient databases
alike.
"""
from datetime import datetime

from sqlalchemy import inspect, text

from .models import Base

_TABLE = "schema_migrations"

# Tables migration 001 is responsible for (provider_settings is 003's).
_TABLES_001 = ("tenants", "users", "agent_configs", "prompt_versions",
               "campaigns", "leads", "call_sessions", "call_results")


# ── schema introspection helpers ─────────────────────────────────────

def _tables(insp) -> set:
    return set(insp.get_table_names())


def _columns(insp, table: str) -> dict:
    if table not in _tables(insp):
        return {}
    return {c["name"]: c for c in insp.get_columns(table)}


def _has_columns(insp, table: str, *names: str) -> bool:
    cols = _columns(insp, table)
    return all(n in cols for n in names)


def _ts(dialect: str) -> str:
    return ("TIMESTAMP WITHOUT TIME ZONE" if dialect == "postgresql"
            else "TIMESTAMP")


def _now_literal() -> str:
    # ADD COLUMN ... NOT NULL needs a value for existing rows. The
    # 002 file's DEFAULT now() is PostgreSQL-only syntax; a UTC
    # timestamp literal computed here is the same statement in both
    # dialects (existing rows are stamped with the migration time).
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_column(conn, table: str, ddl: str) -> None:
    """Add one column if (and only if) it is missing. `ddl` is the full
    column definition, e.g. "timezone VARCHAR(60) NOT NULL DEFAULT '…'".
    Re-inspects on every call, so a partially-applied step resumes
    exactly where it stopped."""
    column = ddl.split(None, 1)[0]
    insp = inspect(conn)
    if column not in _columns(insp, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


# ── the steps (mirror backend/migrations/NNN_*.sql, same order) ──────

def _marker_001(insp, dialect):
    return "tenants" in _tables(insp)


def _apply_001(conn, insp, dialect):
    # Empty database: create the initial tables from the models (the
    # single source of truth) — see the module docstring. checkfirst
    # keeps this a no-op for any table that somehow already exists.
    tables = [Base.metadata.tables[t] for t in _TABLES_001]
    Base.metadata.create_all(bind=conn, tables=tables, checkfirst=True)


def _marker_002(insp, dialect):
    return _has_columns(insp, "campaigns", "product_detail", "audience",
                        "channel", "updated_at")


def _apply_002(conn, insp, dialect):
    _ensure_column(conn, "campaigns",
                   "product_detail VARCHAR(300) NOT NULL DEFAULT ''")
    _ensure_column(conn, "campaigns", "audience TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "campaigns",
                   "channel VARCHAR(120) NOT NULL DEFAULT 'Service Channel 1'")
    _ensure_column(conn, "campaigns",
                   f"updated_at {_ts(dialect)} NOT NULL DEFAULT '{_now_literal()}'")


def _marker_003(insp, dialect):
    return "provider_settings" in _tables(insp)


def _apply_003(conn, insp, dialect):
    Base.metadata.tables["provider_settings"].create(bind=conn, checkfirst=True)


def _marker_004(insp, dialect):
    return _has_columns(insp, "agent_configs", "voice_gender")


def _apply_004(conn, insp, dialect):
    _ensure_column(conn, "agent_configs",
                   "voice_gender VARCHAR(10) NOT NULL DEFAULT 'female'")


def _marker_005(insp, dialect):
    return _has_columns(insp, "call_sessions", "stt_status", "stt_language",
                        "stt_detail")


def _apply_005(conn, insp, dialect):
    _ensure_column(conn, "call_sessions",
                   "stt_status VARCHAR(20) NOT NULL DEFAULT ''")
    _ensure_column(conn, "call_sessions",
                   "stt_language VARCHAR(20) NOT NULL DEFAULT ''")
    _ensure_column(conn, "call_sessions",
                   "stt_detail VARCHAR(500) NOT NULL DEFAULT ''")


def _marker_006(insp, dialect):
    return (_has_columns(insp, "agent_configs", "max_turns")
            and _has_columns(insp, "call_sessions", "llm_status", "llm_detail",
                             "tts_status", "tts_detail")
            and _has_columns(insp, "call_results", "review_required"))


def _apply_006(conn, insp, dialect):
    _ensure_column(conn, "agent_configs",
                   "max_turns INTEGER NOT NULL DEFAULT 50")
    _ensure_column(conn, "call_sessions",
                   "llm_status VARCHAR(20) NOT NULL DEFAULT ''")
    _ensure_column(conn, "call_sessions",
                   "llm_detail VARCHAR(500) NOT NULL DEFAULT ''")
    _ensure_column(conn, "call_sessions",
                   "tts_status VARCHAR(20) NOT NULL DEFAULT ''")
    _ensure_column(conn, "call_sessions",
                   "tts_detail VARCHAR(500) NOT NULL DEFAULT ''")
    _ensure_column(conn, "call_results",
                   "review_required BOOLEAN NOT NULL DEFAULT FALSE")


def _marker_007(insp, dialect):
    return _has_columns(insp, "agent_configs", "silence_max_prompts")


def _apply_007(conn, insp, dialect):
    _ensure_column(conn, "agent_configs",
                   "silence_max_prompts INTEGER NOT NULL DEFAULT 1")


def _marker_008(insp, dialect):
    return (_has_columns(insp, "tenants", "timezone")
            and _has_columns(insp, "leads", "next_attempt_at"))


def _apply_008(conn, insp, dialect):
    _ensure_column(conn, "tenants",
                   "timezone VARCHAR(60) NOT NULL DEFAULT 'Asia/Kolkata'")
    _ensure_column(conn, "leads", f"next_attempt_at {_ts(dialect)}")


def _marker_009(insp, dialect):
    if not _has_columns(insp, "call_sessions", "channel", "test_lead"):
        return False
    if dialect != "postgresql":
        # SQLite: column presence is the whole marker — the legacy
        # NOT NULL on campaign_id/lead_id stays (009's header: recreate
        # dev databases; production PostgreSQL takes the DROP below).
        return True
    cols = _columns(insp, "call_sessions")
    return bool(cols["campaign_id"]["nullable"] and cols["lead_id"]["nullable"])


def _apply_009(conn, insp, dialect):
    _ensure_column(conn, "call_sessions",
                   "channel VARCHAR(30) NOT NULL DEFAULT ''")
    _ensure_column(conn, "call_sessions",
                   "test_lead JSON NOT NULL DEFAULT '{}'")
    if dialect == "postgresql":
        cols = _columns(inspect(conn), "call_sessions")
        for col in ("campaign_id", "lead_id"):
            if not cols[col]["nullable"]:
                conn.execute(text(
                    f"ALTER TABLE call_sessions ALTER COLUMN {col} DROP NOT NULL"))


MIGRATIONS = [
    (1, "001_initial", _marker_001, _apply_001),
    (2, "002_a3_campaign_fields", _marker_002, _apply_002),
    (3, "003_a4_provider_settings", _marker_003, _apply_003),
    (4, "004_a6_voice_gender", _marker_004, _apply_004),
    (5, "005_a7_stt", _marker_005, _apply_005),
    (6, "006_a8_conversation", _marker_006, _apply_006),
    (7, "007_a9_barge_in_silence", _marker_007, _apply_007),
    (8, "008_a10_dispatch_scale", _marker_008, _apply_008),
    (9, "009_a11_browser_test", _marker_009, _apply_009),
]


# ── the runner ────────────────────────────────────────────────────────

def run_migrations(engine) -> list:
    """Bring the connected database to the current schema, in place.

    Returns [(version, name, action), …] for the steps touched this
    boot, action "applied" (DDL ran) or "backfilled" (marker already
    present — recorded without DDL). Steps already recorded in
    schema_migrations are skipped silently, so a current database
    boots with an empty list: the runner is a no-op.
    """
    dialect = engine.dialect.name
    touched = []
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "version INTEGER PRIMARY KEY, "
            "name VARCHAR(200) NOT NULL DEFAULT '', "
            "applied_at TIMESTAMP NOT NULL)"))
        recorded = {row[0] for row in
                    conn.execute(text(f"SELECT version FROM {_TABLE}"))}
        for version, name, marker, apply_step in MIGRATIONS:
            if version in recorded:
                continue
            insp = inspect(conn)
            if marker(insp, dialect):
                action = "backfilled"
            else:
                apply_step(conn, insp, dialect)
                action = "applied"
            conn.execute(
                text(f"INSERT INTO {_TABLE} (version, name, applied_at) "
                     "VALUES (:v, :n, :ts)"),
                {"v": version, "n": name, "ts": datetime.utcnow()})
            touched.append((version, name, action))
    return touched
