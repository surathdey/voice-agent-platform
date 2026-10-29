-- NOTE (post-A12 upgrade-path fix): the application applies this step
-- automatically at boot via the migration runner (backend/app/
-- migrations.py) — idempotent, per-object, dialect-aware (PostgreSQL
-- + SQLite), tracked in the schema_migrations table. This file
-- remains the DDL of record; the runner step of the same number is
-- its executable twin.
-- 013_c2_catalog_pinning.sql — Epic C, story C2: campaign pinning +
-- adoption linkage columns. New TABLES (catalog_agents,
-- catalog_versions, tenant_adoptions, adoption_log) need no DDL here:
-- create_all in db.init_db creates missing tables on every boot.
-- All new columns are NULLABLE: pre-C2 rows pin nothing and fall
-- back to the agent's live prompt, exactly as before.
--   agent_configs.adopted_catalog_agent   catalog slug this agent was
--                                         last adopted from (NULL =
--                                         never adopted / diverged)
--   agent_configs.adopted_catalog_version catalog version adopted
--   campaigns.pinned_prompt               prompt text snapshotted at
--                                         creation, kept for life
--   campaigns.pinned_prompt_version       tenant prompt version pinned
--   campaigns.pinned_catalog_agent        catalog slug pinned (if any)
--   campaigns.pinned_catalog_version      catalog version pinned (if any)
ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS adopted_catalog_agent VARCHAR(80);
ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS adopted_catalog_version INTEGER;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS pinned_prompt TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS pinned_prompt_version INTEGER;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS pinned_catalog_agent VARCHAR(80);
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS pinned_catalog_version INTEGER;
