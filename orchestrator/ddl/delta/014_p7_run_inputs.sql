-- 014_p7_run_inputs: independent review 2026-09-25 item 1 ("run inputs" --
-- docs/specs/P7_mapping_authoring_design.md §1.3). Column mappings, parameter
-- overrides and unsupplied sources are declared in the existing
-- SOURCE_BINDINGS file (D-P7-1), resolved once at run creation
-- (orchestrator.run_inputs.resolve_run_inputs) and pinned into
-- RunState.options["run_inputs"] -- this migration only adds the ONE
-- additional fingerprint column that pins them into the run's provenance
-- record: run_inputs_hash, the sha256 of the canonical JSON of the resolved
-- run_inputs dict. Nullable, and excluded from `_HASHED_FIELDS`'s
-- fingerprint_id computation whenever it is null, so this migration changes
-- nothing about any existing run's fingerprint_id or verify_fingerprint
-- result (orchestrator/fingerprint.py).

ALTER TABLE ${catalog}.${schema}.run_fingerprints ADD COLUMN run_inputs_hash STRING;
