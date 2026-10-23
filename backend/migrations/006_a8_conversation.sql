-- 006_a8_conversation.sql — Phase A chunk 7 (A8: full conversation loop).
-- The self-hosted stream gains the LLM + TTS legs beside the A7 STT leg,
-- and results gain the review flag the post-call classifier sets when it
-- cannot produce a trustworthy disposition.
--   agent_configs.max_turns   conversation-loop guard: the engine ends
--                             the call (end_reason max_turns) once this
--                             many agent turns have run (default 50).
--   call_sessions.llm_status  '' (never streamed) | active | failed |
--                             not_configured — same vocabulary as
--                             stt_status (005); llm_detail / tts_detail
--                             carry the failure reason (exception class /
--                             short label only — never secrets, never raw
--                             provider response bodies).
--   call_sessions.tts_status  same vocabulary as llm_status.
--   call_results.review_required  set when classification failed, was
--                             unparseable, or returned a disposition
--                             outside the agent prompt's bucket set (an
--                             unknown bucket is never stored).
-- The application also creates fresh tables in this shape on boot
-- (db.init_db from the models); this file is the DDL of record for
-- upgrading a database created by 001–005.

ALTER TABLE agent_configs ADD COLUMN max_turns INTEGER NOT NULL DEFAULT 50;
ALTER TABLE call_sessions ADD COLUMN llm_status VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE call_sessions ADD COLUMN llm_detail VARCHAR(500) NOT NULL DEFAULT '';
ALTER TABLE call_sessions ADD COLUMN tts_status VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE call_sessions ADD COLUMN tts_detail VARCHAR(500) NOT NULL DEFAULT '';
ALTER TABLE call_results ADD COLUMN review_required BOOLEAN NOT NULL DEFAULT FALSE;
