-- 005_a7_stt.sql — Phase A chunk 6 (A7: STT integration).
-- CallSession gains the self-hosted STT record: the Sarvam Saaras
-- streaming session opened per call inside the Plivo media stream.
--   stt_status   '' (no STT leg) | active | failed | not_configured
--   stt_language the Saaras language code the session was opened with
--                (bn-IN / hi-IN / en-IN — resolved from the agent's
--                language rule for the lead)
--   stt_detail   the failure reason when stt_status = 'failed'
--                (exception class / short label only — never secrets,
--                never raw provider response bodies)
-- Final transcript segments live in call_sessions.transcript (already
-- present since 001); partials are an in-memory live view only.
-- The application also creates fresh tables in this shape on boot
-- (db.init_db from the models); this file is the DDL of record for
-- upgrading a database created by 001–004.

ALTER TABLE call_sessions ADD COLUMN stt_status VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE call_sessions ADD COLUMN stt_language VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE call_sessions ADD COLUMN stt_detail VARCHAR(500) NOT NULL DEFAULT '';
