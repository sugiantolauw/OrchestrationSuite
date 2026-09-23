-- 005_p3_indeterminate_severity: allow a finding's severity ladder to record
-- "could not be evaluated" as its own explicit outcome (CLAUDE.md NN14,
-- P2/P3 gate review item 4).
--
-- orchestrator.findings._select_severity previously used the same Kleene
-- collapse-to-False that a `trigger` correctly uses (a not_testable test's
-- missing metric must never make a finding fire by accident) for `severity[]
-- .when` too -- so a `when` whose own metric was simply missing silently
-- fell through to a lower rung, exactly as if the condition had genuinely
-- evaluated False. A rule meant to catch a High case could render Medium or
-- Low with nothing in the finding to say a metric was unavailable, not that
-- the severity was genuinely low.
--
-- Fixed in orchestrator/findings.py: a `when` that cannot be evaluated
-- (evaluate_ternary returns None) now reports severity='Indeterminate',
-- severity_basis='indeterminate', and the specific missing-metric(s)
-- explanation in severity_rule (already unconstrained text) -- never a real
-- High/Medium/Low, and never silently equated with Low.

ALTER TABLE ${catalog}.${schema}.findings DROP CONSTRAINT findings_severity;
ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_severity
  CHECK (severity IN ('High', 'Medium', 'Low', 'Indeterminate'));

ALTER TABLE ${catalog}.${schema}.findings DROP CONSTRAINT findings_severity_basis_chk;
ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_severity_basis_chk
  CHECK (severity_basis IS NULL OR severity_basis IN ('fixed', 'threshold', 'indeterminate'));
