-- Epic C, story C5: roles + allocation + queue audit (DDL of record).
-- Applied by backend/app/migrations.py _apply_014 (marker-gated, so
-- re-running it is a no-op); new TABLES (teams, user_invites) are
-- created by create_all on boot and need no step here.
--
-- Columns on existing tables (all nullable — pre-C5 rows keep working):
--   users.team_id, users.phone
--   leads.assigned_caller_id, leads.allocated_by, leads.allocated_at
--   call_results.disposition_updated_by, call_results.disposition_updated_at
--   call_results.approval_decision, call_results.approved_by,
--   call_results.approved_at, call_results.approval_expires_at
--
-- Data migration (the one C5 cannot avoid): legacy role values become
-- C5 role names, so enforcement checks exactly four names:
--   UPDATE users SET role = 'tenant_admin' WHERE role = 'admin';
--   UPDATE users SET role = 'caller' WHERE role = 'operator';
ALTER TABLE users ADD COLUMN team_id VARCHAR(40);
ALTER TABLE users ADD COLUMN phone VARCHAR(30);
ALTER TABLE leads ADD COLUMN assigned_caller_id VARCHAR(40);
ALTER TABLE leads ADD COLUMN allocated_by VARCHAR(40);
ALTER TABLE leads ADD COLUMN allocated_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE call_results ADD COLUMN disposition_updated_by VARCHAR(40);
ALTER TABLE call_results ADD COLUMN disposition_updated_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE call_results ADD COLUMN approval_decision VARCHAR(20);
ALTER TABLE call_results ADD COLUMN approved_by VARCHAR(40);
ALTER TABLE call_results ADD COLUMN approved_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE call_results ADD COLUMN approval_expires_at TIMESTAMP WITHOUT TIME ZONE;
UPDATE users SET role = 'tenant_admin' WHERE role = 'admin';
UPDATE users SET role = 'caller' WHERE role = 'operator';
