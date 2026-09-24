-- 013_p7_export_code_revision: independent review 2026-09-24 gap #11 /
-- CLAUDE.md section 11 "Paused runs across a code deploy" -- a run signed
-- off under one code revision may complete its export phase under a later
-- one, since its numbers were already fixed by execute and only the code
-- that writes the export actually changes. run_fingerprints stays immutable
-- (CLAUDE.md P1A) so the code revision actually used for export is
-- recorded here instead: runs.export_code_revision for a cheap direct
-- read by get_run/list_runs/exports, and run_fingerprint_overrides as an
-- append-only history of every accepted fingerprint-field override, kept
-- general (run_id, phase, field) in case a later phase relaxes another
-- field the same way. Both stay NULL/empty until a run actually exports
-- under a different code revision than the one it was created under.

ALTER TABLE runs ADD COLUMN export_code_revision TEXT;

CREATE TABLE IF NOT EXISTS run_fingerprint_overrides (
  override_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT NOT NULL,
  fingerprint_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  field TEXT NOT NULL,
  stored_value TEXT,
  override_value TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS run_fingerprint_overrides_no_update BEFORE UPDATE ON run_fingerprint_overrides BEGIN SELECT RAISE(ABORT, 'run_fingerprint_overrides is append-only'); END;

CREATE TRIGGER IF NOT EXISTS run_fingerprint_overrides_no_delete BEFORE DELETE ON run_fingerprint_overrides BEGIN SELECT RAISE(ABORT, 'run_fingerprint_overrides is append-only'); END;
