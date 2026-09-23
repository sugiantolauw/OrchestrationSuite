-- 006_p3_monetary_basis: persist each finding's monetary basis (CLAUDE.md
-- P2/P3 gate review, B2 -- "the headline is not an exposure").
--
-- findings.yaml (skills/tne_exco/) now requires every finding to declare
-- monetary_basis: spend | excess | approved_not_spent | none, so the run
-- headline (orchestrator.nodes.fieldwork.prioritise) knows which findings'
-- flagged transactions are genuinely at-risk reimbursed spend ('spend'), an
-- excess/overage figure only ('excess'), money approved but never spent
-- ('approved_not_spent' -- reported separately, never summed into the
-- headline), or carry no dollar figure at all ('none'). Previously the
-- headline summed every flagged row of every monetary finding regardless of
-- what its cited amount actually meant -- T3.1a's approved-but-unspent
-- travel amounts, both lines of a duplicate pair, whole T6.1d days -- none
-- of which is "gross value of flagged spend".

ALTER TABLE ${catalog}.${schema}.findings ADD COLUMN monetary_basis STRING;
ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_monetary_basis_chk
  CHECK (monetary_basis IS NULL OR monetary_basis IN ('spend', 'excess', 'approved_not_spent', 'none'));
