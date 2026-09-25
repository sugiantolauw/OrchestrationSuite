# P7-round design: column mapping, Skill authoring kit, review workflow

**Status:** design only. Nothing here is built until the user answers the decisions in §5.
**Scope:** CLAUDE.md §11 "Acceptance-round scope" (2026-09-25): build-here items 2, 5 and 6.
**Authority:** CLAUDE.md. Where this spec and CLAUDE.md disagree, CLAUDE.md wins. Say so and ask.
**Base:** `claude/bold-faraday-w9gb8y` at `cb07206`.

Three rules shape all of it:
- **UI is the prototype's, exactly.** Every visible addition is a numbered UI item (UI-M*, UI-R*) for the user to approve.
  `/run/<id>` gets these additions; prototype pages get almost none. Item 2 has no UI.
- **Configuration, not guessing** (NN14). A mapping, a parameter override or an unsupplied source is always an exact,
  declared value, and it is pinned in the fingerprint.
- **Genie Code at the corporate workspace edits files but cannot run code.** So everything here is finished and tested
  in this build. The corporate side then only supplies YAML: bindings, mappings, parameter files and group names.

---

## 1. Column mapping at run setup (build-here item 2)

### 1.1 What exists already

| Capability | Where | Notes |
|---|---|---|
| Exact source binding per environment (`volume_file` / `uc_table`) | `orchestrator/source_bindings.py:43-132`, `SOURCE_BINDINGS` env (`config.py:118,335`) | YAML/JSON outside version control. It beats every other auto-bind rule (`service.py:1353-1355`) |
| Auto-bind by exact name (governed table short name, or a recent Ready upload with the same file stem) | `service.suggest_bindings` `service.py:1334`, `app/src/run_setup.py:371 _auto_bind` | A source with no binding at all blocks the run (`run_setup.py:395-397`, `service.py:1447-1449`) |
| Contract columns must match exactly after the source's `header_trim` | `orchestrator/contract.py:132 validate_contract`, `:218 header_trim` | UC: `_logical_column_map` does the per-column strip (`adapters/datasource_uc.py:580`) |
| Reference data (population of interest, supplier lists, lookups) | `skills/tne_exco/reference/*.yaml|csv`, loaded by `skills._load_references` `skills.py:68`, resolved from `{ref: name}` by `populations._resolve_value` `populations.py:49` | Skill content, so it is part of `skill_content_hash`. It cannot vary by project today |
| Plan-declared `not_testable` tests | `engine.py:251-268`, `frames.not_testable_flags`, `catalogue_counts.py` | A missing metric evaluates to Unknown, not False (`expr.py:150,188`). `RF_*` columns are filled with null, not 0 |
| Fingerprint | `fingerprint.py:235 compute_fingerprint`, `_HASHED_FIELDS` `:38`, `verify_fingerprint` `:304` | `source_bindings_path` is excluded from `runtime_config_hash` (`config.py:395`). A mapping is **not** captured anywhere |
| Plan-confirmation gate | `status.py:43` (`plan_confirmed` or Playbook `auto_confirm_plan`) | Nothing makes confirmation mandatory for project-specific inputs |

### 1.2 Gap

1. No way to say "this project's `Emp No` is the contract's `Employee ID`".
2. No per-project reference data. Overriding `exco_employee_ids` today means editing the Skill, which changes its content
   hash and version.
3. One missing source fails the whole run. No source can be declared "not supplied, its tests are not testable".
4. None of the above is fingerprinted, forces plan confirmation, or shows in finding provenance.

### 1.3 Design: "run inputs"

One concept covers mappings, parameter overrides and unsupplied sources: **run inputs**. They are declared in the
existing `SOURCE_BINDINGS` file (D-P7-1), validated at `start_audit_run`, pinned into `RunState.options["run_inputs"]`
and hashed into the fingerprint.

**Bindings file: additive schema** (`source_bindings.py`):

```yaml
SKILL-001:
  expense_report:
    kind: volume_file                  # existing
    path: /Volumes/<cat>/<sch>/<vol>/te/expense.xlsx
    sheet: data
    columns:                           # NEW, optional. {contract column: physical column}, exact
      Employee ID: Emp No
      Expense Amount (reimbursement currency): Amount AUD
  booking_detail:
    kind: not_supplied                 # NEW. Allowed only for a contract source declared optional
    reason: "Travel agency data not held by this business unit"
  parameters:                          # NEW, reserved key (no contract source may be named "parameters")
    population_of_interest:
      path: /Volumes/<cat>/<sch>/<vol>/te/exco_ids.csv   # exact file; local path on the local backend
      format: csv                      # csv | yaml
      provenance: {owner: "<name>", as_of: "2026-07-01", source: "HR extract ref <x>"}
```

**Contract declarations: additive** (`contract.yaml` and `orchestrator/schemas/contract.schema.json`):

```yaml
sources:
  booking_detail:
    optional: true                     # NEW, default false. Only an optional source may be not_supplied
parameters:                            # NEW. Which references a project may override, and their shape
  population_of_interest:
    references: [exco_employee_ids]    # the reference name(s) this parameter replaces
    kind: id_list                      # id_list | table | mapping
    item_type: integer                 # id_list only
    columns: {}                        # table only: {column: type}, exact
    group: population_of_interest      # every parameter in a group must be overridden together
```

For SKILL-001 the population of interest is three references: `exco_employee_ids`, `exco_display_names` (the T3.1b
name join) and `exco_namesake_excluded_ids`. They form one `group`, so overriding the IDs without the names is refused.
Adding these declarations changes SKILL-001's content: bump the version and regenerate `content.lock` (the
`tests/test_skill_content_lock.py` guard).

**Validation at run start** (new `orchestrator/run_inputs.py`: `resolve_run_inputs(skill, configured, data_source) ->
RunInputs`). It runs before the fingerprint is computed, and every failure is a `ContractViolation` that lists every
problem:
- **Mapping keys.** Each key is a declared contract column of that source.
- **Physical columns.** Each value exists exactly in the source's header at the **pinned** version: the file header
  after `header_trim`, or `DESCRIBE` at `VERSION AS OF`.
- **No collisions.** No two contract columns map to the same physical column. An unmapped contract column whose name is
  also some *other* column's physical target is refused as ambiguous.
- **Unsupplied sources.** `not_supplied` is allowed only for `optional: true`, and `reason` must be non-empty.
- **Parameter files.** Each file is read once and hashed from the same buffer (the N9 TOCTOU rule). It is parsed as its
  declared `kind`/`item_type`/`columns` and must be non-empty.
  - Groups must be complete.
  - `provenance.owner` and `provenance.as_of` are required. There are no defaults.
- **Unknown names.** An unknown parameter or source name is refused. So is `columns` on a `not_supplied` entry.

**Applying mappings: one wrapper, adapter-agnostic.** New file `orchestrator/adapters/mapped_source.py`, class
`MappedDataSource`, follows the `_CachingDataSource` pattern (`nodes/context.py:10`).
- It renames logical to physical names in `columns=`, `filters=`, `column_stats`, `profile_columns` and
  `distinct_count`, so UC SQL pushdown still works.
- It renames physical to logical names in the returned frames and drops any shadowed physical column.
- It wraps the result of `ctx.data_source_factory` in both places: the resolve-only instance in `start_audit_run` and
  the executor's `NodeContext`. `validate_contract` then sees contract names, unchanged.
- Recoding values (for example code→label) is **not** column mapping and is out of scope. A declared lookup parameter
  can express it later.

**Unsupplied sources become per-test `not_testable`.**
- **Dependencies.** Each test's source dependency is derived at Skill load: the populations named in its params, then
  each population's `source`. A test whose primitive is custom must declare `requires_sources: [..]` in `plan.yaml`,
  because load cannot see inside code. `validate_skill` computes and stores `skill.test_sources: {test_id:
  set[source]}`.
- **In `engine.py`.** An unsupplied source is not read. Any population built on it is not built. Every test that depends
  on it gets:

  ```
  {"status": "not_testable", "reason": "source <name> not supplied for this run: <reason>"}
  ```

  Its declared `RF_*` flags are filled with null (the existing N11 path). A run metric over it is absent, so the
  findings rules see Unknown.
- **In `fieldwork`.** `profile` and G6 reconciliation list the source as `not_supplied` with its reason, never as 0
  rows.
- **`data_assets` entry:**

  ```
  {"source", "table_fqn": None, "version": None, "not_supplied": "<reason>"}
  ```

  `source_table_versions` omits it.
- **`restart_stale_run`** (`service.py:2533`) carries `run_inputs` over from the old state (the same setup), re-resolves
  versions fresh, and must tolerate `table_fqn: None`.

**Mandatory plan confirmation.**
- The `status.py:43` gate becomes:

  ```python
  plan_confirmed or (playbook and auto_confirm_plan and not options.run_inputs)
  ```

- `start_audit_run` also forces `auto_confirm_plan = False` when there are run inputs, so a Playbook run stops at
  `awaiting_confirmation`. The existing "Confirm plan" button confirms it. The state machine enforces this, not the UI.

**Fingerprint.**
- New nullable column `run_fingerprints.run_inputs_hash`: the sha256 of the canonical JSON of `run_inputs`, meaning
  `{mappings, not_supplied, parameters: {name: {path, sha256, provenance}}}`.
- `_HASHED_FIELDS` includes it **only when non-null**, so every existing fingerprint id and `verify_fingerprint` result
  is unchanged for runs without run inputs.
- Parameter files are also added to `reference_data_hashes` (`{path: sha256}`), the existing field for pinned reference
  files.
- At resume, the current fingerprint is recomputed from `state.options["run_inputs"]`, never by re-reading the bindings
  file. Editing that file later does not affect a run in flight.

**Finding provenance.**
- The engine annotates each metric's `source_ref` with `mapped_columns: {contract: physical}`, covering only the
  `source_ref.columns` that were mapped, and `parameters: [name]` for the references the test's populations resolved.
- Findings inherit these through `metrics_cited`.
- The exports show them without any UI approval:
  - **XLSX:** a new "Run inputs" sheet (one row per mapping, parameter or unsupplied source), plus a `run_inputs` flag on
    the cover.
  - **PPTX:** the methodology slide gains one line: "Project-specific inputs applied: N column mappings, M parameters, K
    sources not supplied (see workpaper)".
- `/run/<id>` and `/workspace/tne` are UI-M1 and UI-M2.

### 1.4 UI items (item 1)

| # | Page | Element (existing components only) | Text | Why |
|---|---|---|---|---|
| UI-M1 | `/run/<id>`, `awaiting_confirmation` block (`run_status.py:364`) | Below the existing `P.sub`: one `P.sub` sentence, then an `html.Ul` of `html.Li` lines, one per run input. This is the pattern of `workspace_tne.py:1889` | "This run uses project-specific inputs, so the plan must be confirmed." Lines read "expense_report: column 'Emp No' used as Employee ID", "population_of_interest: exco_ids.csv (sha256 1a2b…, owner X, as of 2026-07-01)" and "booking_detail not supplied (reason) — T3.1b, T3.3a not testable" | The auditor confirms what they can see. A mandatory confirmation of an invisible mapping is not a control |
| UI-M2 | `/workspace/tne`, evidence offcanvas `source_ref` cell (`workspace_tne.py:455 _fmt_source_ref`) | No new element. The existing cell text gains a suffix | "expense_report@ab12… (row) — mapped: Emp No→Employee ID; parameter: population_of_interest" | This is the finding-provenance requirement. It is a content change in an existing cell, but it is still visible on a prototype page, so it needs approval (D-P7-3) |

Not-testable reasons for unsupplied sources render in the existing not-testable slots on `/workspace/tne`. That is data,
not a new element.

### 1.5 Config, env, trace

- No new env var. `SOURCE_BINDINGS` gains the three keys above.
- `.env.example`: extend the comment on line ~105 with a placeholder example of `columns`, `not_supplied` and
  `parameters`. No corporate names (NN16).
- `runtime_config_hash`: unchanged. `source_bindings_path` stays excluded, and the content that matters is hashed via
  `run_inputs_hash`.
- New trace event: `run_inputs_applied`, written once at creation, e.g. "2 column mappings, 1 parameter, 1 source not
  supplied".

### 1.6 Tests

| Kind | Test |
|---|---|
| Unit | `tests/test_run_inputs.py`: every refusal in §1.3 (unknown key, missing physical column at the pinned version, collision, shadowing, `not_supplied` on a required source, incomplete group, missing provenance, empty or wrong-typed parameter file, `parameters` as a source name) |
| Unit | `tests/test_mapped_source.py`: rename both ways; filter and column pushdown translated; shadowed column dropped; a wrapped `LocalFileDataSource` and a fake UC adapter give identical frames |
| Engine | `tests/test_unsupplied_source.py` on `tests/fixtures/skills/mini` with one optional source: only dependent tests are `not_testable`, with the exact reason; flags are null, not 0; dependent findings are not raised; reconciliation lists `not_supplied`; the other tests' numbers are identical to a full run |
| Fingerprint | Byte-identical `fingerprint_id` for a run with no inputs, before and after; a changed mapping → different id; resume after editing the bindings file still verifies; `restart_stale_run` carries the inputs over |
| Gate | Playbook with run inputs cannot pass plan→execute without `confirm_plan` (the state machine rejects it) |
| G9 | Two processes, same mapping and data → byte-identical non-narrative output (extend the existing G9 worker with a mapped fixture) |
| Skill | SKILL-001 with a renamed-column copy of the planted fixture plus a mapping gives Surface 2 results identical to the unmapped fixture |
| Browser (`app/tests/e2e_local`) | `test_run_inputs_ui.py`: mapped run stops at confirmation; UI-M1 lines visible; confirm → completes; XLSX has a "Run inputs" sheet |
| Live (final test plan) | **L-M1:** a Volume file with renamed columns + mapping → run completes; XLSX lists the mapping; `run_fingerprints.run_inputs_hash` is non-null. **L-M2:** a UC table with renamed columns → SQL shows physical names in pushdown (from `system.access.audit`). **L-M3:** mapping to a non-existent column → the run page shows the named violation within the async-start error path (CLAUDE.md §11 "never silent") |

---

## 2. Skill authoring kit (build-here item 5)

The goal: a new Skill is YAML-only work, checked from a notebook or the CLI, with planted fixtures generated from its
contract. There is **no UI** (D-P7-11 confirms this).

### 2.1 What exists: reused, not rewritten

| Reused as-is | Where |
|---|---|
| Per-file JSON Schemas | `orchestrator/schemas/*.schema.json`, `skills._load_yaml_validated` `skills.py:54` |
| All load-time semantic checks: populations, references, primitive param schemas, thresholds provenance, findings trigger/severity via the restricted evaluator, `metrics_cited` ⊆ produced metrics, `monetary_basis`, `entry_key` | `skills.validate_skill` `skills.py:365-593` |
| `control_id`/`risk_id`/`assertion` required per test | `schemas/plan.schema.json:29,44` |
| A skill round trip through `load_skill`/`validate` in a temp directory | `explorer/materialise.check_materialised_skill` `:501` (the pattern) |
| Prose template checks (no code substrings, placeholders ⊆ allowed names) | `explorer/validate._check_no_code_substrings` `:108`, `_check_template` `:128` |
| Content-hash computation | `fingerprint.skill_content_hash` `:145` |
| Promotion guard (named reviewer ≠ creator, Surface 2 thresholds) | `service.publish_skill` `service.py:1977` |

| Moved into `orchestrator/` (logic currently lives only in tests) | From |
|---|---|
| G8 checks (catalogue text == thresholds, every threshold referenced, analyst-set ⇒ pending, severity-basis labelling) | `tests/test_g8_thresholds.py:46-130` (hardwired to `skills/tne_exco`) |
| The content-lock guard | `tests/test_skill_content_lock.py` |

The tests keep their assertions and call the moved functions instead.

| New | |
|---|---|
| Referential check: every `plan.yaml` `control_id`/`risk_id` exists in `risk_control.yaml`, and each control's `tests` list names real tests | No check exists today. It is added to `validate_skill`, so it is a load-time rule for every Skill. SKILL-001 must pass it, and a failure is a real defect to report |
| Collect-all mode: every file's schema errors in one report, not the first raised | `load_skill` raises on the first failing file |

### 2.2 Package `orchestrator/authoring/`

**Scaffold.** `scaffold.py`:

```python
scaffold_skill(dest, *, skill_id, name, domain, owner, sources: dict[str, SampleSpec] | None = None)
```

- It writes a Skill that loads and validates:
  - `manifest.yaml` (`status: draft`, `version: 0.1.0-draft`);
  - `contract.yaml` (with `timezone` required as an explicit argument, never defaulted);
  - `plan.yaml`, with one `raw_<source>` population per source and `tests: []`;
  - `findings.yaml`, `thresholds.yaml`, `risk_control.yaml` and `catalogue.yaml`, empty but valid;
  - `reference/`, `plants.yaml` (a skeleton) and `content.lock`.
- It never writes `custom.py` or `workspace.py`.
- **Optional sample files.** Given a sample CSV/XLSX per source, it proposes contract columns and types, using the
  Explorer profiler's type inference (`explorer/profile.py`). Every inferred entry carries a `# REVIEW: inferred from
  sample <sha256[:12]>` comment.
  - This is authoring-time inference, reviewed by a human, like §4.6.
  - Runtime binding stays exact.
  - PII is never inferred as false. Every column is written `pii: true` until the author changes it.

**Validator.** `checks.py` and `report.py`:

```python
validate_skill_dir(path, *, fixtures=None, plants=None) -> ValidationReport
```

`ValidationReport` holds `errors`, `warnings` and `checks: [{name, passed, detail}]`. The checks run in order:
1. Required files; per-file schemas (collect-all).
2. `validate_skill` (everything in §2.1, plus the referential check, plus item 1's `optional`/`parameters`/
   `requires_sources` rules).
3. G8.
4. Content lock: `content.lock` exists, and its hash equals the content hash, **or** the version changed. A draft Skill
   with no lock is a warning, not an error.
5. Prose templates: no code substrings.
6. Code presence: `custom.py` or `workspace.py` present → **warning** "contains code — needs code review; not a
   YAML-only Skill".
7. Plants coverage: warn below 15 plants and 15 near-misses per scorable test (the SKILL-001 precedent).
8. When `fixtures`/`plants` are given, a dry run:
   - `engine` over `LocalFileDataSource` on the fixture directory;
   - generic Surface 2 scoring (§2.3), giving a per-test precision/recall table;
   - pass means ≥0.98 precision and ≥0.95 recall on every scorable test.

**CLI.**

```
python -m orchestrator.authoring {scaffold|validate|generate-fixtures|score} ...
```

- `--json` gives machine output.
- A non-zero exit code means errors.

**Notebook.** `ops/notebooks/skill_authoring.py` is a Databricks notebook source, the same style as the existing
`ops/notebooks/*`. Its cells are scaffold → validate → generate fixtures → score. It prints the report as a table.
- It runs on a workspace cluster (CLAUDE.md §11 "Operations must run from a workspace cluster/notebook").
- It writes nothing to Delta except the one optional step below.

**Recording Surface 2 results.** A new service function records a passing scored result on the draft's `skill_versions`
row (the column `publish_skill` already reads):

```python
record_skill_surface2(ctx, skill_id, version, results, *, actor)
```

- It appends a trace-style audit event.
- Publishing stays the existing guard, and a named reviewer is still required. The kit never publishes.

### 2.3 Planted-fixture generator (CLAUDE.md §9: never its own oracle)

**Generator.** `fixtures.py`:

```python
generate_fixtures(skill_dir, plants_path, out_dir) -> dict[source, rows]
```

**The sidecar `plants.yaml`** is hand-authored and schema-checked by the new `orchestrator/schemas/plants.schema.json`.
It is the only oracle:

```yaml
seed: 20260925
natural_id_column: Fixture Line Id     # extra non-contract column added to every generated source
background:                            # rows the AUTHOR declares clean
  expense_report:
    count: 250
    template: {Payment Type: Corporate Card, Reimbursement Currency: AUD}
    vary:                              # declared variation only: choice | int_range | date_range | decimal_range
      Transaction Date: {date_range: [2025-01-01, 2025-12-31]}
      Expense Amount (reimbursement currency): {decimal_range: [10, 300]}
tests:
  T4.1:
    scoring_unit: row                  # row | group
    source: missing_receipt
    rows:
      - {natural_id: MR-001, exception: true,  fields: {...}}
      - {natural_id: MR-101, exception: false, fields: {...}}   # near-miss
  T5.2:
    scoring_unit: group
    groups:
      - {natural_id: DUP-01, exception: true, members: [{natural_id: D1a, fields: {...}}, {natural_id: D1b, fields: {...}}]}
```

**Generator rules.**
- Values for every contract column come from the declared `template`/`vary`/`fields` only.
- A contract column with no declared value in a non-nullable position is a **generator error**. It is never filled with
  a guessed value (NN14 applies to fixtures too).
- Randomness uses seeds derived from sha256(seed, source), because Python's `hash` changes per process
  (PYTHONHASHSEED; the G9 lesson, `test_fixture_generation.py`).
- Output uses the contract's `format`/`file`/`sheet`. The output must pass `validate_contract`.

**Generic scorer.** `score.py`:

```python
score_fixtures(skill_dir, plants, data_dir) -> {test_id: TestScore}
```

- **Row grain.** The expected set is the natural ids with `exception: true`. The flagged set is read from the flagged
  rows' `natural_id_column`.
- **Group grain.** A group is its frozenset of member natural ids. A flagged group matches a planted group only when
  the member sets are equal.
- **Anything flagged that no plant declares counts as a false positive.** Background rows are declared clean, so a
  background row that trips a test is an authoring error, and precision exposes it. It is never relabelled.
- SKILL-001 keeps its bespoke `orchestrator/eval/surface2.py` and generator unchanged. That avoids churn to a
  gate-passing harness.
- The generic scorer is validated against a SKILL-001 run: its per-test counts must equal `surface2.py`'s on the
  committed fixture, where the scoring units coincide. Tests where the units differ are listed and excluded from that
  comparison, with a reason.

### 2.4 Tests

| Kind | Test |
|---|---|
| Unit | `tests/test_authoring_scaffold.py`: a scaffolded Skill loads, validates and hashes; `timezone` is required; sample inference writes `pii: true` and `# REVIEW` comments |
| Unit | `tests/test_authoring_validate.py`: one broken copy of `tests/fixtures/skills/mini` per check, each producing exactly its named error; collect-all reports errors from two files at once; `custom.py` → warning; CLI exit codes and `--json` |
| Unit | `tests/test_authoring_fixtures.py`: deterministic across two subprocesses; undeclared non-nullable column → error; output passes `validate_contract`; a background row that trips a test is scored as a false positive |
| Regression | SKILL-001 passes `validate_skill_dir` with zero errors (the new referential check included); moved G8/lock tests still pass |
| Cross-check | Generic scorer == `surface2.py` on the SKILL-001 committed fixture (for the coinciding tests) |
| Live | **L-K1:** run the notebook on a workspace cluster: scaffold → validate → generate → score for `tests/fixtures/skills/mini`; the report prints; no Delta writes except the optional record step |

---

## 3. P7 review workflow (build-here item 6)

### 3.1 What exists already

| Capability | Where |
|---|---|
| Single sign-off, never blocked on identity; `self_approved = actor == run_owner`; `SOD_ENFORCED = False` | `orchestrator/signoff_policy.py:150-163` |
| Sign-off refuses undecided candidates, snapshots narration and decisions, moves findings draft→…→approved, and emits `signed_off` | `runs.sign_off` `runs.py:180-244`, `_advance_findings_to_approved` `:32` |
| Schema: `review_notes` (open/cleared), `findings.review_state` (draft/prepared/reviewed/approved), `runs.prepared_by/reviewed_by/approved_by` | `ddl/delta/002_p1b_suite.sql:175-229` (empty or unused except `approved_by`) |
| `set_finding_review_state` advances one step only | `persistence_local.py:964-973` |
| G14 narrative edit trail: version, origin/action, actor, at, before/after, diff, reason, call_id | `ddl/delta/011_p6_narration.sql:36`, `service.edit_narrative` `:2880` |
| Self-approval label | `components.run_card:153-157` (`/runs`), `run_status._signoff_text:87`, XLSX `fieldwork.py:1424-1429`, PPTX `pptx_export.py:318,795`; derived again in `service.list_runs` (`:2085`, derivation at `:2168`) |
| Identity | `X-Forwarded-Email` / `X-Forwarded-User` headers. `local-user` is allowed only on the local backend (`run_status.py:71-84`, `run_setup.py:95`, `adapters.MissingIdentityHeader`) |
| Themes | Themes are stored (`finding_themes`); §4.6 "auditor confirms them at sign-off" is **not implemented** |

### 3.2 Gap

There is no preparer or reviewer step, no role check, no review-note lifecycle and no theme confirmation. Self-approval
is universal.

### 3.3 Workflow

The run's `status` stays `awaiting_signoff` for the whole review, so the state machine, the `runs` status CHECK and every
status query are unchanged. A new lifecycle field `RunState.review` holds the stage:

```
awaiting_signoff ── stage: preparation ──[Mark as prepared]──▶ stage: review ──[Mark as reviewed]──▶ stage: approval ──[Sign off findings]──▶ queued/export
                         ▲                                        │                                      │
                         └────────────[Return to preparer, reason]┴──────────────────────────────────────┘
```

| Action | Who (configured group, §3.5) | Preconditions | Effect |
|---|---|---|---|
| Edit narrative, decide candidates | Preparer | Stage `preparation` | Existing functions, plus a stage check. `narrative_edits.review_stage` is recorded |
| **Mark as prepared** | Preparer | Every candidate decided (the check moves here from sign-off, which re-checks it); no open notes | Findings → `prepared`. `runs.prepared_by`. The current theme generation is recorded as **confirmed** (this closes the §4.6 theme-confirmation gap, so no new theme UI is needed) |
| **Raise note** (on the run or a finding) | Reviewer or approver | Stage `review` or `approval` | `review_notes` row, `open` |
| **Respond to note** | Preparer | Note open | `response`, `responded_by/at`. During `review`/`approval` the preparer may also edit narrative text in reply to a note. Such edits are logged with `note_id` |
| **Clear note** | The raiser, or anyone holding the raiser's role | Note responded | `cleared` |
| **Mark as reviewed** | Reviewer | Stage `review`; zero open notes | Findings → `reviewed`. `runs.reviewed_by` |
| **Return to preparer** | Reviewer or approver | Stage `review` or `approval`; a reason is given | Stage → `preparation`. Findings → `draft` (new persistence method, recorded). Candidate decisions **stand**: changing one needs Regenerate, which supersedes candidates, as today. Open notes stay open |
| **Sign off findings** (existing button) | Approver | Stage `approval`; zero open notes; all candidates decided | The existing `runs.sign_off`, with `signoff` extended (§3.4). Findings → `approved`. Export phase |
| Regenerate narration | Preparer | Stage `preparation` | Existing behaviour |

**Segregation of duties** is evaluated per run on identities, not on group membership (D-P7-5). Preparer, reviewer and
approver must be three distinct people. `run_owner` may be the preparer. A person in several groups can hold any
**one** role on a given run.

Every refused action writes a `review_action_refused` trace event naming the actor, the attempted action and the
reason. It is shown to the user through the existing `_error_panel`.

### 3.4 Data model: migration `015_p7_review_workflow` (Delta + SQLite mirrors)

(`014` is item 1's `014_p7_run_inputs`. If another branch claims either number first, renumber at merge: migration
checksums are per file, and neither is applied anywhere yet.)

```sql
CREATE TABLE review_steps (              -- append-only evidence of every review action
  step_id STRING NOT NULL, run_id STRING NOT NULL, engagement_id STRING,
  action STRING NOT NULL,                -- prepared | reviewed | returned | approved
  actor STRING NOT NULL, role STRING NOT NULL,
  matched_group STRING, role_source STRING NOT NULL,   -- workspace_groups | config
  sod_mode STRING NOT NULL,              -- enforced | labelled
  reason STRING, theme_generation INT, narration_generation INT,
  state_version INT NOT NULL, at TIMESTAMP NOT NULL,
  CONSTRAINT review_steps_pk PRIMARY KEY (step_id)
) TBLPROPERTIES ('delta.appendOnly' = 'true');
ALTER TABLE review_notes ADD COLUMNS (raised_role STRING, responded_by STRING, responded_at TIMESTAMP, cleared_role STRING);
ALTER TABLE narrative_edits ADD COLUMNS (review_stage STRING, note_id STRING);
ALTER TABLE runs ADD COLUMNS (prepared_at TIMESTAMP, reviewed_at TIMESTAMP);
```

- **`step_id`** is deterministic: `sha256(run_id:action:state_version)`, so a replayed click is a no-op (the
  `_trace_event_id` pattern).
- **`RunState.review`** is a lifecycle field with default `None`, so a pre-P7 JSON loads unchanged:

  ```
  {stage, prepared: {actor, at, role_source}, reviewed: {...}, returns: [{actor, at, reason}]}
  ```

  Each stage change is a CAS `save_state` with no status change. A stale version is rejected, and two reviewers racing
  get exactly one winner.
- **`RunState.signoff` gains** `prepared_by`, `reviewed_by`, `sod_mode`, `role_source`, `sod_waived: [pairs]` and
  `open_notes_at_signoff: 0`.
  - `self_approved` is kept, and means "some SoD rule was waived".
  - The `status.py:45` gate becomes: `signoff is not None and (signoff.sod_mode == "labelled" or (prepared_by and
    reviewed_by))`.
  - A legacy signoff (no `sod_mode`) passes as it does today. It only matters for runs already past the gate.
- **New persistence methods** (Protocol + Local + Delta, one contract test):
  - `append_review_step`, `list_review_steps`;
  - `add_review_note`, `respond_review_note`, `clear_review_note` (CAS on `state`), `list_review_notes`;
  - `reset_findings_review_state(run_id, *, actor, now, reason)`, which is the only backwards move and is recorded.

### 3.5 Identity and roles

- **Identity** is unchanged: the forwarded headers, and `local-user` on the local backend only. The local backend also
  honours an `X-Forwarded-Email` header when present, which is how e2e tests act as several people.
- **Roles.** New `orchestrator/identity.py` provides `RoleResolver.roles_for(email) -> {role: matched_group}`, with two
  implementations chosen by `REVIEW_ROLE_SOURCE`:
  - **`workspace_groups`.** The App's service principal calls
    `WorkspaceClient().users.list(filter='userName eq "<email>"', attributes="userName,groups")` and reads
    `groups[].display`.
    - Only direct membership counts. Nested groups are not expanded, and this is stated.
    - The lookup runs on action clicks only, never while polling. It is a REST call, not a warehouse query, so it adds
      no idle cost.
    - A 60 s in-process cache applies.
    - A lookup failure refuses the action ("could not verify group membership"). It never falls back to config.
  - **`config`.** A gitignored YAML (`REVIEW_ROLE_ASSIGNMENTS`, shaped `{email: [role, ...]}`) for development and the
    local backend. The source is recorded on every step (`role_source: config`) and in the XLSX, so a config-derived
    role is never mistaken for a verified one.
- **Verifiable here vs at the corporate port.**
  - Here: headers, SCIM user lookup by the App SP, and group names from a test group. Live item L-R3.
  - At the port: whether the corporate App SP may read SCIM users, whether account-level groups appear in workspace
    SCIM, and what the corporate group names are.
  - Also at the port: on-behalf-of-user (`X-Forwarded-Access-Token` with `current_user.me()`) is the stronger source
    and stays a governance switch (build-here item 9). It is not built now.
  - These items go in `docs/CAPABILITY_MATRIX.md`, for the `mechanical` agent.

### 3.6 Config (`orchestrator/config.py`, `.env.example`)

```
REVIEW_SOD_MODE=enforced                   # enforced | labelled   (code default: enforced)
REVIEW_ROLE_SOURCE=workspace_groups        # workspace_groups | config
REVIEW_PREPARER_GROUPS=audit-preparers     # comma-separated, exact group display names (D-P7-6)
REVIEW_REVIEWER_GROUPS=audit-reviewers
REVIEW_APPROVER_GROUPS=audit-approvers
REVIEW_ROLE_ASSIGNMENTS=                   # path to gitignored YAML; required iff REVIEW_ROLE_SOURCE=config
```

- An unset group variable while in `enforced` mode is a `ConfigError` at App start, and `/ready` reports it. There is no
  default group that silently admits everyone.
- In `labelled` mode, the role checks still run, but the distinct-person rule is waived and labelled.
- **All six are excluded from `runtime_config_hash`**, added to `_RUNTIME_HASH_EXCLUDED_FIELDS`. The reason: the review
  policy never changes a number or a finding, and hashing it would make `verify_fingerprint`, which runs at every
  executor pass, refuse to export a paused run after a policy change. The policy in force is instead recorded on every
  `review_steps` row and in `signoff`.

### 3.7 Labels: what replaces self-approval

- `signoff_policy.py` becomes the policy module:

  ```python
  evaluate_step(action, actor, run_review, policy) -> Allowed | Refused(reason)
  label_for(signoff | runs_row) -> str | None
  ```

  `SOD_ENFORCED` is removed, and the policy comes from config.
- The existing text **"Self-approved — segregation of duties not enforced"** stays **verbatim**. It appears wherever it
  appears today, whenever `signoff.sod_waived` is non-empty (labelled mode) or the signoff is legacy with approver ==
  owner. So `/runs` needs **no change**: `components.run_card` still reads `run["self_approved"]`, and `list_runs`
  derives it through `label_for` from `prepared_by/reviewed_by/approved_by/run_owner`, falling back to the legacy rule
  when `prepared_by` is null.
- The trace `signed_off` message becomes "Findings signed off by A (prepared by P, reviewed by R)", plus the existing
  self-approved suffix when waived.
- The XLSX cover gains `prepared_by`, `prepared_at`, `reviewed_by`, `reviewed_at`, `sod_mode` and `role_source`, plus a
  **"Review notes"** sheet (body, target, raised by/role/at, response, cleared by/at).
- The PPTX sign-off line becomes "Prepared by P · Reviewed by R · Signed off by A on T". G13 covers the new fields.

**Existing runs.** No data migration. A run signed off before this deploy keeps its `signoff` and its label, which is
historically true. A run sitting at `awaiting_signoff` at deploy time enters stage `preparation` (D-P7-9).
`scripts/deploy_app.py`'s existing paused-run warning lists these runs.

### 3.8 UI items (item 3): all on `/run/<id>`, `awaiting_signoff` and `completed` blocks

| # | Element (existing components only) | Text | Why |
|---|---|---|---|
| UI-R1 | One `P.sub` line under the existing H3 "Findings are ready for sign-off" | "Review stage: preparation", "Prepared by P at T — awaiting review" or "Reviewed by R at T — awaiting sign-off" | Each person can see whose turn it is |
| UI-R2 | Two `html.Button` (`btn-generate`), shown only in their own stage, in the slot of the existing "Sign off findings" button, which now shows only in stage `approval` | "Mark as prepared", "Mark as reviewed" | The two new steps |
| UI-R3 | `dcc.Input` (the candidate-reason pattern, `run_status.py:308`) + `html.Button` (`ghost`), stages review/approval | "Reason for returning" placeholder; "Return to preparer" | Lets the reviewer or approver send the run back |
| UI-R4 | A **"Review notes"** `panel`. Per note: a `chip` (Open/Responded/Cleared), the body as `P`, and `P.sub` lines "Raised by X (reviewer) at T · on finding <title>" and "Response by P: …". Per open note: `dcc.Input` + `ghost` buttons "Respond" and "Clear". A raise row: `dcc.Dropdown` (the candidate-severity pattern) with "Whole run" plus each finding title, a `dcc.Input`, and a `ghost` "Raise note" button | as listed | Notes are the review mechanism, and they block review and sign-off |
| UI-R5 | The existing `completed`-block `P.sub` (`_signoff_text`) | "Prepared by P, reviewed by R, signed off by A at T", plus the existing label when waived | Shows who did each step |
| UI-R6 | Refusal texts through the existing `_error_panel` | "You are not in a preparer/reviewer/approver group (<groups>).", "Segregation of duties: you already acted on this run as <role>.", "Clear every open review note first (N open).", "Text can only be edited during preparation — ask the reviewer to return the run.", "Could not verify group membership — try again." | The rules must be visible when they bite |

- Buttons are **not** hidden by role, because that would need per-render group lookups and would hide the rule from the
  person it applies to. They are refused on click.
- Existing narrative-edit and candidate controls stay visible after preparation, and the server refuses them with a
  UI-R6 message.
- **Prototype pages** (`/runs`, `/workspace/tne`, `/trace`, `/actions`) change **nothing**. Trace rows are data.

### 3.9 Trace events

- New events: `review_prepared`, `review_note_raised`, `review_note_responded`, `review_note_cleared`, `review_reviewed`,
  `review_returned` and `review_action_refused`.
- `signed_off` gets the message change in §3.7.
- Event ids use `_service_trace_event_id(run_id, action, row_id)` (`service.py:2588`), so back-to-back actions never
  collide.

### 3.10 Tests

| Kind | Test |
|---|---|
| Unit policy | `tests/test_review_policy.py`: the full role × stage × action matrix; each SoD pair refused in `enforced` and waived and labelled in `labelled`; `label_for` over legacy, enforced and labelled signoffs |
| Unit identity | `tests/test_identity.py`: SCIM resolver against a fake `WorkspaceClient` (direct groups, a user not found, API error → refused, never fallback); config resolver; unset groups in enforced mode → `ConfigError` |
| Contract | The same persistence contract test on Local and Delta for review steps, notes (CAS clear) and the review-state reset |
| Workflow | `tests/test_review_workflow.py`: happy path P→R→A; open note blocks review and sign-off; return resets findings to draft and keeps candidate decisions; racing "Mark as reviewed" → exactly one winner (CAS); a replayed step is a no-op; edit after preparation refused; theme generation recorded at prepare |
| Failure injection | Crash between `append_review_step` and `save_state` → retry is idempotent (deterministic `step_id`) and the stage advances once |
| Gate | Enforced-mode signoff without `prepared_by`/`reviewed_by` fails at `status.py`, even if `runs.sign_off` were bypassed |
| Export (G13) | XLSX cover and "Review notes" sheet, and the PPTX line, equal the values in `RunState`/`review_steps` |
| Migration | A pre-P7 `RunState` JSON loads; a legacy self-approved run still shows the exact label on `/runs`, `/run/<id>` and in the XLSX |
| Layout parity | The prototype-page parity test is unchanged and green (no allow-list entries) |
| Browser (`app/tests/e2e_local/test_review_ui.py`) | Three Playwright contexts with different `X-Forwarded-Email` headers and `REVIEW_ROLE_SOURCE=config`: prepare → a note raised → responded → cleared → reviewed → signed off → XLSX downloads; the preparer trying to review sees the SoD message; state survives a reload and an App restart (CLAUDE.md P7 DoD) |
| Live (final test plan) | **L-R1:** in the dev App (`labelled` mode, one human), the full workflow completes, and the label appears on `/run/<id>`, `/runs` and in the XLSX. **L-R2:** the run paused at `review` survives an App restart and resumes from Delta. **L-R3:** `workspace_groups` source with a test group containing the developer → the role resolves, and `role_source: workspace_groups` is recorded; a user not in the group is refused. **L-R4:** with two workspace identities if available (D-P7-7), `enforced` mode refuses the second role for the same person. Pass criteria: every event is in `trace_events`, every step is in `review_steps`, `runs.prepared_by/reviewed_by/approved_by` are set, and there is no warehouse query from polling (idle-cost check green) |

---

## 4. Implementation batches

Each batch is sized for one or two Sonnet agents, ends green, and commits. **Parallel lanes:** A (item 1), K (item 2)
and R (item 3) are independent. Within a lane, the batches run in order.

Shared files, with merge order:
- `service.py`: B1b then B3b, disjoint functions.
- `status.py`: B1b then B3b, different gate lambdas.
- `run_status.py`: B1c then B3c.
- `fieldwork.py`: B1c then B3b, different XLSX regions.
- `persistence_*.py`: B1a then B3a, different methods.

| Batch | Lane | Files | Content |
|---|---|---|---|
| **B1a** | A | `orchestrator/run_inputs.py` (new), `orchestrator/source_bindings.py`, `orchestrator/schemas/contract.schema.json`, `orchestrator/skills.py`, `orchestrator/fingerprint.py`, `orchestrator/ddl/{delta,sqlite}/014_p7_run_inputs.sql`, `orchestrator/adapters/persistence_{local,delta}.py` (fingerprint column), tests | Declarations and validation, test→source dependencies and `requires_sources`, the risk/control referential check (§2.1), and `run_inputs_hash`. SKILL-001 `contract.yaml` parameters, a version bump and `content.lock` |
| **B1b** | A | `orchestrator/adapters/mapped_source.py` (new), `orchestrator/engine.py`, `orchestrator/nodes/fieldwork.py` (profile/reconciliation only), `orchestrator/status.py`, `orchestrator/service.py` (`start_audit_run`, `suggest_bindings`, `restart_stale_run`, factory wrapping), `app/src/run_setup.py` (`_auto_bind` treats `not_supplied` as bound), tests | Apply mappings, unsupplied sources → not_testable, the mandatory-confirmation gate, the `run_inputs_applied` trace event, G9 extension |
| **B1c** | A | `app/src/run_status.py` (UI-M1), `app/src/workspace_tne.py` (`_fmt_source_ref`, UI-M2 if approved), `orchestrator/nodes/fieldwork.py` (XLSX "Run inputs" sheet), `orchestrator/pptx_export.py` (one methodology line), `app/tests/e2e_local/test_run_inputs_ui.py` | Show run inputs in the UI and exports |
| **B2a** | K | `orchestrator/authoring/{__init__,__main__,scaffold,checks,report}.py` (new), `tests/test_g8_thresholds.py`, `tests/test_skill_content_lock.py` (call the moved checks), `ops/notebooks/skill_authoring.py` (new), `orchestrator/service.py` (`record_skill_surface2` only; append-only hunk at end of the Skills section), tests | Scaffold, collect-all validator, CLI, notebook |
| **B2b** | K | `orchestrator/authoring/{fixtures,score}.py` (new), `orchestrator/schemas/plants.schema.json` (new), `tests/fixtures/skills/mini/plants.yaml` (new), `tests/test_authoring_fixtures.py` | Generator, generic scorer, SKILL-001 cross-check. Parallel with B2a; B2a's CLI imports it through a lazy import stub agreed up front |
| **B3a** | R | `orchestrator/ddl/{delta,sqlite}/015_p7_review_workflow.sql`, `orchestrator/adapters/{protocols,persistence_local,persistence_delta}.py` (review methods), `orchestrator/state.py` (`review` field), `orchestrator/identity.py` (new), `orchestrator/config.py`, `.env.example`, `orchestrator/signoff_policy.py`, tests (policy, identity, persistence contract) | Schema, persistence, policy, role resolution |
| **B3b** | R | `orchestrator/runs.py` (prepare/review/return/notes/sign_off), `orchestrator/service.py` (wrappers, `list_runs` label derivation, stage checks in `edit_narrative`/`decide_candidate`/`regenerate_narration`), `orchestrator/status.py`, `orchestrator/nodes/fieldwork.py` (XLSX cover + notes sheet), `orchestrator/pptx_export.py`, tests (workflow, gate, failure injection, G13) | Workflow and exports |
| **B3c** | R | `app/src/run_status.py` (UI-R1–R6 + callbacks), `app/src/platform/adapters.py` (thin wrappers), `app/tests/test_run_status_review.py`, `app/tests/e2e_local/test_review_ui.py`, `app/tests/e2e_local/conftest.py` (multi-identity contexts) | UI after approval |
| **B-docs** | — (`mechanical`, Haiku) | `docs/CAPABILITY_MATRIX.md`, CLAUDE.md decision record | Record §3.5 port items and the user's answers to §5 |

UI batches B1c and B3c start only after the user approves the relevant UI items. Every other batch can start at once.

---

## 5. Decisions for the user

Everything not listed here is decided above.

| # | Question | Options | Recommendation | Why |
|---|---|---|---|---|
| **D-P7-1** | Where is a project's column mapping, parameter overrides and unsupplied sources declared? | (a) **In the existing `SOURCE_BINDINGS` file, per environment, zero UI**; (b) a new mapping step on the landing page | **(a)** | No prototype-page change. It is what Genie Code at the port supplies anyway ("configuration, real data mapping"). It is exact, reviewable and fingerprinted. (b) is a new screen on a prototype page |
| **D-P7-2** | Approve UI-M1: run inputs listed in the `/run/<id>` plan-confirmation block? | approve / change text / reject | **Approve** | A mandatory confirmation must show what is being confirmed |
| **D-P7-3** | Approve UI-M2: mapping and parameter suffix on the `/workspace/tne` evidence `source_ref` cell? | (a) **approve** (text in an existing cell, no new element); (b) provenance only in the XLSX/PPTX and `/run/<id>` | **(a)** | "Shows in finding provenance" is the requirement, and this cell is where finding provenance already lives |
| **D-P7-4** | Which SKILL-001 sources become `optional`? | (a) **none now**; (b) the ones you name | **(a)** | Your 2026-09-24 decision makes a missing per-diem file fail the run. The feature is tested on the fixture Skill. Any source can be made optional later with a version bump |
| **D-P7-5** | SoD rule | (a) **preparer, reviewer and approver all different people; the run owner may be the preparer**; (b) allow preparer = reviewer (two-person); (c) other | **(a)** | Three distinct roles is the audit norm (§4.8), and (b) is a one-line config change later if the team is small |
| **D-P7-6** | Default group names (config; the corporate ones are supplied at the port) | **`audit-preparers`, `audit-reviewers`, `audit-approvers`** / your names | **As shown** | They are placeholders only. Enforced mode refuses to start if they are unset |
| **D-P7-7** | Development workspace: allow self-approval? | (a) **`REVIEW_SOD_MODE=labelled` in dev (roles checked, same person allowed, the existing label shown)**, `enforced` as the code default and at the port; (b) enforced in dev too (needs a second workspace user for acceptance) | **(a)**, plus L-R4 with a second user if you can add one | One human cannot otherwise complete the workflow here, and the label keeps it honest |
| **D-P7-8** | Development workspace: where do roles come from? | (a) **workspace groups (a test group you create), config fallback only for the local backend/e2e**; (b) the config file in dev too | **(a)** | It exercises the same path the port uses. `role_source` is recorded either way |
| **D-P7-9** | Runs signed off before this deploy, and runs waiting at sign-off at deploy time | (a) **keep old sign-offs and their label unchanged; waiting runs enter the new workflow at "preparation"**; (b) re-open old runs for review | **(a)** | The old records are true as recorded, and re-opening a completed, exported run would contradict the export |
| **D-P7-10** | Who may edit model-written text and decide AI-proposed findings? | (a) **the preparer during preparation (and in reply to a note); reviewers and approvers raise notes or return the run**; (b) the reviewer may also edit | **(a)** | A reviewer who edits reviews their own work, which defeats the review. Notes keep the dialogue on the record |
| **D-P7-11** | Approve UI-R1 to UI-R6 on `/run/<id>` | approve all / per item | **Approve all** | All are on the approved gate page, from existing components. There are no prototype-page changes. The authoring kit (item 2) has no UI |
