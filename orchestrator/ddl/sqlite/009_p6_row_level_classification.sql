-- 009_p6_row_level_classification: per-row results for the T4.3 row-level
-- LLM classification capability (independent review 2026-09-24 item 4).
-- Built, switched off by default (ENABLE_ROW_LEVEL_LLM=false) -- while off,
-- nothing ever writes here; T4.3 stays `not_testable`. Idempotent
-- replace-per-run (same pattern as flagged_rows), so a re-run of the
-- classification step overwrites its own prior results, never appends
-- duplicates.

CREATE TABLE IF NOT EXISTS t43_classifications (
  run_id TEXT NOT NULL,
  row_key TEXT NOT NULL,
  personal_expense INTEGER NOT NULL,
  confidence REAL NOT NULL,
  rationale TEXT,
  call_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, row_key)
);
