-- 014_p7_run_inputs: see orchestrator/ddl/delta/014_p7_run_inputs.sql for
-- the full rationale -- same column, sqlite dialect.

ALTER TABLE run_fingerprints ADD COLUMN run_inputs_hash TEXT;
