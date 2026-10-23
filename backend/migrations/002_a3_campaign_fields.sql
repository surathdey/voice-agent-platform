-- NOTE (post-A12 upgrade-path fix): the application applies this step
-- automatically at boot via the migration runner (backend/app/
-- migrations.py) — idempotent, per-object, dialect-aware (PostgreSQL
-- + SQLite), tracked in the schema_migrations table. This file
-- remains the DDL of record; the runner step of the same number is
-- its executable twin.
-- 002_a3_campaign_fields.sql — Phase A chunk 2 (A3: campaigns & leads).
-- Campaign gains the §15 business fields: audience, named channel, product
-- detail, and an updated_at stamp; product narrows to the canonical product
-- kind (the kind decides the on-call identifier type). Lead rows are
-- unchanged — the product identifier lives in leads.agent_fields under
-- product_identifier / product_identifier_type.
-- The application also creates fresh tables in this shape on boot
-- (db.init_db from the models); this file is the DDL of record for
-- upgrading a database created by 001_initial.sql.

ALTER TABLE campaigns ADD COLUMN product_detail VARCHAR(300) NOT NULL DEFAULT '';
ALTER TABLE campaigns ADD COLUMN audience TEXT NOT NULL DEFAULT '';
ALTER TABLE campaigns ADD COLUMN channel VARCHAR(120) NOT NULL DEFAULT 'Service Channel 1';
ALTER TABLE campaigns ADD COLUMN updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now();
