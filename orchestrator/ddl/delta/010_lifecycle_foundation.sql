-- 010_lifecycle_foundation: LM1 (LIFECYCLE_design.md §4) -- the shared shape
-- the lifecycle modules write into. This migration creates schema headroom
-- only (CLAUDE.md §4.8/§4.9 P1B pattern extended to the lifecycle spec):
-- new run_kind values, an executor-routing projection on `runs`, an
-- append-only dispatch ledger for the future JobsExecutor (L0.5, not built
-- in this work package), engagement metadata, an engagement<->enterprise-
-- risk link table, and provenance/decision columns on `risks`/`controls`
-- for the future Sensing/Assessment/Design modules (L2/L3/L7). No lifecycle
-- node writes any of these columns yet.

ALTER TABLE ${catalog}.${schema}.runs DROP CONSTRAINT runs_run_kind;
ALTER TABLE ${catalog}.${schema}.runs ADD CONSTRAINT runs_run_kind
  CHECK (run_kind IN ('fieldwork', 'sensing', 'assessment', 'planning', 'design_assessment', 'reporting', 'evidence'));

-- executor/executor_ref/executed_as (LIFECYCLE_design.md §2.5): which
-- Executor implementation ran the most recent dispatched phase of this run,
-- its external run id (a Databricks Jobs run id once JobsExecutor exists),
-- and the identity it executed as -- distinct from runs.run_owner, the
-- requesting user, which never changes.
ALTER TABLE ${catalog}.${schema}.runs ADD COLUMNS (executor STRING, executor_ref STRING, executed_as STRING);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.executor_dispatches (
  dispatch_id STRING NOT NULL,
  run_id STRING NOT NULL,
  phase STRING NOT NULL,
  phase_epoch BIGINT NOT NULL,
  executor STRING NOT NULL,
  external_run_id STRING,
  dispatched_by STRING NOT NULL,
  dispatched_at TIMESTAMP NOT NULL,
  CONSTRAINT executor_dispatches_pk PRIMARY KEY (dispatch_id)
) USING DELTA TBLPROPERTIES (
  'delta.appendOnly' = 'true'
);

ALTER TABLE ${catalog}.${schema}.engagements ADD COLUMNS (
  description STRING,
  business_unit STRING,
  materiality DOUBLE,
  materiality_currency STRING,
  prior_engagement_id STRING,
  created_by STRING
);

-- Links enterprise-register risks (LIFECYCLE_design.md §5.1 Risk Sensing) to
-- the engagements that draw on them. A sensed risk with no engagement yet
-- has no row here -- this table records the use of a risk by an
-- engagement, not the existence of the risk itself (that stays in `risks`).
CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.engagement_risks (
  engagement_id STRING NOT NULL,
  risk_id STRING NOT NULL,
  added_by STRING NOT NULL,
  added_at TIMESTAMP NOT NULL,
  removed_by STRING,
  removed_at TIMESTAMP,
  reason STRING,
  CONSTRAINT engagement_risks_pk PRIMARY KEY (engagement_id, risk_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

ALTER TABLE ${catalog}.${schema}.risks ADD COLUMNS (
  proposed_by_run_id STRING,
  decided_by STRING,
  decided_at TIMESTAMP,
  decision_reason STRING,
  register_scope STRING
);

-- document_corpus (Risk Sensing over a knowledge corpus) and explorer (a
-- risk proposed alongside an Explorer plan, CLAUDE.md §4.5) widen the
-- existing provenance vocabulary. NOTE for whoever merges this alongside
-- the Explorer migration (LIFECYCLE_design.md §4 flags this same caveat
-- itself): if that migration already added explorer to this CHECK under a
-- different version number, the two ADD CONSTRAINT statements collide and
-- the later one wins on drop/re-add -- reconcile into a single CHECK
-- covering the union of every value at merge time.
ALTER TABLE ${catalog}.${schema}.risks DROP CONSTRAINT risks_source;
ALTER TABLE ${catalog}.${schema}.risks ADD CONSTRAINT risks_source
  CHECK (source IN ('manual', 'glean', 'regulation', 'erm_import', 'document_corpus', 'explorer'));

ALTER TABLE ${catalog}.${schema}.risks ADD CONSTRAINT risks_register_scope
  CHECK (register_scope IS NULL OR register_scope IN ('enterprise', 'engagement'));

ALTER TABLE ${catalog}.${schema}.controls ADD COLUMNS (
  status STRING,
  proposed_by_run_id STRING,
  decided_by STRING,
  decided_at TIMESTAMP,
  design_assessment_id STRING,
  design_concluded_by STRING,
  design_concluded_at TIMESTAMP
);

-- Backfill (LIFECYCLE_design.md §4 LM1): every control seeded before this
-- migration (the P2 T&E seeds, CLAUDE.md §4.9) was accepted at seeding
-- time -- there was no proposal/decision step yet for it to have gone
-- through.
UPDATE ${catalog}.${schema}.controls SET status = 'accepted' WHERE status IS NULL;

ALTER TABLE ${catalog}.${schema}.controls ADD CONSTRAINT controls_status
  CHECK (status IS NULL OR status IN ('proposed', 'accepted', 'rejected', 'superseded'));
