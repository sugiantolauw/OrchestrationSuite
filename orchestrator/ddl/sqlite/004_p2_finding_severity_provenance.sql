-- 004_p2_finding_severity_provenance: persist the analyst-set-threshold label on
-- findings (CLAUDE.md §0.4, G8). See orchestrator/ddl/delta/004_p2_finding_severity_provenance.sql
-- for the full rationale -- same columns, sqlite dialect.

ALTER TABLE findings ADD COLUMN analyst_set_severity INTEGER;
ALTER TABLE findings ADD COLUMN severity_basis TEXT
  CHECK (severity_basis IS NULL OR severity_basis IN ('fixed', 'threshold'));
