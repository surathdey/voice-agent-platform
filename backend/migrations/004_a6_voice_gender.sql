-- 004_a6_voice_gender.sql — Phase A chunk 5 (A6: telephony spike).
-- AgentConfig gains voice_gender (female | male): the agent persona's
-- configured voice gender. In managed mode the Vapi transport uses it to
-- pick the female / male voice ID from the tenant's per-language Vapi
-- voice profile (provider settings, chunk 5 — those live in the
-- provider_settings.fields JSON and need no DDL).
-- The application also creates fresh tables in this shape on boot
-- (db.init_db from the models); this file is the DDL of record for
-- upgrading a database created by 001–003.

ALTER TABLE agent_configs ADD COLUMN voice_gender VARCHAR(10) NOT NULL DEFAULT 'female';
