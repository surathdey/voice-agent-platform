-- NOTE: the application applies this step automatically at boot via
-- the migration runner (backend/app/migrations.py) — idempotent,
-- per-object, dialect-aware (PostgreSQL + SQLite), tracked in the
-- schema_migrations table. This file remains the DDL of record; the
-- runner step of the same number is its executable twin.
-- On a FRESH database this DDL is unnecessary: create_all builds
-- catalog_versions WITH the status column from the model. It applies
-- to EXISTING databases whose catalog_versions predates the column.
-- 015_c2_draft_status.sql — Epic C, C2 draft/publish split: explicit
-- status on catalog versions. published_at is NOT NULL in the schema
-- (Mapped[datetime] infers it), so it cannot mark a draft. The status
-- column does: 'draft' rows are work in progress invisible to tenants,
-- 'published' rows are live. ADD COLUMN with NOT NULL DEFAULT
-- backfills existing rows as 'published' on both dialects.
ALTER TABLE catalog_versions ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'published';
UPDATE catalog_versions SET status = 'published' WHERE status IS NULL;
