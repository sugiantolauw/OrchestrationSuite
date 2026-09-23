-- 006_p3_monetary_basis: persist each finding's monetary basis (CLAUDE.md
-- P2/P3 gate review, B2 -- "the headline is not an exposure"). See
-- orchestrator/ddl/delta/006_p3_monetary_basis.sql for the full rationale --
-- same column, sqlite dialect.
--
-- A brand-new nullable column with its own CHECK -- unlike migration 005's
-- widened severity/severity_basis constraints, this needs no rebuild-and-copy
-- dance (SQLite supports ALTER TABLE ADD COLUMN ... CHECK for a genuinely
-- new column, same as migration 004's analyst_set_severity/severity_basis).

ALTER TABLE findings ADD COLUMN monetary_basis TEXT
  CHECK (monetary_basis IS NULL OR monetary_basis IN ('spend', 'excess', 'approved_not_spent', 'none'));
