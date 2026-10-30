-- NOTE: the application applies this step automatically at boot via
-- the migration runner (backend/app/migrations.py) — idempotent,
-- per-object, dialect-aware (PostgreSQL + SQLite), tracked in the
-- schema_migrations table. This file remains the DDL of record; the
-- runner step of the same number is its executable twin.
-- On a FRESH database this DDL is unnecessary: create_all builds
-- catalog_versions WITH the new columns from the model. It applies
-- to EXISTING databases whose catalog_versions predates them.
-- 016_c2_version_full_content.sql — Epic C, C2 stack-conditional form:
-- the version row carries its FULL content (stack + runtime config),
-- not just prompt + metadata. Previously a version stored only
-- prompt/note/suitable_for/model/validated_on/status, and adoption
-- copied the agent row's config — so a version whose stack changed
-- after the agent was created adopted a STALE stack (the seed's
-- managed v1 adopted the agent row's Sarvam defaults). After this
-- step every version row is self-contained and adoption reads the
-- version, never the agent row.
-- Backfill (existing rows only; new rows are filled by the version
-- creation resolver): stack providers by suitable_for — managed rows
-- resolve to the Vapi stack (deepgram/elevenlabs/""), self_hosted
-- rows to the Sarvam stack (saaras/bulbul/agent voice when the agent
-- itself is Bulbul), both rows keep the agent's set. Non-stack config
-- (language, voice gender, language_rule, captured_fields, duration,
-- turn/silence settings) copies from the parent agent row. validated_on
-- is HISTORY and is never rewritten here.
-- Dialect note: JSON columns are JSON on PostgreSQL, TEXT on SQLite
-- (SQLAlchemy's JSON type renders as TEXT there); the runner picks
-- the spelling per dialect.
ALTER TABLE catalog_versions ADD COLUMN stt_provider VARCHAR(60) NOT NULL DEFAULT '';
ALTER TABLE catalog_versions ADD COLUMN stt_language VARCHAR(20) NOT NULL DEFAULT 'hi-IN';
ALTER TABLE catalog_versions ADD COLUMN tts_provider VARCHAR(60) NOT NULL DEFAULT '';
ALTER TABLE catalog_versions ADD COLUMN tts_voice_id VARCHAR(120) NOT NULL DEFAULT '';
ALTER TABLE catalog_versions ADD COLUMN voice_gender VARCHAR(10) NOT NULL DEFAULT 'female';
ALTER TABLE catalog_versions ADD COLUMN language_rule JSON;
ALTER TABLE catalog_versions ADD COLUMN captured_fields JSON;
ALTER TABLE catalog_versions ADD COLUMN max_duration_s INTEGER NOT NULL DEFAULT 300;
ALTER TABLE catalog_versions ADD COLUMN max_turns INTEGER NOT NULL DEFAULT 50;
ALTER TABLE catalog_versions ADD COLUMN silence_prompt_after_s INTEGER NOT NULL DEFAULT 8;
ALTER TABLE catalog_versions ADD COLUMN silence_max_prompts INTEGER NOT NULL DEFAULT 1;
UPDATE catalog_versions SET
  stt_provider = CASE suitable_for
    WHEN 'self_hosted' THEN 'saaras'
    WHEN 'managed' THEN 'deepgram'
    ELSE (SELECT stt_provider FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id)
  END,
  tts_provider = CASE suitable_for
    WHEN 'self_hosted' THEN 'bulbul'
    WHEN 'managed' THEN 'elevenlabs'
    ELSE (SELECT tts_provider FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id)
  END,
  tts_voice_id = CASE suitable_for
    WHEN 'managed' THEN ''
    WHEN 'self_hosted' THEN (SELECT CASE WHEN tts_provider = 'bulbul' THEN tts_voice_id ELSE '' END FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id)
    ELSE (SELECT tts_voice_id FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id)
  END,
  stt_language = (SELECT stt_language FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id),
  voice_gender = (SELECT voice_gender FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id),
  language_rule = (SELECT language_rule FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id),
  captured_fields = (SELECT captured_fields FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id),
  max_duration_s = (SELECT max_duration_s FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id),
  max_turns = (SELECT max_turns FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id),
  silence_prompt_after_s = (SELECT silence_prompt_after_s FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id),
  silence_max_prompts = (SELECT silence_max_prompts FROM catalog_agents WHERE catalog_agents.catalog_agent_id = catalog_versions.catalog_agent_id);
