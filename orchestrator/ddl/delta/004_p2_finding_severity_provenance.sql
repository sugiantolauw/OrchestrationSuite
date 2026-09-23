-- 004_p2_finding_severity_provenance: persist the analyst-set-threshold label on
-- findings (CLAUDE.md §0.4, G8). build_findings (orchestrator/findings.py) already
-- computes analyst_set_severity/severity_basis per finding but write_findings
-- (orchestrator/adapters/persistence_delta.py, persistence_local.py) was dropping
-- both before P2/P3 gate review item 3: the XLSX export and the UI must never
-- default this to False -- it must come from the persisted row or the caller must
-- raise. severity_basis records WHY: 'fixed' (a hardcoded severity, no threshold
-- involved) or 'threshold' (severity chosen by a threshold comparison, which may or
-- may not itself be analyst-set -- analyst_set_severity is the flag that matters for
-- the UI label).

ALTER TABLE ${catalog}.${schema}.findings ADD COLUMN analyst_set_severity BOOLEAN;
ALTER TABLE ${catalog}.${schema}.findings ADD COLUMN severity_basis STRING;
ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_severity_basis_chk
  CHECK (severity_basis IS NULL OR severity_basis IN ('fixed', 'threshold'));
