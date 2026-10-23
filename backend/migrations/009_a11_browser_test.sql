-- 009_a11_browser_test.sql — Phase A chunk 10 (A11: browser test harness).
--   call_sessions.channel    how the session was originated: '' = a
--                            dispatched campaign attempt; 'browser-test' =
--                            the in-console harness. Rides the §8.2 surface
--                            (CallOut.channel) so a test call is visibly a
--                            test everywhere it appears.
--   call_sessions.test_lead  the browser-test lead snapshot (JSON): the
--                            fictitious / picked lead the operator tested
--                            against, incl. the agent id and any source
--                            campaign/lead references. A snapshot, never a
--                            lead row — a test creates, consumes and
--                            re-queues NO campaign lead.
--   campaign_id / lead_id    become nullable: a browser-test session is
--                            not a campaign attempt and references neither.
-- The application also creates fresh tables in this shape on boot
-- (db.init_db from the models); this file is the DDL of record for
-- upgrading a database created by 001–008. (The DROP NOT NULL form is
-- PostgreSQL; a fresh SQLite dev database takes the shape from the
-- models, and SQLite does not enforce the old NOT NULL once the column
-- exists only via these ADD COLUMNs on a pre-009 file — recreate dev
-- databases instead.)

ALTER TABLE call_sessions ADD COLUMN channel VARCHAR(30) NOT NULL DEFAULT '';
ALTER TABLE call_sessions ADD COLUMN test_lead JSON NOT NULL DEFAULT '{}';
ALTER TABLE call_sessions ALTER COLUMN campaign_id DROP NOT NULL;
ALTER TABLE call_sessions ALTER COLUMN lead_id DROP NOT NULL;
