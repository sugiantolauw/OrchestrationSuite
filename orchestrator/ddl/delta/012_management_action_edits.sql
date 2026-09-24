-- 012_management_action_edits: persist an auditor edit to a management
-- action owner/status/target_date/response (independent review
-- 2026-09-24 gap #3 / the /workspace/tne subtitle CLAUDE.md §11 requires:
-- "Action ownership and responses are saved with this run"). Until now
-- edits lived only in a browser session dcc.Store and were lost on refresh
-- -- update_management_action (orchestrator/service.py) writes here
-- instead, recording who made the edit alongside the existing last_updated
-- "when".
--
-- Also widens management_actions_status to include agreed and
-- remediated: the Management Action Tracker own status dropdown
-- (app/src/workspace_tne.py _actions_tab) already offers both, but no write
-- path ever exercised the CHECK before this migration, so the mismatch
-- between the UI control and the constraint went unnoticed.

ALTER TABLE ${catalog}.${schema}.management_actions ADD COLUMN updated_by STRING;

ALTER TABLE ${catalog}.${schema}.management_actions DROP CONSTRAINT management_actions_status;
ALTER TABLE ${catalog}.${schema}.management_actions ADD CONSTRAINT management_actions_status
  CHECK (status IN ('draft', 'open', 'under_review', 'in_progress', 'agreed', 'remediated', 'closed'));
