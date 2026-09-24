# AI Audit Analyst — Build Brief v2

**Status:** supersedes v1 entirely. v1 specified a LangGraph backend and preserved behaviour
that turned out to be unsafe to preserve. If you have seen v1, discard it.

You are building a domain-agnostic Internal Audit analytics platform on Databricks.
A polished Dash front end already exists. Your job is to put a real, governed, auditable
execution engine underneath it without losing the UI.

Read this file in full before proposing anything. Where it conflicts with code comments,
docstrings, or the README, **this file wins** — several docstrings in the repo are factually
wrong (see §2.3).

---

## 0. Ground truth about the current repository

Read these before writing code:

- `reference_app/app.py` (2,468 lines — the whole app)
- `reference_app/src/computation.py` (the real detection logic)
- `reference_app/src/test_catalogue.py` (14 test definitions)
- `reference_app/src/platform/adapters.py` (the service interface you will implement)
- `reference_app/src/platform/pages.py`, `components.py`, `methodology.py`

### 0.1 What actually works

- Dash app with routes `/`, `/workspace/tne`, `/runs`, `/skills`, `/skills/<id>`, `/actions`, `/trace`.
- A complete design system (`assets/theme.css`, ~760 lines) and Optus PPTX template.
- Excel and PPTX exporters.
- A test catalogue with 14 T&E control tests.
- `src/computation.py` — genuine detection logic (groupby duplicates, split claims, per-diem).

### 0.2 Why the prototype looks like this, and what is not wired up

**Read this as context, not as an audit of careless work.** The prototype was built by an agentic
coding tool under three hard constraints, and it succeeded at what it was actually for:
demonstrating the UI, the audit approach and the export pipeline to stakeholders. The constraints
were:

1. **No Databricks Model Serving.** Every LLM-shaped decision had to be made at authoring time and
   written into code.
2. **No reliable access to the raw source files.** The eight files in `FILE_REGISTRY` were not
   dependably available, so the app was built to work from whatever data it could obtain.
3. **No backend at all.** No job runner, no state store, no persistence — everything computed at
   import and held in module globals.

Almost everything in this section follows from those three. The code is not wrong for a prototype;
it is wrong for a governed audit platform, which is what the build turns it into.

#### What is not wired up

**`src/computation.py` is the intended detection engine, and `app.py` does not import it.**
Check `app.py:24–49`. This is a consequence of constraint 2, not an oversight: `computation.py`
consumes eight raw source files — `expense_report`, `booking_detail`, `travel_request_segment`,
`travel_requests_no_expense`, `missing_receipt`, `attendee_validity`, `approval_aging`,
`per_diem_rates` — and when those were not reachable, the app had to be built against pre-flagged
data instead.

**`computation.py` is the best available behavioural reference, not an unquestionable oracle.**
P2 ports its detection logic into primitives, but an independent review found material semantic
defects that must not be reproduced:
- T3.1b joins bookings to travel requests by employee name rather than a request key (`:82`).
- T3.3b compares the full claim amount against $40/$80 instead of per-head spend (`:202`).
- T5.1 split-claim window sums the entire employee/vendor/type group after finding any pair
  within two days (`:365`) — the population inflates.
- T6.1d says "per employee per country" but groups only by employee and date (`:562`).
- Several functions still use fuzzy column selection or turn missing inputs into zero results.

**P2 must port against an audit-approved test specification, not against `computation.py` output.**
For each of the 14 tests, the P2 planning session writes: population, grain, join key, exception
identity, **scoring unit** (row, transaction pair, employee-day, claim group — precision and recall
are not interpretable without it), threshold, exclusions, and expected evidence. Where
`computation.py` and the spec disagree, the spec wins.

**`app.py` therefore counts flags rather than computing tests.** `compute_evidence_payload`
(`app.py:315`) derives most metrics by summing pre-existing `RF_*` columns —
`flag_count("RF_CS_SplitClaims_SameDay")` and similar (lines 324–362). Those columns came from an
earlier run of the detection logic elsewhere, not from this process. Where the two approaches
overlap they differ, and `computation.py` is the one to follow:

| Test | `computation.py` (behavioural reference) | `app.py` (workaround) |
|---|---|---|
| T6.1d daily spend | Per-diem-rate driven (`:568`) | Hardcoded `$1,000` (`:415`) |
| T3.3a late booking | `Advance Purchase Days` (`:147`) | `RF_CS_LateBooking` flag (`:388`) |

**`src/narrative.py` and `src/guardrail.py` are also unwired, for constraint 1.** `narrative.py` is
the *intended* LLM layer, never called because there was no endpoint — not a failed design. Its
T&E test-ID framework (`:46–60`) and tone instructions are the closest thing to a specification for
the real prompts in P6. Do not import it; do use it as reference. `guardrail.py`'s numeric check is
a reasonable first pass whose approach does not hold up (§5, G11), and P6 rewrites it.

`src/eval_gate.py` was deleted in P0: it was a stub whose docstring described a pipeline that never
existed.

#### What must not survive into the build

These are sound prototype choices and unsound production ones. The difference is that a demo must
never crash, whereas a governed audit run must **fail loudly rather than proceed on a guess**.

**Placeholder values for absent columns.** `_standardise_combined`, `_standardise_pre` and
`_standardise_approval` (`app.py:107–226`) insert `"Unknown"`, `0` and `2025-01-01` when a column
is missing, so the UI always renders. Defensive coding for a demo; a silent wrong answer on real
data. If `Transaction Date` is absent, every row becomes 2025-01-01, every date filter then matches
everything, **the population reconciliation still balances**, and nothing signals that anything went
wrong. `_find_col` (`:65–77`) has the same shape — it matches any column whose name merely
*contains* a candidate substring, so `"amount"` can bind to the wrong column.

Non-negotiable 14 replaces all of it: a contract violation fails the run.

**Values held constant because they could not be computed.** These were sensible placeholders —
real prior-audit figures used so the prototype showed realistic numbers — and every one must become
either a real computation or an explicit `not_testable` result with a reason string. Never a number.

- `compute_test_4_3_cached()` → `flagged: 132` (`computation.py:288`). Note this one is deliberate:
  T4.3 is a GenAI classification test the docstring says takes ~5 minutes, and with no endpoint
  available caching a known result was the only option. It moves to the `classify` node in P6.
- `compute_test_6_1c()` → `exceptions: 0`, "Based on audit findings" (`:529`)
- `intl_exceptions: 2, # From audit findings` (`:590`)
- `total_records: 152_921`, `total_files: 8` (`:641–642`)
- `aus_rate = 500  # Fallback reasonable ATO rate` (`:576`)

**Generated demo data.** `build_demo_data` (`app.py:229–287`) produces a deterministic seeded
population with random `RF_*` flags (`rng.random(n) < p`). The right call for a prototype with no
data; it is not synthetic audit data and no finding derived from it means anything. Deleted in P3,
replaced by a completed run over a real synthetic dataset, persisted and clearly labelled (§9).

**Genuine bug, unrelated to any constraint:** `computation.py:390` —
`same_day_splits if 'same_day_splits' in dir() else ...` works by accident. Fix it when porting.

#### Hardcoded values that violate the portability contract (§3.16)

`app.yaml` was fixed in P0; these remain and belong to the phase that touches each file:

- `src/platform/adapters.py:71` — `_UPLOAD_BASE = "/Volumes/sdpt_gia/ep_temp/taxgovernance"`.
  Must come from `DBX_VOLUME`. Fix in **P5** with the real upload implementation.
- `src/platform/fixtures.py` — `sdpt_gia.*` table names and `data-engineering@optus.com.au` owners
  throughout the demo data. Fix in **P3** when fixtures give way to Delta reads.
- `src/narrative.py:23` — `"You are an internal audit analyst for Optus."` Delete with the file in P6.

A CI grep test for these patterns is part of P9. Until then:
`grep -rniE 'dvlp_11|ia_dart|sdpt_gia|optus|adb-[0-9]' --include=*.py --include=*.yaml .`

### 0.3 The exposure number on the executive brief is wrong

`app.py:1625` shows "Potential financial exposure of $X" where X is
`sum(financial_exposure)` across findings, and each finding's exposure is
`max(amount over cited metrics)` (`app.py:919`). Populations overlap — the same $6,000 claim
is counted in `hv_amount`, `daily_over_amount` and possibly `missing_receipt_amount`.
The risk score `sev*30 + recurrence*0.4 + exposure_pct*0.3` (`app.py:926`) has no stated basis.
Both are fixed in P3.

### 0.4 The findings are frozen LLM output, not a designed rule set

`build_all_findings` (`app.py:528–895`) looks like a rules engine. It is not. It is the output of
an agentic coding tool that looked at the data once, decided what an auditor should care about,
and wrote that judgement into Python as f-strings. The app has never called a model at runtime
because model serving was unavailable when it was built.

This matters in two ways.

**It is not a problem.** An LLM authoring a rule set, which a human then reviews and versions, is
sound — it is reproducible, inspectable and defensible. It is exactly what Explorer Mode
(§4.5) automates. Do not "fix" it by moving finding selection to runtime.

**But the thresholds have no source.** `>10% missing receipts = High` (`app.py:574`),
`>30% no-receipt-viewed = High` (`app.py:547`), `>50 daily exceedances = High` (`app.py:869`)
and the rest were invented by the authoring model. They are not from any T&E policy. In P2 every
threshold moves to `thresholds.yaml` and must carry either a real policy reference or
`provenance: analyst-set` with `pending_policy_confirmation: true` — and the UI must display that
label wherever the threshold drives a severity. A finding whose severity rests on an unattributed
number is not defensible in a CAO meeting.

### 0.5 Data-model problems to fix when porting

- **`EXCO_MEMBERS` is a list of human names** (`data_loader.py:242`) and every population filter
  joins on it (`app.py:235`, `computation.py:45`, `:93`, `:111`). Name matching is fragile —
  formatting differences, duplicates, and name changes all silently drop or duplicate rows.
  `contract.yaml` must key on an employee ID, with name as a display attribute only.
- **Currency is unhandled.** The amount column is `"Expense Amount (reimbursement currency)"` —
  the name says the currency varies — yet everything sums it as if it were AUD and
  `contract.yaml` in v1 declared `currency: AUD`. Either the contract requires a single
  currency and the test fails when it is violated, or the pipeline converts at a stated rate
  with the rate recorded in the run. Decide in P2; do not sum mixed currencies.
- **Audit-period boundaries have no timezone.** `filter_by_dates` (`app.py:985`) compares naive
  timestamps. An audit period is a business-calendar concept in a stated timezone; `RunState`
  stores ISO dates and the contract must state the timezone. Off-by-one-day at a period boundary
  is a reproducibility failure and a reconciliation failure.

---

## 1. Product vision

**Positioning:** a governed, full-population fieldwork analytics engine with explicit evidence
contracts and bounded AI narration. The long-term target is PwC's Internal Audit Orchestration
Suite capability set; the credible near-term pitch is deterministic, repeatable, auditable
transaction testing — structured-data-first, hybrid evidence later.

One governed application serving multiple audit use cases through:

- Reusable **Skills** — versioned audit methodologies, mostly declarative
- **Playbook Mode** (run a saved Skill) and **Explorer Mode** (author a new one)
- Governed data discovery from Unity Catalog plus business file upload
- A deterministic pipeline with LLM narration at bounded points
- Evidence-linked findings with full source provenance (table version / file hash → row keys)
- Persistent runs and management actions in Delta
- Excel / PowerPoint / HTML exports
- Jira integration behind an explicit approval step (deferred — not built now)

The UX is identical regardless of Skill. The Skill supplies sources, tests, thresholds,
prompts and the workspace layout. T&E ExCo is SKILL-001; T4.8 Input GST is SKILL-002.

---

## 2. Architecture

### 2.1 Runtime — the App is the whole application

Confirmed with a Databricks Solutions Architect: compute runs inside the Databricks App. There is
no Jobs dependency.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Databricks App  (Python / Dash)                                     │
│                                                                      │
│   web tier                    executor (in-process thread pool)      │
│   • UI, routing, forms   ──▶  • the pipeline loop                    │
│   • start_audit_run           • nodes, Skill tests, LLM calls        │
│   • polls Delta, renders      • writes exports to the Volume         │
│   • HITL approval gates       • semaphore-capped, queue in Delta     │
└───────────────┬──────────────────────────────┬───────────────────────┘
                │ read                         │ write
                ▼                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Unity Catalog — serverless SQL warehouse attached as an App resource│
│    runs · run_state · findings · issues · management_actions         │
│    trace_events · uploaded_files · llm_calls · llm_cache             │
│    narrative_edits · risks · controls · risk_assessments             │
│    engagements · review_notes · evaluation_runs                      │
│  Volume: uploads/ exports/     MLflow: per-run, per-node spans        │
│  Model Serving: two endpoints  ·  AI Gateway + inference tables       │
└──────────────────────────────────────────────────────────────────────┘
```

**The web tier never executes a node; the executor never renders UI.** They communicate only
through Delta. Keeping that separation is what makes the executor swappable (§2.3) and the pipeline
testable without a browser.

The App does run Python in callbacks — filtering a completed run's population for charts, streaming
an already-created export on download. The rule: **nothing over a couple of seconds, and nothing
that produces audit evidence, runs in a callback.** Evidence is produced by the executor and
carries a `run_id`. A download callback streams an artifact the export node already wrote to the
Volume; it does not build the artifact.

`start_audit_run` inserts the `runs` row, hands `run_id` to the executor, and returns immediately.
The UI polls Delta on a `dcc.Interval`. A run takes minutes; a Dash callback cannot.

### 2.2 No orchestration framework in the pipeline

**Do not use LangGraph, Airflow, Prefect or Dagster for the audit pipeline.** Delta is the state
store, MLflow is the tracer, and the pipeline is a plain Python loop:

```python
state = persistence.load_state(run_id)
for node in NODES_FOR[state.run_kind][state.phase][state.next_node_index:]:
    state = node(skill, state)
    persistence.save_state(state)
    if state.status in ("awaiting_confirmation", "awaiting_signoff"):
        return
```

Nine nodes for a fieldwork run, linear, no branching, no cycles. A framework would add a second
persistence layer competing with Delta and 40+ transitive dependencies, for nothing. See §4.10 for
the one module where a bounded agentic loop **is** permitted.

### 2.3 Executor — swappable, thread-based by default

`Executor.start(run_id, phase)`, with the pipeline function identical underneath. Selected by
`EXECUTOR` env var.

| Implementation | Use |
|---|---|
| `ThreadExecutor` | **Default.** `ThreadPoolExecutor` inside the App, `Semaphore(2)`, further runs held as `queued` rows in Delta. Also used by pytest. |
| `JobsExecutor` | Optional, not built now. `w.jobs.run_now(job_id, job_parameters={...})`. Reserved for workloads too long for a web container — chiefly risk sensing (§4.10), which can run for hours over a document corpus. |

Keep the interface even though only one implementation exists. It is what lets the pipeline be
tested with no workspace, and what lets a long-running module move out later without touching a
node.

**Four rules that make in-App execution safe. These are requirements, not advice.**

1. **Every node is idempotent.** Re-running a node for the same `run_id` overwrites its own outputs
   and never appends duplicates. State is persisted after every node, so a restart resumes at
   `next_node_index` rather than from the beginning.
2. **A reaper runs on App start.** Any run left `running` was orphaned by a container restart →
   mark `interrupted`, surface a Resume action. **This is load-bearing:** in-App execution means a
   redeploy kills in-flight runs, and the P3 gate tests it explicitly.
3. **Concurrency is capped.** Executor runs share CPU and memory with the web tier. Two concurrent
   runs maximum; the rest queue in Delta.
4. **Memory has a ceiling.** App containers are small. `DataSourceAdapter` must be able to push
   aggregation and filtering into the SQL warehouse rather than loading a whole population into
   pandas. A node that cannot fit its population in memory must fail loudly, never silently sample.

**What in-App execution costs, and the mitigation.** A Databricks job run would have given an
independently platform-written run record — useful evidence in an audit tool, because it is not
asserted by the application being audited. Without Jobs, compensate with two records the app does
not author: an **MLflow run per pipeline run** (per-node spans, parameters, outcome) and
**`system.access.audit`**, which independently logs every SQL statement the App issued. Both are
named in the methodology panel as the independent trail.

### 2.4 Human-in-the-loop gates — phase boundaries in Delta

A run is up to three executor passes. Each ends by writing state and returning; the next begins
when a human acts.

| Phase | Nodes | Ends with |
|---|---|---|
| `plan` | discover → profile → plan | `awaiting_confirmation` |
| `execute` | execute → classify → find → prioritise → act | `awaiting_signoff` |
| `export` | export | `completed` |

- Plan confirmation: **mandatory in Explorer**, optional in Playbook.
- **Findings sign-off before export: mandatory in both modes.** A human signs off on what leaves
  the system. This is the control that matters for a defensible workpaper.
- Sign-off records approver identity and timestamp in `trace_events` and on `runs.approved_by`.

Because state lives in Delta and not in a thread, an approval can happen hours later, from a
different browser, after an App restart. That property is the reason for the design and the P7
gate tests it.

### 2.5 Apps compute vs Jobs — the trade-off, for review

This section exists so the execution decision can be evaluated rather than assumed. It states both
options fairly, the reasoning for the choice made, the conditions that would reverse it, and the
platform facts still unverified.

**Decision:** pipeline execution runs in-process inside the Databricks App (§2.1–2.3), behind an
`Executor` interface that keeps a Jobs implementation available. Confirmed with a Databricks
Solutions Architect as feasible.

#### Apps compute — in-process thread pool

**Strengths**

- **One deployable.** No job definition to create, permission, version or keep in sync with the
  App's source. Fewer governed assets means fewer approvals in a corporate workspace, and approval
  lead time is a real project risk here.
- **No cold start.** A thread starts immediately. A serverless job run takes tens of seconds to
  begin, which is poor UX behind an interactive "Run audit" button and makes the Trace page open on
  a spinner rather than an event.
- **One identity.** No handoff between the App's identity and a job's own identity. This matters
  directly for the largest open governance question (§9A.1): whether Unity Catalog row filters on
  executive data are enforced per auditor.
- **Same code path in tests.** `ThreadExecutor` is what pytest uses, so the pipeline is exercised
  identically in CI and in production. With Jobs, the tested path and the deployed path differ.
- **Simpler debugging.** One log stream, one environment, one dependency set.
- **Lower cost and no idle compute.** No per-run compute spin-up.

**Weaknesses**

- **No platform-written run record.** A job run produces execution evidence the application did not
  author. This is the most audit-relevant loss; the mitigation (MLflow run + `system.access.audit`,
  §2.3) is weaker because both are still initiated by the App.
- **A restart or redeploy kills in-flight runs.** Resume-from-Delta becomes load-bearing rather
  than a nicety, which is why it is a P3 gate and not a P9 hardening item.
- **No isolation.** Executor work shares CPU and memory with the web tier. A heavy run degrades the
  UI for every user.
- **Hard memory ceiling.** App containers are small. Large populations cannot be loaded into
  pandas, forcing aggregation into SQL and a loud failure when a population will not fit.
- **No horizontal scale.** One container, concurrency capped at two. Several auditors running
  simultaneously will queue.
- **No platform retries, timeouts or alerting.** All of it must be written and maintained in code.
- **Long-running work is impossible.** A risk-sensing pass over a document corpus (§4.10) cannot
  run here at all.
- **Queueing must be built.** The `queued` status and `MAX_CONCURRENT_RUNS` exist because the
  platform provides no queue.
- **Cost attribution is coarse.** Per-run compute cost is not separable from App compute.

#### Databricks Jobs — serverless, triggered by `run_now`

**Strengths**

- **Durable, platform-written run history.** "Run 8843, 21 Sep 14:02, these parameters, this
  outcome", recorded by the platform. For an audit tool this is genuine evidence.
- **Isolation and right-sizing.** Own compute per run, sized to the work, no effect on the UI.
- **Retries, timeouts, alerts and notifications** are configuration rather than code.
- **Scales.** Many concurrent runs, each with its own compute.
- **Handles long-running work** — the only viable home for risk sensing.
- **Schedulable**, which continuous sensing requires.
- **Per-run cost attribution** through system tables.
- **Survives App redeploy.** A run in flight is unaffected by a UI deployment.

**Weaknesses**

- **Cold start** on every run, as above.
- **Two deployables.** The job's source path, dependency set and runtime must track the App's, or
  the two drift and the deployed pipeline stops matching the tested one.
- **More permissions to obtain.** The App's service principal needs `CAN_MANAGE_RUN`; the job's
  compute needs its own Unity Catalog grants. In a corporate workspace each is a request with lead
  time.
- **Identity handoff.** The job executes under its own identity, so per-user Unity Catalog
  enforcement requires deliberately carrying and re-checking user context.
- **More surface to operate** — two log streams, two environments, a job definition someone owns.

#### Why this workload favours Apps

The deciding factors are properties of *this* workload, not general preferences:

| Factor | This workload | Consequence |
|---|---|---|
| Run duration | Minutes | Isolation and durability matter less |
| Frequency | A few per week per auditor | Scale does not bind |
| Concurrency | A handful of auditors | A cap of two is tolerable |
| Interaction model | A person clicks "Run" and waits | Cold start is a visible cost |
| Data volume | ~150K rows today | Fits in memory; SQL pushdown covers growth |
| Governance context | Every new permission is a request with lead time | Fewer governed assets is real value |
| Reversibility | `Executor` interface | The decision is not load-bearing |

Jobs is the better engineering answer on durability and isolation. It is the worse answer on
latency, permission surface, identity, and test/production fidelity. For a prototype heading to a
pilot, with an interface preserving the option, Apps is the better trade — and the loss that most
deserves scrutiny is the platform-written run record, not performance.

#### What would reverse the decision

Any one of these should move execution to `JobsExecutor`:

1. **The risk-sensing module is built** (§4.10). Hours-long corpus passes cannot run in a web
   container. This is a certainty if the lifecycle roadmap proceeds, not a risk.
2. **A population stops fitting in App memory** after SQL pushdown is exhausted.
3. **Concurrent demand exceeds the cap** often enough that auditors wait.
4. **An assurance requirement demands a platform-written execution record** that MLflow and
   `system.access.audit` do not satisfy.
5. **Runs routinely exceed ~10 minutes**, making restart-kills frequent rather than rare.

Because the pipeline is a pure function of `RunState` and every node persists to Delta, that switch
is a deployment change, not a rewrite. Keep it that way: **no node may reference the executor, the
thread pool, or Dash.**

#### Platform facts not yet verified — confirm in P1

Stated here so a reviewer does not read them as established:

- App container CPU and memory limits, and whether they are configurable.
- Whether an App can scale to zero, restart on idle, or recycle containers unprompted — all of
  which would make orphaned runs common rather than rare, and would materially weaken this choice.
- Whether background threads survive for the full duration of a multi-minute run, or are bounded by
  a request or worker lifecycle.
- HTTP/gateway timeout applying to the polling callbacks.
- How App compute appears in `system.billing.usage` for per-run cost attribution.

If any of these turns out worse than assumed — particularly container recycling — revisit this
section before P3 rather than after.

---

## 3. Non-negotiables

Change none of these without stopping and proposing to the user first.

1. **No orchestration framework for the audit pipeline.** Plain Python loop (§2.2). One stated
   exception: the risk-sensing module (§4.10) is a bounded research loop and may use a tool-use
   framework, confined to its own package, with a hard ceiling on calls. Nothing in the fieldwork
   pipeline may import it.
2. **The LLM authors rules; it does not decide results at runtime.** An LLM may author a
   Skill's tests, finding rules, thresholds and prose templates — reviewed by a human and
   versioned (§4.6). At runtime, every number and every finding's existence comes from Python
   evaluating those rules against data. The runtime LLM writes prose around numbers the node
   already fixed, and synthesises themes across findings. It never invents a finding, a number
   or a severity. `execute` never calls an LLM.
3. **Two HITL gates** (§2.4). Findings sign-off is mandatory in both modes.
4. **Skills are data.** A Skill is YAML (manifest, contract, plan, thresholds, prompts) plus a
   small `custom.py` escape hatch and a `workspace.py`. Not a pile of bespoke functions.
5. **Delta is the system of record.** `RunState` is the in-flight working set; Delta is the truth.
   There is no second checkpoint store.
6. **`RunState` is JSON-serialisable — refs only.** No DataFrames, no objects. Volume paths,
   table names, row counts. Enforced by a round-trip test.
7. **Every LLM call is logged** to `llm_calls` synchronously before the call returns: prompt,
   response, endpoint, served model version, params, token counts, latency, node, run_id.
   AI Gateway inference tables provide an independent second copy.
8. **Replayability comes from the Delta response cache;** reproducibility comes from the full
   **run fingerprint**. Cache key: `(prompt_sha256, endpoint, served_model_version, params_json)`.
   Sampling parameters are per-endpoint config, tested, never assumed. The cache provides exact
   replay of a previously captured response — it does not by itself prove reproducibility of the
   audit result. A reproducible run requires matching the full fingerprint: source table version
   or uploaded-file SHA-256, Skill content hash (not just a version label), code revision,
   dependency lock hash, runtime configuration hash, endpoint, served model version, and prompt
   template version. **The run fingerprint is stored in `run_fingerprints` (P1A) and verified at
   resume.** Per-LLM-call provenance (actual served model version, token counts) is captured in
   `llm_calls` (P6) — the fingerprint answers "was the setup identical?", `llm_calls` answers
   "did each model call behave identically?".
9. **Model routing is config-driven.** Two endpoints (§6). No model names in node code.
10. **Every metric carries source provenance** — `{value, unit, source_ref}` where `source_ref`
    identifies the source table version or uploaded-file SHA-256, the source columns, and the
    aggregation grain. `source_file` alone is metadata, not lineage — it cannot answer "which
    rows produced this number?" For UC table inputs, `source_ref` includes the table version
    (`DESCRIBE HISTORY`); for uploaded files, the file hash stored at ingest.
11. **No chain-of-thought exposed or stored raw.** Trace shows execution events only — timestamps,
    stages, statuses, durations. `llm_calls` logs prompts and response content (the structured
    output). Raw reasoning traces (`reasoning_content`) are neither stored nor rendered — store
    structured rationale and citations instead (§9C).
12. **Narrative traceability.** Every LLM paragraph in the UI shows which `RunState` fields
    supplied its numbers.
13. **No fake successful integrations.** UC search fails → demo indicator. Jira not connected →
    "Preview — not submitted". Endpoint misconfigured → deterministic output only, labelled
    "LLM unavailable". Never silently fabricate.
14. **No silent defaults for missing data.** Contract violation → run fails. `_find_col` and the
    `_standardise_*` defaulting behaviour are deleted, not ported.
15. **`/workspace/tne` stays functionally identical on the same data.** LLM narration is additive.
    Note "functionally", not "bit-for-bit": v1 said bit-for-bit, which would have preserved the
    placeholder constants and silent defaults in §0.2.
16. **No hardcoded workspace, catalog, volume, endpoint, or organisation names anywhere in source.**
    Migration to another workspace is an environment change, not a code change.

---

## 4. The pipeline

### 4.1 RunState

Defined in `orchestrator/state.py`. No framework imports. JSON round-trip tested.

```python
@dataclass
class RunState:
    run_id: str
    run_kind: Literal["fieldwork", "sensing", "assessment", "planning"]  # §4.10
    engagement_id: str | None            # None for corpus-scoped runs (sensing); non-null for engagement-scoped (fieldwork, assessment, planning). Enforced by application validation at run creation, not by a DB constraint — see §4.8 and §4.10.
    skill_id: str | None                 # None for Explorer
    skill_version: str | None
    mode: Literal["playbook", "explorer"]
    phase: Literal["plan", "execute", "export"]
    next_node_index: int
    audit_period: tuple[str, str]        # ISO dates
    objective: str
    business_unit: str | None
    materiality: float | None
    options: dict

    # identity and provenance
    run_owner: str                       # user identity captured at run creation (§9A Q1)
    fingerprint_id: str                  # FK → run_fingerprints table (run-level provenance)
    state_version: int                   # optimistic concurrency — incremented on every transition, CAS enforced

    # timestamps
    created_at: str                      # ISO 8601 UTC — when start_audit_run was called
    started_at: str | None               # when status first moved to "running"
    completed_at: str | None             # when status moved to a terminal state
    last_state_change_at: str            # updated on every status or phase transition

    # node tracking
    current_node_attempt_id: str | None  # FK → node_attempts; the node currently executing or last completed

    data_assets: list[dict]              # UC refs
    uploaded_files: list[dict]           # Volume refs
    profile_result: dict | None
    plan: dict | None                    # resolved primitive instances
    plan_confirmed: bool
    plan_edits: list[dict]               # auditor diffs against the proposal

    # results — REFS AND SCALARS ONLY, never DataFrames
    test_results: list[dict]
    flagged_table: str | None            # Delta table name holding RF_* rows
    reconciliation: dict | None          # rows, sum, min/max date, variance
    exceptions: list[dict]
    findings: list[dict]
    management_actions: list[dict]
    exports: dict
    signoff: dict | None                 # approver, timestamp
    status_reason: str | None            # why the run is in its current status (error message, interruption reason, etc.)

    # LLM narration — never contains raw numbers the model invented
    profile_narrative: str | None
    plan_rationale: dict[str, str]
    classification_reasoning: dict[str, str]
    finding_narratives: dict[str, str]
    priority_rationale: dict[str, str]
    remediation_drafts: dict[str, str]
    exec_summary: str | None
    chart_captions: dict[str, str]

    events: list[dict]
    errors: list[dict]
    status: Literal["queued","running","awaiting_confirmation",
                    "awaiting_signoff","completed","failed","interrupted"]
```

#### Run fingerprints (P1A)

The `run_fingerprints` table stores the immutable provenance snapshot captured at run start.
`RunState.fingerprint_id` is a FK to this table. The fingerprint is computed once at
`start_audit_run` and never updated.

| Field | What it captures |
|---|---|
| `fingerprint_id` | PK — deterministic hash of the remaining fields |
| `source_table_versions` | `{table_fqn: version}` — resolved via `DESCRIBE HISTORY` **before** reading; the read uses `VERSION AS OF` to pin the exact snapshot. This ordering is critical: resolving the version after the read creates a TOCTOU gap where the table could change between read and provenance capture |
| `uploaded_file_hashes` | `{volume_path: sha256}` |
| `skill_content_hash` | SHA-256 over the Skill's manifest + contract + plan + thresholds + findings + prompts |
| `code_revision` | Git commit hash of the deployed application code |
| `dependency_lock_hash` | SHA-256 of `requirements.txt` or lockfile |
| `runtime_config_hash` | SHA-256 of the effective runtime configuration (excluding secrets) |
| `endpoint_config` | `{task: endpoint_name}` — which endpoints were configured |
| `prompt_template_version` | Version or hash of the prompt templates in use |

Per-LLM-call provenance (actual served model version, token counts, latency) is captured in
`llm_calls` (P6), not here. The fingerprint answers "was the setup identical?"; `llm_calls`
answers "did the model behave identically?".

#### Additions from the P1A gate review (2026-09-23)

- **`RunState.phase_epoch: int`** (after `state_version`) — the state version at which the run
  entered its current phase. Node execution keys are `run_id:phase:phase_epoch:node_name:attempt`,
  so a phase entered again (Explorer re-plan, Regenerate, re-run after rejection) executes fresh
  instead of replaying stale attempts.
- **Node output ownership.** RunState fields are partitioned into node-owned (results and
  narration) and lifecycle (identity, status, phase, timestamps, gates). A node may change only
  node-owned fields; `events`/`errors` are append-only. A node that touches a lifecycle field fails.
- **Phase-change gates in the state machine** — plan→execute requires `plan_confirmed` (or a
  Playbook `auto_confirm_plan`), execute→export requires `signoff`, resume never changes phase.
- **`run_state.status`** is written in the same single-row CAS as `state_json`; the reaper and all
  status queries read it. `runs` is a best-effort projection repaired at App start.
- **`run_fingerprints.reference_data_hashes`** — `{path: sha256}` of pinned reference files (the RBA
  exchange-rate snapshot, city→country and supplier lists).
- **Fingerprint verified at every executor pass,** not only at resume.

### 4.2 Nodes

| Node | LLM | Writes | Event |
|---|---|---|---|
| `discover` | — | resolved source refs | "Sources selected" |
| `profile` | narrator | schemas, row counts, null rates, cardinality, quality flags | "Data profiled" |
| `plan` | Explorer only | ordered primitive instances | "Plan generated" |
| *(gate)* | — | halts unless `plan_confirmed` | "Awaiting confirmation" |
| `execute` | **never** | metrics payload **and** row-level `RF_*` flags → Delta | "Tests executed" |
| `classify` | residual only | classified exceptions with confidence | "Exceptions classified" |
| `find` | narrator + bounded critic | findings with evidence refs | "Findings generated" |
| `prioritise` | 1-line rationale | risk-scored ordering | "Findings prioritised" |
| `act` | narrator | draft management actions | "Actions drafted" |
| *(gate)* | — | halts until sign-off | "Awaiting sign-off" |
| `export` | exec summary + captions | PPTX / XLSX / Jira preview | "Exports prepared" |

`execute` emitting **both** the metrics payload and the row-level `RF_*` flags is what keeps the
existing UI, charts, exception drill-down and `BREACH_FLAG_GROUPS` working while the engine
underneath is replaced. Do not skip it.

### 4.3 Primitives

The 14 T&E tests are instances of ~8 domain-agnostic primitives. Build the registry, not 14
bespoke functions — this is what makes GST cheap and Explorer possible.

| Primitive | Params | T&E | GST T4.8 |
|---|---|---|---|
| `threshold_exceedance` | column, limit, direction | T4.4, T6.1d | invoice > delegation limit |
| `duplicate_detection` | key columns, amount tolerance, date window | T5.2 | duplicate invoices |
| `split_detection` | group keys, window days, aggregate threshold | T5.1 | split POs under a limit |
| `anti_join_gap` | left source, right source, join keys | T3.1a, T3.1b | invoice with no PO |
| `list_membership` | column, allowed list, negate | T3.2a | vendor not on ABN register |
| `date_lag` | start col, end col, threshold days | T3.3a | payment before invoice date |
| `ratio_per_group` | numerator, denominator, group, limit | T3.3b | GST ≠ 10% of ex-GST |
| `attribute_missing` | column, condition | T4.1, T4.2 | missing tax invoice |

Each primitive has a JSON Schema for its params and returns `(metrics, flagged_rows)`.
Anything that genuinely does not fit (T6.1a approver review) goes in the Skill's `custom.py`.

### 4.4 Skill layout

```
skills/tne_exco/
├── manifest.yaml      # id, name, domain, version, owner, status: draft|published
├── contract.yaml      # required sources + columns + types + nullability + PII class
├── plan.yaml          # ordered [{primitive, params, control_objective, risk_rule}]
├── findings.yaml      # which findings can exist, severity rules, title/observation/
│                      #   recommendation/question templates, metrics_cited
├── thresholds.yaml    # every threshold, with provenance + effective date
├── prompts/           # overrides of platform defaults
├── custom.py          # tests no primitive expresses
└── workspace.py       # render_workspace(run_id)
```

The Skill protocol is `load()`, `validate()`, `custom_tests()`, `render_workspace()`. Building the
test plan, running tests and building findings are **generic pipeline code reading `plan.yaml`** —
not per-Skill methods.

`contract.yaml` describes the **raw** sources (`Lead Traveller Name`, `Advance Purchase Days`),
because that is what `execute` now consumes.

### 4.5 Explorer Mode — a Skill-authoring workflow

Explorer does not generate code or SQL. It **composes primitives**.

```
objective + profile + primitive schemas + 1–2 reference Skills
        │
        ▼  one Sonnet call, strict JSON schema
   PlanProposal { tests[{primitive, params, control_objective, risk_hypothesis, rationale}],
                  data_gaps[], assumptions[] }
        │
        ▼  Python validator: primitive exists? columns in profile? params satisfy schema?
   ≤1 repair round (GPT-OSS, same schema)  →  invalid tests greyed out, never a third call
        │
        ▼  structured edit UI (dropdowns of real columns — not a chat box)
   auditor confirms  →  identical pipeline to Playbook  →  optionally saved as a draft Skill
```

The planner sees **aggregates only** — schemas, counts, null rates, cardinality, inferred semantic
types, PII columns masked. Never rows. It has no tools and cannot query data.

**Explorer authors `findings.yaml` as well as `plan.yaml`.** A test that flags exceptions nobody
writes up is useless, so the `PlanProposal` schema carries a `findings[]` block in the same shape
as §4.6, validated the same way: every `metrics_cited` entry must be a metric the proposed tests
actually produce, every threshold must be numeric and land in `thresholds.yaml` with
`provenance: analyst-set`, and every `trigger`/`severity.when` must parse under the restricted
evaluator. This is the product feature that replaces "ask a coding agent to write the findings".

Promotion `draft → published` requires Surface 2 precision/recall on planted synthetic data plus
a named reviewer.

**Do not build:** a tool-using planner, a planner that emits SQL or pandas, a multi-agent swarm,
a supervisor pattern, a planner/skeptic pair, or a conversational companion agent. If a phase
seems to need one, stop and ask — the answer is usually a better prompt or a rule change.

### 4.6 Authoring time vs run time

The single most important distinction in this system. Both moments involve an LLM; only one of
them is allowed to vary per run.

| | **Authoring time** | **Run time** |
|---|---|---|
| Who | Explorer Mode planner, or a human with a coding agent | The `find` node |
| Produces | `plan.yaml`, `findings.yaml`, `thresholds.yaml` — which tests run, which findings can exist, what severity rules apply, prose templates | This run's numbers, filled narratives, cross-finding synthesis |
| Human review | Once, at Skill confirmation. Then versioned | Per run, at the sign-off gate |
| Varies per run | Never | Prose may; membership and numbers never |
| Testable by | Surface 2 (precision/recall of the rule set) | G11, judge |
| Answer to "why does this finding exist?" | "SKILL-001 v1.2 §findings.T4_1, confirmed by <name> on <date>" | — |

**`findings.yaml` shape** (port `build_all_findings` into this, do not port it into Python):

```yaml
findings:
  - id: T4_1
    test_id: T4.1
    title: Missing Receipt Documentation
    trigger: missing_receipt_count > 0
    severity:
      - when: missing_receipt_pct > 10   # threshold ref: thresholds.missing_receipt_high
        then: High
      - when: missing_receipt_pct > 5
        then: Medium
      - else: Low
    metrics_cited: [missing_receipt_count, missing_receipt_pct, missing_receipt_amount]
    observation: >
      {missing_receipt_count} claims ({missing_receipt_pct}% of total) are missing receipt
      documentation, representing {missing_receipt_amount} in unsupported spend.
    recommendation: >
      Enforce mandatory receipt attachment before expense report submission.
    management_questions:
      - What is the current policy for handling claims without receipts?
```

`trigger` and `severity.when` are evaluated by a small, explicitly-scoped expression evaluator
over the metrics dict — **not** `eval()`. Restrict to comparison and boolean operators over
known metric names; reject anything else at Skill load time.

**Run-time synthesis (the one place the LLM adds judgement to findings).** After the rules
produce the finding set, `find` makes one call that may:
- group findings into themes and propose a root-cause hypothesis per theme
- note cross-test patterns no single rule can see, as **review observations** attached to the
  theme (not findings — they carry no test_id, severity or metrics_cited)
- propose a severity *alongside* the computed one, with a reason

It may not add a finding outside the rule set, remove one, change a computed severity, or emit a
number. Review observations are informational annotations, not findings — they do not appear in
exports or counts. Themes are stored on `RunState.findings[].theme_id` plus a `themes` list,
and the auditor confirms them at sign-off.

**Regenerate** in the UI re-runs narration and synthesis. It never re-runs rule selection.

### 4.7 Exports — the PPTX pack

The PPTX pack is the workpaper an executive actually reads. `src/pptx_export.py` (596 lines) is a
reasonable foundation — it builds **native PowerPoint charts** rather than pasted images, so they
stay editable and on-brand, and it uses the Optus template with consistent design tokens. Keep
both properties. What is wrong is structure and robustness, not the approach.

**Defects to fix:**

1. **It is a data dump, not a deck.** `generate_pptx` emits one slide per finding, all of them:
   cover + exec summary + 3 charts + coverage + N high findings + divider + M medium/low findings
   + methodology + end. Fourteen findings produces ~23 slides. The UI already knows better —
   `_render_filtered_findings` (`app.py:1552`) shows the top 3 and collapses the rest. The deck
   does not.
2. **Fixed-height text boxes with variable-length content.** `_add_text(slide, left, top, width,
   height, …)` (`pptx_export.py:92`) sets an explicit height and never enables autofit. A long
   observation silently overflows the shape or runs off the slide. **This is the single most
   visible defect and the cheapest to fix.**
3. **No written narrative.** Every word is mechanical — the "Executive Summary" slide
   (`:176`) is computed counts. This is the one slide read verbatim by an executive, and after P6
   it must carry the generated `exec_summary`.
4. **The headline exposure figure is the double-counted one.** `_build_risk_chart` (`:433`) sums
   `financial_exposure` across findings — the same overlapping-population error as `app.py:919`.
   Fixed once in P3; the exporter must read the corrected value, not recompute it.
5. **Zero findings produces a broken deck** — a "Detailed Findings" divider followed by nothing.
   A clean audit is a legitimate and important outcome; say so explicitly on a slide.
6. **Private API use.** `generate_pptx` (`:545`) manipulates `prs.slides._sldIdLst` and drops
   relationships by hand with a fallback attribute loop. It will break on a `python-pptx` version
   bump. Ship a template that contains no content slides instead.

**Target structure** — roughly ten slides, mirroring how the UI already thinks:

```
Cover                     run id, Skill + version, audit period, data mode, date
Executive summary         LLM-written (P6), 3 short paragraphs, one number callout
What we found             the themes from §4.6 synthesis — not a finding list
Top matters (3–5 slides)  one per priority finding: observation, evidence, recommendation,
                          management question, source files
Risk and exposure         one chart, honest arithmetic, methodology note on what exposure means
── Appendix ──
All findings              compact table, one row per finding
Test coverage             the existing coverage slide
Methodology & limitations the existing slide, plus threshold provenance (§0.4)
```

**Rules for the rebuild:**

- **Load the `dataviz` skill before writing or changing any chart code.** Do not choose chart
  types, colours or layouts without it.
- Keep native `add_chart` output. Never paste a rendered image — it loses editability and scales badly.
- Every text frame: `word_wrap = True` and `MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE`, or measure and
  truncate with an explicit ellipsis. Never let content overflow silently.
- Slide count must be bounded and deterministic for a given finding count. State the rule.
- Every number on every slide comes from `RunState` — never recomputed in the exporter. This is
  what **G13** asserts.
- Stamp `run_id` and generation timestamp in the footer of every slide. An export must be
  traceable to the run that produced it (§9A.2).
- Where a severity rests on an analyst-set threshold (§0.4), the finding slide says so.
- The exporter takes a completed run as input. It does not read module globals.

**Phasing.** The narrative does not exist until P6 and the numbers are not trustworthy until P3,
so a full rebuild before then is rework. Split it:

- **P4** — fix autofit/overflow (defect 2), the zero-findings case (5), and the private-API use
  (6). Wire G13. Small, and immediately visible.
- **P6** — the structural rebuild: new slide order, themes slide, LLM exec summary, bounded slide
  count, footer stamping, threshold-provenance labels.

### 4.8 Schema headroom for the audit-suite model

The target product is a commercial-grade audit suite, not a single-run analytics app. Three
concepts from that model change the **shape of the tables**, so they belong in the P1B schema even
though the features themselves land much later.

**P1B creates the shape. P1B does not build the features.** Adding a column to an empty table is
free. Adding it after real runs exist means backfilling rows that have no sensible value,
rewriting every query, and re-deriving every permission check — at precisely the moment there is
real data and real users to disrupt.

**Note:** P1B adds only tables whose concepts and identifiers are agreed. The `risks`, `controls`,
`risk_assessments`, `review_notes`, and `issues` tables are created only once their lifecycle
rules are confirmed — empty columns are cheap, but wrong concepts are not (§8, P1B DoD).

#### 1. Engagement scoping

Today the system thinks in *runs*. An audit suite thinks in *engagements*: "FY26 T&E audit of
Consumer" owns many runs, across many Skills, over months.

P1B delivers:
- An `engagements` table: `engagement_id`, `name`, `entity`, `period_start`, `period_end`,
  `owner`, `status`, `created_at`.
- A **nullable** `engagement_id` column on `runs`, `findings`, `management_actions`, `trace_events`
  and `uploaded_files`. Nullable in the schema because corpus-scoped runs (sensing) have no
  engagement. **Application validation at run creation enforces non-null for engagement-scoped
  run kinds** (fieldwork, assessment, planning).
- Exactly one seeded default engagement, so nothing in the UI has to change yet.
- `RunState.engagement_id`.

Later phases add engagement creation, listing, scoping of the runs/actions pages, and per-engagement
access control. None of that requires a migration.

#### 2. Review hierarchy

Real audit review is preparer → reviewer → partner, with review notes raised against a finding
("justify excluding FCM from the split-claim test") that must be cleared before the file closes.
The current design has a single sign-off.

P1B delivers (once lifecycle rules are agreed — see §8 P1B DoD):
- A `review_notes` table: `note_id`, `engagement_id`, `run_id`, `finding_id` (nullable — a note
  can sit on the run), `raised_by`, `raised_at`, `body`, `state` (`open`/`cleared`),
  `cleared_by`, `cleared_at`, `response`.
- `findings.review_state` — `draft` / `prepared` / `reviewed` / `approved`.
- `runs.prepared_by`, `runs.reviewed_by`, `runs.approved_by` (nullable).

P7's sign-off gate writes `approved_by` and moves findings to `approved`. The full note lifecycle
and role model come later; the table exists and stays empty until then.

#### 3. Rollforward

"Same finding as last year; management agreed to fix it; they have not." This requires a finding
identity that survives across periods. Today a finding is `run_id + finding_id`, which is unique
to one run and meaningless across runs.

P1B delivers:
- `findings.rule_id` — the stable identity of the *rule that produced the finding*, namespaced by
  Skill: `SKILL-001.T4_1`. It comes free from `findings.yaml` (§4.6) and never changes across runs.
- `findings.prior_finding_id` (nullable) — the same rule's finding in the prior period.
- `findings.recurrence_count` (default 0).

Matching logic, the "recurring finding" badge and prior-period comparison come later. Without
`rule_id` recorded from the first run, that history can never be reconstructed — which is the
whole point of doing it now.

#### Not in scope

Two further suite capabilities are **features, not schema**, and are explicitly deferred with no
P1B obligation:

- **Statistical sampling** (monetary-unit and attribute sampling with defensible sample-size
  calculation). The platform currently tests 100% of the population. This becomes a new category
  of primitive (§4.3) when it is wanted.
- **Workpaper indexing and cross-referencing** (A-1, B-2.3 style indices that findings cite).
  Additive to `findings` when wanted.

### 4.9 The lifecycle spine — Risk → Control → Test → Finding → Issue → Action

The long-term target is a platform organised around the **audit lifecycle**, not around a single
analytic run (see `docs/PRODUCT_POSITIONING.md`). The current design covers the middle of that
chain — `Test → Finding → Action`. The front (Risk, Control) and one middle link (Issue) are
missing.

**P1B creates every link whose concepts are agreed. Only the middle three are exercised.** Same
rule as §4.8: an empty column is free, a retrofit after real engagements exist is not. Tables
whose lifecycle rules are not yet confirmed are deferred rather than guessed (§8, P1B DoD).

#### Risk and Control as first-class objects

`test_catalogue.py` already carries a `control_objective` string per test. That string *is* a
control; it is simply not modelled as one. Promoting it now costs almost nothing.

```
risks:     risk_id | engagement_id | title | description | category | owner
                  | status (proposed|accepted|rejected|superseded)
                  | source (manual|glean|regulation|erm_import) | source_ref
                  | as_of_date | confidence | prior_risk_id | created_at

controls:  control_id | risk_id | engagement_id | title | description
                  | type (preventive|detective) | frequency | owner
                  | design_conclusion | operating_conclusion | created_at
```

Every test in `plan.yaml` gains three fields:

```yaml
- primitive: duplicate_detection
  params: {...}
  control_id: CTL-TNE-07
  risk_id:    RSK-TNE-03
  assertion:  operating          # design | operating
```

This buys a **risk-and-control matrix (RCM)** view for free — the central artifact of audit
planning — and makes every finding traceable to a risk rather than only to a test.

**`assertion` is not optional.** Design effectiveness (*is this control capable of preventing the
risk*) and operating effectiveness (*did it work on these transactions*) are different audit
conclusions. Every test in the current catalogue is operating; the platform does not yet do design
assessment. If tests are unlabelled, that distinction cannot be reconstructed later.

#### Issue is not the same object as Finding

`findings` currently does double duty. In an audit file these are distinct:

| | **Finding** | **Issue** |
|---|---|---|
| Means | A test produced exceptions | A reportable control deficiency |
| Scope | One run | The engagement — may draw on several findings, across several runs |
| Nature | Factual | Judgement: an auditor decided to raise it |
| Lifecycle | Ends with the run | Rated, owned, due-dated, tracked to closure, followed up next period |
| Audience | Working paper | Audit committee |

```
issues:  issue_id | engagement_id | rule_id | title | description | rating
               | status (draft|open|agreed|remediated|closed|superseded)
               | raised_by | raised_at | owner | due_date
               | remediation_plan | management_response
               | prior_issue_id | finding_ids (array<string>) | run_ids (array<string>)
```

In P1B the app auto-creates one issue per finding, so behaviour is unchanged. The structure is what
matters: the moment you want "three findings, one issue" or "still open from last period", it is
there. `management_actions` becomes a child of an issue rather than of a finding.

#### Engagement lifecycle stage

One column: `engagements.stage` ∈ `planning | fieldwork | reporting | closed`. Every later module
keys off it.

#### Risk sensing — headroom only, no implementation

The intended long-term source of risks is an internal knowledge tool (Glean) reached over MCP,
plus external regulation lookups, producing a ranked register scored on financial, reputational
and other impact dimensions. That is **months away on governance grounds and must not be built
now.** What P1B provides is the shape it will write into.

**1. Provenance is already in the `risks` table above.** A risk without a citation is unusable in
audit — an auditor must be able to click through to the source paragraph. `as_of_date` matters
more than it looks: regulations change, and a risk scored against last year's version goes stale
silently without it.

**2. Scoring is multi-dimensional and append-only.** Do not collapse it to one number on `risks`:

```
risk_assessments: assessment_id | risk_id | dimension | score | rationale
               | method (model|human) | assessed_by | assessed_at | source_ref
```

`dimension` ∈ `financial | reputational | regulatory | operational`. Append-only gives rating
history for free — "medium last quarter, high now" — which is the entire point of *continuous*
sensing, and cannot be reconstructed from a single current-value column.

**3. A seventh adapter Protocol, defined in P1A and implemented never:**

```python
class KnowledgeSourceAdapter(Protocol):
    def search(self, query: str, *, as_of: date | None) -> list[Document]: ...
    def fetch(self, doc_id: str) -> Document: ...
```

Glean-over-MCP becomes one implementation if and when it is approved. A file drop into a Volume is
another, and is how this would be prototyped before then. Nothing in the pipeline knows which.

**4. Governance — an LLM-authored risk register is a heavier question than LLM-authored findings.**
Risks drive the audit plan; the plan drives what is tested; what is tested drives the assurance the
board receives. An error of *omission* is invisible — nobody discovers the risk that was never on
the list. Therefore:

- A proposed risk is a **candidate** until an auditor accepts it — hence `risks.status`.
- No citation, no acceptance.
- Coverage is reported explicitly ("sensed from 412 documents, 38 risks proposed, 22 accepted")
  together with a standing note that **absence of a risk is not assurance**.

#### Not in scope, deliberately

- Glean-specific code, MCP dependencies (see §7), or regulation fetching.
- Risk scoring algorithms or weightings — the enterprise risk function's existing methodology will
  dictate these, and a second conflicting methodology is worse than none.
- `audit_plans` / annual planning. `engagement_id` is sufficient headroom.
- Any UI for risks, controls or issues. Tables only in P1; UI arrives with the module.

### 4.10 Three workload classes — one guarantee does not fit all

The lifecycle target (§4.9, `docs/PRODUCT_POSITIONING.md`) spans five modules, and they are not
the same shape of work. The brief so far assumes one: a deterministic pipeline over tabular data.
That holds for fieldwork. It cannot hold for risk sensing — you cannot make *"read the policy
corpus and tell me what is emerging"* reproducible, and claiming otherwise would either block the
feature or ship a false guarantee.

So the architecture has three classes, each with **its own guarantee**:

| Class | Modules | Shape | LLM's job | Guarantee |
|---|---|---|---|---|
| **Deterministic pipeline** | Fieldwork, Reporting & Issues | Fixed node sequence over tabular data | Narration + bounded synthesis (§4.6) | **Reproducible** — same data, same findings, same numbers |
| **Structured generation** | Audit Planning, Explorer | One call → validate → at most one repair | Authors artifacts against a strict schema (§4.5) | **Reproducible via cache, human-confirmed** |
| **Bounded research** | Risk Sensing, Risk Assessment | Iterative search → read → synthesise, with tools | Genuinely agentic | **Not reproducible — auditable instead** |

Only the first two exist today. The third is unbuilt and its module is months away; what follows is
the contract it must satisfy when it arrives, so nobody has to break a non-negotiable to build it.

**"Auditable instead of reproducible"** means, concretely: every query logged, every document read
logged with its version, every proposed risk carrying a citation to a real passage, coverage
reported explicitly, a hard call ceiling, and nothing accepted without a human (§4.9
`risks.status`). That is a different guarantee from determinism, equally defensible, and it must be
stated as such rather than left implicit.

**Consequences for the design, when that module is built:**

1. **`RunState.run_kind`** — `fieldwork | sensing | assessment | planning`, with
   `NODES_FOR[run_kind][phase]`. The loop in §2.2 stays generic. **Added in P1A**, one field, because
   retrofitting a second pipeline shape into a single hardcoded node list is exactly the kind of
   change this brief exists to avoid.
2. **`runs.engagement_id` stays nullable in the schema** (as §4.8 now states). Sensing is
   corpus-scoped and continuous, not engagement-scoped — it runs on a schedule and feeds many
   engagements. Application validation enforces non-null for fieldwork/assessment/planning.
   **Decided in P1A/P1B.**
3. **A bounded agentic loop is permitted, in that package only** (NN1's stated exception). Hard
   ceiling on tool calls, every call logged, confined so nothing in the fieldwork pipeline can
   import it.
4. **The response cache does not transfer.** `(prompt_hash, endpoint, version, params)` never hits
   in a research loop, so cost is unbounded. Sensing needs **document-level** caching — the
   extraction from document X at version Y — which is a different table and a different key.
5. **The evaluation gates do not transfer.** G11 asserts every number maps to a cited metric; risks
   are not numbers. Surface 2 asserts precision/recall against planted exceptions; there is no
   ground truth for "emerging risks". Sensing needs its own gate, RAG-faithfulness shaped:
   - **G17 citation validity** — every proposed risk cites a document and passage, the passage
     exists, and it actually supports the claim. Sampled and judged.
   - **Coverage reporting** — "sensed 412 documents, 38 risks proposed, 22 accepted", always
     accompanied by: **absence of a risk is not assurance.**
6. **Model tiering changes.** §6's routing rule is "who reads the output verbatim". Sensing breaks
   it: nobody reads the intermediate steps, but it is long-horizon reasoning over large context
   with the highest downstream stakes in the product — risks drive the plan, the plan drives what
   is tested, that drives the assurance the board receives. This is the first genuine case for a
   premium endpoint at high effort.
7. **It does not run in the App.** A corpus pass can run for hours; a web container cannot hold it
   and a redeploy would kill it. This is the workload `JobsExecutor` (§2.3) is reserved for.
8. **Cost is a different order.** Fieldwork is ~45 short calls, tens of cents per run. A sensing
   pass is 10–100× that. Budget it separately, cap it explicitly, and schedule it rather than
   offering it on demand.

**What does not change:** deterministic numbers in fieldwork, Skills as data, primitives,
`findings.yaml`, the HITL gates, every-call logging, the adapter Protocols, and the two fieldwork
endpoints. Sensing adds a premium tier and a gate; it does not loosen anything.

---

## 5. Evaluation

Tiered. Build Tier A now; do not attempt all sixteen gates at once.

### Tier A — in CI from the phase that introduces them

| Gate | Asserts | Phase |
|---|---|---|
| **G6 Reconciliation** | tested-population rows, Σamount, min/max date == source totals; variance 0; persisted per run | P3 |
| **G7 Contract conformance** | contract violation → `status=failed`. No fuzzy column matching, no defaults | P3 |
| **G8 Threshold provenance** | catalogue thresholds == `thresholds.yaml` == code; every threshold has either a policy reference or `provenance: analyst-set`; every analyst-set threshold that drives a severity is labelled as such in the UI | P2 |
| **G10 Negative control** | clean dataset → zero findings, and no risk language in LLM output | P3 |
| **G11 Cited-metric faithfulness** | every number in a finding's prose maps to a metric in *that finding's* `metrics_cited`, unit-aware; every cited metric appears; quantifiers flagged | P6 |
| **G13 Export fidelity** | every number in PPTX/XLSX == `RunState` value, same rounding | P4 |
| **Surface 2** | per test: precision ≥ 0.98, recall ≥ 0.95 against planted exceptions | P2 |

**G11 replaces v1's "Surface 3".** v1 asserted `narrative_numbers ⊆ all_payload_numbers` and
claimed it catches ~90%. It does not. With several hundred numbers in the payload, a subset check
on small integers is nearly always satisfied — `months_covered: 16` will "support" "16 duplicate
claims". `guardrail.py`'s own docstring admits it: *"checks that a quantity is supported, not that
it is used with the right sense."* Scope to the finding's own cited metrics, match units, and
require coverage in both directions.

### Tier B — before any migration to a corporate workspace

- **Surface 1 parity against a hand-verified oracle** (~100 rows, metrics worked independently by a
  human). **Not** parity against `app.py`'s flag-counting (§0.2) — that measures the workaround.
  Surface 1 is the **correctness gate** — it proves the engine produces the right answers.
  There is also a second, narrower comparison worth having during the P2 port: a **historical
  regression comparison** against `computation.py` on the same inputs. This is not a correctness
  test — `computation.py` has documented semantic defects (§0.2) — it is a regression test that
  ensures every difference between the new engine and the old one is **explained** by a known
  defect or a deliberate correction. The gate is "no unexplained divergence", not "matches
  `computation.py`". Keep both, and do not confuse them: Surface 1 (oracle) proves correctness;
  the `computation.py` comparison proves the port was intentional.
- **G9 cross-process determinism** — same config + same data snapshot, two separate processes,
  byte-identical non-narrative output. Catches `abs(hash(x))` (`app.py:269`, PYTHONHASHSEED),
  dict ordering, float summation order.
- **G14 Edit audit trail** — every narrative regenerate/edit versioned: who, when, before/after diff.
- **G15 PII egress** — columns marked `pii: true` never enter a prompt unless the Skill whitelists
  them; whitelist recorded per `llm_calls` row.

### Tier C — steady state

- **Surface 4 narrative quality**, dual judge: GPT-OSS scores grounding and citation
  (cross-family, avoids self-preference), Sonnet scores clarity and tone. Disagreements go to the
  weekly human sample. Golden set ≈50 examples per Skill as a versioned Delta table, including
  zero-finding and near-threshold cases.
- **G16 Judge reliability** — Cohen's κ ≥ 0.6 against the human sample before judge scores drive
  any decision.
- **G12 Semantic drift** — no unhedged causal/intent language ("intentional structuring",
  "to circumvent"); no "Policy requires…" without citing the catalogue's `control_objective`.

### Deleted from v1

- **The model-routing decision loop (v1 Phase 7c).** It optimised ~40 cents per run using a 1–5
  rubric on n≈50 that cannot resolve a 5% difference. Replaced by one rule: *promote a node to a
  stronger endpoint when the judge shows a gap; never downgrade to save cents.*
- **"Surface 5 end-to-end"** as a separate surface — it is Surface 1 + G9 run end to end.
- **"Within tolerance"** on deterministic outputs. Deterministic means exact.

---

## 6. Model serving

Two Databricks Foundation Model endpoints. Names from config, never literals in nodes.

| Task | Endpoint | Settings | Why |
|---|---|---|---|
| Explorer `plan` | Sonnet | structured JSON schema | The one genuine reasoning task; 1 call/run |
| Finding narratives | Sonnet | — | CAO reads these |
| Exec summary | Sonnet | — | Executives read verbatim |
| Profile / priority / remediation | Sonnet | — | Auditor-edited prose; consistent voice |
| `classify` T4.3 residual | **GPT-OSS via `ai_query()`** | low reasoning effort, structured JSON + confidence | The only node scaling with rows. Batch in SQL; rows stay in Delta |
| Chart captions | GPT-OSS | low effort | One templated sentence |
| Plan repair round | GPT-OSS | medium effort | Mechanical schema fixing |
| Judge — grounding | GPT-OSS | high effort | Cross-family, avoids self-preference bias |
| Judge — clarity/tone | Sonnet | — | Needs the stronger model |

**Fallback:** if Sonnet is unavailable, degrade `profile`/`prioritise`/`act` to GPT-OSS; for
`find`, exec summary and `plan`, show **"LLM unavailable — deterministic output only"**.
Never silently degrade what an executive reads.

`NODE_MODELS`:
```
plan_explorer, find, export_summary, profile, prioritise, act, judge_quality → ${MODEL_SONNET}
classify, export_caption, plan_repair, judge_grounding                        → ${MODEL_GPT_OSS}
```

Opus may be added later for Explorer `plan` and exec summary — that is a config change only.

**Client:** `WorkspaceClient().serving_endpoints.get_open_ai_client()`. Do **not** use
`mlflow.deployments` (the pattern in the orphaned `narrative.py`), and do not hand-build an
`openai` client with a token.

**Per-endpoint parameter support must be tested and recorded in this file, not assumed** —
`response_format`, `temperature`, `max_tokens`, reasoning-effort controls. Sampling parameters
are rejected by some current model families. If structured output does not pass through,
validate the schema client-side with one retry — never strip markdown fences the way
`narrative.py:121–127` does.

**Recorded parameter matrix — development workspace, 2026-09-23** (full detail in
`docs/specs/P6_P8_explorer_llm_design.md`; client `get_open_ai_client()`, databricks-sdk 0.140.0):

- `MODEL_SONNET` (`databricks-claude-sonnet-5`): **untestable** — every call, including the bare
  baseline, returns `403 PERMISSION_DENIED: The endpoint is temporarily disabled due to a
  Databricks-set rate limit of 0.` The same happens on every proprietary-model endpoint in the
  workspace; the open-weights endpoints answer.
- `MODEL_GPT_OSS` (`databricks-gpt-oss-120b`): passes `max_tokens`, `temperature`, `top_p`, `n`,
  `tools`/`tool_choice`, `reasoning_effort` (low/medium/high, effective), and `response_format`
  json_schema `strict: true` (enforced; `anyOf`/`oneOf`/`$defs`/`$ref`, enums, bounds, `const` all
  accepted). Rejects `pattern` in schemas, `seed`, `stop`, `max_completion_tokens` and `thinking`.
  `json_object` needs the word "json" in the prompt. A strict-mode bare `{"type":"object"}` property
  comes back empty. Truncation returns `finish_reason: "length"` with no text part. `content` is a
  list of a reasoning part (strip it before storing, NN11) and a text part. The response `model`
  (`gpt-oss-120b-080525`) is the served model version; `usage` is present, and there is no
  `system_fingerprint`.
- AI Gateway: usage tracking only on both endpoints; inference tables are **not** enabled.
- **Development-workspace override (user decision, 2026-09-24):** while the Claude endpoints are
  disabled here, the Sonnet role (`MODEL_SONNET`) points at `databricks-gpt-oss-120b`. This is a
  config change only, with parameters taken from the GPT-OSS matrix above. The UI and `llm_calls`
  show the actual served model. At the Optus port `MODEL_SONNET` goes back to a Claude endpoint, and
  its parameter matrix must be re-run there.
- **Explorer decisions (user, 2026-09-24; see `docs/specs/P6_P8_explorer_llm_design.md` D2–D8):**
  - (D2) Hidden ids on the three Explorer buttons, plus one hidden `dcc.Store` and one
    `dcc.Interval` on the landing page, are allowed. They are invisible, and the parity test
    allow-lists them.
  - (D3) The proposal is reviewed as rows in the existing "Proposed workflow" panel, and "Start
    audit analysis" confirms it in Explorer mode.
  - (D4) One include/exclude checklist per proposed test, using an existing control, is the only
    edit UI for now. Full column and threshold editing comes later.
  - (D5) Explorer results appear on `/runs`, `/trace`, `/actions` and in the XLSX (`/workspace/tne`
    stays SKILL-001's).
  - (D6) The planner may see category value sets with counts, only for non-PII columns with at most
    30 distinct values, with any value seen in fewer than 5 rows withheld. It never sees rows.
  - (D7) Keep `get_open_ai_client()` as mandated.
  - (D8) Enable AI Gateway inference tables on both endpoints with restricted access. This is an
    accepted, recorded exception to NN11: the platform copy may contain GPT-OSS reasoning text.
    `llm_calls` still never stores it, and the UI never shows it.

**Enable AI Gateway with inference tables on both endpoints.** Inference tables are a
platform-written copy of every request/response — the audit trail a reviewer trusts because the
application did not write it. Keep the synchronous `llm_calls` write as well.

**Cost:** ~45 short narration calls per run plus one batch `classify`. Narration is tens of cents
per run at any tier. `classify` is the only place a wrong choice is expensive — thousands of rows
× per-row calls. Measure `system.billing.usage` after ten real runs before tuning anything.

---

## 7. Configuration and portability

Every workspace-specific value is an environment variable read through `orchestrator/config.py`.
`.env` is gitignored; `.env.example` is committed and complete.

```
DATABRICKS_HOST  DATABRICKS_TOKEN
DBX_CATALOG  DBX_SCHEMA  DBX_VOLUME  DBX_WAREHOUSE_HTTP_PATH  DBX_APP_NAME
MODEL_ENDPOINT_HOST  MODEL_SONNET  MODEL_GPT_OSS
EXECUTOR=thread          # 'jobs' reserved for §4.10 sensing; DBX_JOB_ID only then
MAX_CONCURRENT_RUNS=2
DEMO_MODE=true|false
```

**Seven adapter Protocols** in `orchestrator/adapters/`: `DataSourceAdapter`, `ModelClient`,
`PersistenceAdapter`, `PromptRepository`, `TracingAdapter`, `ExportStorageAdapter`, and
`KnowledgeSourceAdapter` (§4.9 — declared in P1, no implementation).
`DataSourceAdapter` must expose "give me the tested population" without the caller knowing whether
pandas or Spark produced it — if a dataset later exceeds container memory, only that adapter changes.

**Data access:** attach a serverless SQL warehouse as an App resource (this also grants the App's
service principal access); read/write Delta through `databricks-sql-connector`; Volumes through the
Files API.

**Environment restrictions:** MCP is not permitted in the target corporate environment today — do
not add MCP dependencies, and do not design around them. External knowledge sources (§4.9) sit
behind `KnowledgeSourceAdapter`; if an MCP-reached source such as Glean is approved later it
becomes one implementation of that Protocol and nothing else in the codebase changes. Databricks Apps cannot compile C extensions at deploy time; if a dependency fails
to install, vendor a `manylinux2014_x86_64` wheel and reference it explicitly.

**Glean (user decision, 2026-09-23).** Glean is the organisation's knowledge platform: the source of
company context on processes, policies and guidelines. The MCP connection to Glean is planned but
still being organised, so plan for it now without depending on it:
- `GleanKnowledgeSource` implements `KnowledgeSourceAdapter` (`search`, `fetch`). Its transport is
  pluggable — an MCP transport once approved, and Glean's REST API if that is permitted sooner. MCP
  client dependencies live only inside that adapter's package, are added only when approved, and
  nothing else imports them. The UC Volume document drop remains the prototype implementation, and a
  recorded-fixture fake is used in CI. Nothing here can reach Glean.
- **Identity:** Glean enforces per-user document permissions, so calls must run as the requesting
  auditor (on-behalf-of-user), never as a broad service identity that bypasses them (§9A.1).
- **Provenance:** every retrieved passage is stored with document id, URL, version or last-modified
  time and retrieval time. Citations are mandatory and G17-checked. Extractions are cached per
  document version (§4.10 item 4).
- **Consumers:** Risk Sensing and Risk Assessment (cited risks and impact), Audit Planning (process
  and policy context for scope memos, RCMs and test steps), Explorer (policy context for the
  objective), and threshold provenance — Glean can *propose* the policy reference for an analyst-set
  threshold, but it becomes policy only when an auditor confirms it. Fieldwork `execute` never calls
  Glean (NN2).
- **Egress:** policy text enters prompts under the same aggregates-and-citations rules. Anything
  classified as personal is excluded unless a Skill whitelists it (G15).

---

## 8. Phases

Each phase ends with **stop and report**. Do not auto-start the next.

| Phase | Deliverable | Definition of done |
|---|---|---|
| **P0** | Foundation reset | Done — this file, `.claude/` config, `.env.example`, `.gitattributes`, dead code removed |
| **P1A** | **Immutable run ledger** — `RunState`, explicit state machine, persistence contract | JSON round-trip test green; `runs`, `run_state`, `run_fingerprints`, `node_attempts`, `trace_events` tables created; **run fingerprint** stored per run in `run_fingerprints` (source version/hash, Skill content hash, code revision, dependency lock hash, runtime config hash, endpoint config, prompt template version); **optimistic concurrency**: `state_version` on every transition with CAS enforcement (zero-rows-affected = rejected), deterministic node execution keys, idempotent MERGE into `node_attempts`, failure-injection test proving CAS + idempotent MERGE work together; `LocalPersistence` + `DeltaPersistence` both pass the same contract test; reaper marks orphaned runs `interrupted` (never deletes); state-machine transitions tested (including invalid transitions rejected); one trivial end-to-end run completes |
| **P1B** | **Minimum product schema** — engagement scoping + all suite table DDL | DDL created for **all** suite tables: `engagements`, `findings`, `management_actions`, `skill_versions`, `risks`, `controls`, `risk_assessments`, `review_notes`, `issues`; `engagement_id` nullable in schema, application validation enforces non-null for fieldwork/assessment/planning; one default engagement seeded; `risks` and `controls` tables exist with agreed identifiers — P2 seeds T&E risks/controls from `test_catalogue.control_objective` and references them via `risk_id`/`control_id` in `plan.yaml`; tables that P1B cannot yet populate are empty but schema-tested |
| **P2** | Primitives + T&E Skill | Surface 2 ≥0.98/≥0.95 per test on planted data; G8 green; **zero hardcoded result values** — every one becomes a real computation or an explicit `not_testable` with a reason (grep test); contract describes raw sources; **per-test specification written before porting** (population, grain, join key, exception identity, scoring unit, threshold, exclusions, expected evidence); where `computation.py` and the spec disagree, the spec wins; every test in `plan.yaml` carries `control_id`, `risk_id` and `assertion`, and the T&E controls and risks are seeded from `test_catalogue.control_objective` (§4.9) |
| **P3** | Pipeline loop + nodes + ThreadExecutor | G6, G7, G9, G10 green; full run on fixtures → findings in Delta; `/trace` shows real events; MLflow per-node spans; exposure double-count fixed; **a run survives an App restart — reaper marks it `interrupted` and Resume completes it**; concurrency cap enforced with queued runs in Delta |
| **P4** | App rewired to run-scoped data | Surface 1 vs hand-verified oracle; G13 green; no module-level globals; `/workspace/tne` functionally identical; all routes unchanged; PPTX overflow, zero-findings and private-API defects fixed (§4.7) |
| **P5** | Unity Catalog + Volume upload + deploy | Governed tables discoverable and readable via the SQL warehouse; no-access catalogs surface as `Restricted`; a real file uploads to the Volume and profiles through `Uploaded → Profiling → Ready`; memory ceiling respected (aggregation pushed to SQL, loud failure never silent sampling); app deployed and deployment ID reported |
| **P6** | LLM layer + export rebuild | G11 100% on a 30-case golden set; G14, G15 green; degraded mode works; per-endpoint parameter matrix recorded in §6; `classify` via `ai_query`; PPTX restructured per §4.7 with bounded slide count, written exec summary, themes slide and run-id footer |
| **P7** | HITL gates | Paused run resumes across browser refresh **and** App restart; export blocked until sign-off; both events in `trace_events` |
| **P8** | GST Skill + Explorer | GST end-to-end on synthetic data via primitives (no bespoke test functions); Explorer proposes → validates → confirms → runs → saves draft Skill; planner cannot emit code (schema test) |
| **P9** | Eval harness + hardening | Tier A in CI; nightly green 3 consecutive nights; judge κ recorded; migration checklist written |

### Table-to-phase ownership

DDL is created by the phase that owns the table's schema. Data is first written by the phase shown.
Later phases may add columns (via migration) but the owning phase defines the initial schema.

| Table | DDL (schema owner) | First populated | Notes |
|---|---|---|---|
| `runs` | P1A | P1A | Run metadata — one row per run |
| `run_state` | P1A | P1A | Serialised `RunState` JSON |
| `run_fingerprints` | P1A | P1A | Immutable provenance snapshot per run |
| `node_attempts` | P1A | P1A | Per-node execution tracking with outcomes |
| `trace_events` | P1A | P1A | Execution timeline events |
| `engagements` | P1B | P1B | Engagement scoping; one default seeded |
| `findings` | P1B | P2 | Empty until primitives produce findings |
| `management_actions` | P1B | P3 | Empty until pipeline `act` node runs |
| `skill_versions` | P1B | P2 | Immutable Skill content snapshots |
| `risks` | P1B | P2 | T&E risks seeded from `test_catalogue`; empty schema in P1B |
| `controls` | P1B | P2 | T&E controls seeded from `test_catalogue`; empty schema in P1B |
| `risk_assessments` | P1B | P4 | Empty until risk-sensing or manual entry |
| `review_notes` | P1B | P7 | Empty until HITL gates write sign-off notes |
| `issues` | P1B | P3 | Empty until pipeline maps findings to issues |
| `uploaded_files` | P5 | P5 | Upload metadata: volume path, file hash, profile status |
| `llm_calls` | P6 | P6 | Per-LLM-call provenance including served model version |
| `llm_cache` | P6 | P6 | Response cache keyed on `(prompt_sha256, endpoint, served_model_version, params_json)` |
| `narrative_edits` | P7 | P7 | Edit audit trail for human narrative changes |
| `evaluation_runs` | P9 | P9 | Eval harness run records, judge scores, golden-set results |

Jira submission is **not** built. `create_jira_preview` returns a stub labelled
"Preview — not submitted".

---

## 9. Synthetic data

Three distinct datasets. Do not conflate them.

| Dataset | Purpose | Built by | Needed at |
|---|---|---|---|
| Planted-exception fixtures | Surface 2 precision/recall. 200–1,000 rows, plants declared in a sidecar file | **You**, generated from `contract.yaml` | P2 |
| Hand-verified oracle | Surface 1 ground truth. ~100 rows, metrics verified by a human | Generated by you, **verified by the user** | P4 |
| Realistic volume dataset | Demos, GST AP table, judge realism | The user's separate workstream | P5 / P8 |

**Never use a generator as its own test oracle.** If the generator plants 47 duplicates and the
test finds 47, you have proven they agree — not that either is right. Plants are declared
explicitly in a sidecar, never inferred from generator logic.

The generator satisfies `contract.yaml`; the contract does not describe the generator. Later,
governed views satisfy the same contract with no orchestration change.

**`build_demo_data` (`app.py:229–287`) is deleted in P3.** It was the right call for a prototype
with no data — a deterministic seeded population that makes the UI demonstrable — but its `RF_*`
flags are random, so no finding derived from it means anything and it must not be mistaken for
synthetic audit data. Demo mode becomes "a completed run over a realistic dataset, persisted in
Delta like any other run, clearly labelled".

---

## 9A. Open questions — answer before the corporate migration, not after

These are unresolved and shape the schema or the deployment. Raise them early; several need a
platform owner's decision, not a code change. `docs/DATABRICKS_ARCHITECTURE.md` is the version of
this list written for a Databricks Solutions Architect.

1. **Whose identity runs the audit?** A Databricks App can run as its service principal, or on
   behalf of the signed-in user. This is the biggest open governance question:
   - As a service principal, every auditor sees whatever the SP can see. Unity Catalog row filters
     and column masks on executive expense data are bypassed, and `runs.run_owner` is a value the
     app asserts rather than an identity the platform verified.
   - On behalf of the user, UC permissions apply per auditor and the identity is real — but the
     executor runs on a background thread, not in the request, so the signed-in user's context has
     to be captured at `start_audit_run` and carried in `RunState`, or the pipeline silently falls
     back to the App's own reach.
   Decide before P5. `RunState` must carry a verified `run_owner` either way.
2. **Can a run be retracted?** Findings leave the system as PPTX and XLSX. If a run is later found
   wrong, there is currently no way to mark it superseded, and no way to tell which exports came
   from it. Add `runs.superseded_by` and stamp every export with `run_id` + timestamp. Cheap now,
   very expensive later.
3. **Where does CI run?** Tier A gates need to execute somewhere that can reach a workspace for
   the Delta-backed tests. GitHub Actions will not reach a corporate Databricks workspace. Either
   the gates run against `LocalPersistence` only in CI (and the Delta path is covered by a nightly
   job inside the workspace), or CI moves into Databricks. Decide in P9; design `PersistenceAdapter`
   in P1A so the first option is possible.
4. **Delta time travel vs data deletion.** Executive expense data is personal information. Delta
   retains history, so a deleted row remains readable via time travel until `VACUUM`. A retention
   and vacuum policy must exist before real data lands. This is a platform-owner decision.
5. **Export egress.** A PPTX naming executives and their spend downloads to a laptop. Whatever DLP
   or classification regime applies to that is out of this codebase's control, but the exports must
   at least carry a classification label and the run ID.
6. **Model endpoint region and cross-geo processing.** If pay-per-token endpoints are not served in
   the workspace's region, inference may cross geography. For this data that is a governance
   decision with lead time, not a config flag. Establish it before P6.
7. **Rate limits and quotas.** AI Gateway can rate-limit an endpoint. `classify` submits batch work
   via `ai_query`; behaviour under throttling must be defined — queue, degrade, or fail the run.
   Whatever it is, it must not be a silent partial result.
8. **Secrets.** The App reads environment variables today. A Databricks secret scope is the
   better home for the model-serving and warehouse credentials, and becomes necessary if the
   sensing module (§4.10) ever runs outside the App. Decide in P5.

---

## 9B. Testing a deployed Databricks App

Five layers, cheapest to most expensive. Layers 1–2 run on every deploy; layer 3 on gated
deploys; layers 4–5 at phase boundaries.

### Layer 1 — Smoke (seconds)

Hit `/health` from `curl` or the Databricks CLI. Add a `/ready` endpoint that also checks Delta
connectivity (a single `spark.sql("SELECT 1")` or equivalent catalog-list call). If `/ready`
passes, the container started, Dash is serving, and the runtime is wired to the workspace.

### Layer 2 — API-level integration (minutes)

Dash callbacks are HTTP endpoints. `POST` synthetic callback payloads against the running App URL
— file upload, parameter changes, pipeline trigger — and assert response shape. This catches
serialisation bugs, missing columns, and broken callback chains without a browser. Run from a
notebook or a lightweight script triggered after deploy.

### Layer 3 — Browser E2E (minutes, gated)

Playwright (or Selenium) against the App URL. Key scenarios:

| Scenario | Asserts |
|---|---|
| Upload fixture CSV | Dashboard renders all tabs, no JS errors |
| Trigger a pipeline run | Status callback arrives; findings appear |
| HITL confirm + reject | State survives page refresh (Delta round-trip, not callback memory) |
| PPTX export | File downloads; slide count and chart presence correct |
| App restart resilience | Mid-run: restart App → reload → reaper marks `interrupted` → Resume completes; completed run: restart → findings still visible from Delta |

### Layer 4 — Delta persistence verification (minutes)

Query Delta tables directly from a notebook or SQL warehouse after the E2E run. **Check only the
tables expected by the current phase** (see table-to-phase matrix in §8 — a P1A verification does
not assert `findings` exist; a P3 verification does).

- `runs` table: expected `run_id`, status, timestamps. *(P1A+)*
- `run_fingerprints` table: fingerprint_id matches `RunState.fingerprint_id`, source versions
  recorded. *(P1A+)*
- `run_state` JSON: parseable, contains expected node outputs, `state_version` ≥ 1. *(P1A+)*
- `node_attempts`: rows for each executed node, outcomes correct. *(P1A+)*
- Reaper: insert a stale run left `running`, trigger reaper, confirm it is marked `interrupted`
  (never deleted — audit run records are evidence). *(P1A+)*
- `engagements`: seeded default engagement present. *(P1B+)*
- `findings` table: rows match what the UI showed. *(P2+)*
- Suite tables (`risks`, `controls`, `risk_assessments`, `review_notes`, `issues`): DDL exists
  from P1B; populated as each phase writes to them. *(P1B+ for DDL, per-table for data)*

### Layer 5 — Model-serving integration (minutes, needs endpoint)

- Send a known evidence payload to `generate_findings`; assert valid JSON with `findings` key.
- Send a malformed payload; verify error handling doesn't crash the App.
- Measure latency against the App's configured timeout.
- `classify` via `ai_query` on a small batch; confirm results land in Delta, not returned inline.

### Practical pattern for Free Edition

No CI can reach the workspace easily. Keep a `tests/e2e/` folder with pytest scripts that assume
a running App URL via `APP_URL` env var. Run manually after deploy:

```bash
APP_URL=https://... pytest tests/e2e/ -v
```

When porting to Optus, these same scripts run in a CI pipeline. Design `tests/e2e/` in P3 (when
the pipeline loop exists), expand it each phase.

---

## 9C. Acknowledged risks and deferred items

Items identified by independent review that are real concerns but are either governance decisions
requiring a platform owner, or production-grade requirements that would delay the prototype
without proving correctness. Each is noted with the phase or milestone where it becomes blocking.

### Concurrency model

The `Semaphore(2)` concurrency cap (§2.3) protects only one Python process. It does not prevent
duplicate execution across workers, overlapping deployments, or two containers. The current
`app.yaml` runs `python app.py`, which starts one process — but this is a configuration choice,
not a platform guarantee. Databricks Apps support custom commands including Gunicorn with multiple
workers, and redeployments or restarts can overlap even within a single-process configuration.
The persistence layer must therefore be correct from P1A, because retrofitting concurrency
controls onto an existing schema is expensive and error-prone.

**P1A delivers** (foundational, not deferred):
- `run_state.state_version` — integer, incremented on every state transition.
- **Compare-and-swap on every transition.** `UPDATE run_state SET ... WHERE run_id = ? AND
  state_version = ?` — if zero rows affected, the transition is rejected (stale state). No
  transition is valid without matching the current version.
- **Deterministic node execution keys.** Each node attempt gets a key derived from
  `(run_id, node_name, attempt_number)`. Idempotent `MERGE` into `node_attempts` keyed on this —
  a resumed run that replays a completed node is a no-op, not a duplicate insert.
- **`node_attempts` table** with: `attempt_id`, `run_id`, `node_name`, `attempt_number`,
  `execution_key` (the deterministic key), `started_at`, `completed_at`, `outcome`
  (`succeeded | failed | interrupted`), `error_detail`, `state_version_before`, `state_version_after`.
- **Failure-injection tests:** proving CAS and idempotent MERGE work together under:
  - node output written but state not advanced (crash between node completion and state persistence) — resume must not re-execute the node;
  - state update attempted twice (duplicate CAS) — second attempt must be rejected;
  - two resume requests racing on the same run — exactly one must succeed, the other must get a CAS rejection;
  - executor dying mid-node — `node_attempts` records `interrupted`, resume re-executes that node only.

**Deferred to P3** (production-grade, requires multi-worker):
- Persisted worker claim (`claimed_by`, `lease_expires_at`, `heartbeat_at`) with renewable leases.
- Cross-worker duplicate-execution tests.
- Lease timeout and automatic re-acquisition.

### Corporate-migration prerequisites

These are not prototype deliverables. They are decisions or controls that must exist before
real data enters the system.

| Item | What it requires | When it blocks |
|---|---|---|
| Authorization model (preparer/reviewer/approver) | Governance decision: enforced in UC, the App, or both | Before P7 (HITL gates) |
| Retention and legal hold | Separate retention for raw uploads, evidence, prompts, model responses, exports, Delta history | Before P5 (real data) |
| PII and security threat model | Malicious uploads, prompt injection, PII leakage, stored XSS, path traversal, spreadsheet-formula injection, oversized files, export classification, log redaction | Core controls by P5; detailed model by P6 |
| Free-to-Optus capability matrix | Checked matrix for both environments: Apps, Jobs, SQL warehouse, identity, service principals, model endpoints, regional processing, ai_query, AI Gateway, system tables, Volumes, secrets, network, deployment permissions | P1A (create the matrix); P5 (validate it) |
| Schema migrations and rollback | Schema versioning, forward migration tests, backup/restore, rollback rehearsal | P1A foundation; P9 rehearsal |
| Operational observability and alerting | Health, readiness, queue age, stuck leases, node failures, rate limits, quota exhaustion | P3 (baselines); corporate deployment (routing) |
| Skill versioning and rollback | Immutable Skill content hash stored per run; publishing, superseding, rollback and compatibility rules | P1B / P2 |
| Data quality framework | Uniqueness, referential integrity, allowed values, join cardinality, duplicate source rows, mixed currency, timezone, date coverage, schema drift | P2 DoD |
| Performance budgets | UI acknowledgement latency, callback latency, run duration, memory ceiling, SQL volume, model-call budget | P1A (baselines); enforce from P3 |

### Chain-of-thought storage

Do not design around storing raw model reasoning traces (`reasoning_content`). Store structured
rationale, citations and decision summaries. Raw traces are potentially sensitive, not exposed
consistently by providers, and are not required for the audit trail. `llm_calls` logs the prompt
and the response content; that is sufficient.

---

## 10. Working rules

- Work on the branch the user specifies. Commit at every stable checkpoint.
- Commit format: `[P2] feat(primitives): add duplicate_detection`.
- Run `python -m py_compile` on changed files and `pytest` before every commit. Fix failures first.
- Do not push unless asked. Do not use `--no-verify` or `--force`.
- Ask before adding a dependency or editing `requirements.txt`.
- Do not connect to any workspace other than the one in the environment variables.
- Never write a token to source, and never overwrite `.databrickscfg`.
- Do not add docstrings, comments or type annotations to code you are not otherwise changing.
- Do not create new markdown documentation files unless asked.
- Do not touch `assets/` or the design system. Do not add UI patterns beyond
  `src/platform/components.py`.
- **If this brief conflicts with what you observe in the code, say so and ask. Do not assume.**

### Model discipline — this project has a token budget

| Model | Use for | Never |
|---|---|---|
| **Opus** | Phase kickoff plans, phase-gate reviews, schema design (P1), prompt + G11 design (P6), planner schema (P8) | Writing implementation code, tests, YAML, migrations |
| **Sonnet** | All implementation, tests, debugging, deployment. The default | — |
| **Haiku** | Single-file mechanical work with a precise spec: DDL, YAML, boilerplate, renames | Anything multi-file or requiring judgement |

`.claude/settings.json` sets Sonnet as the default. Three subagents are defined with pinned
models: `implement` (Sonnet), `mechanical` (Haiku), `review` (Opus).

**If the session model is Opus and the task is implementation, delegate to the `implement`
subagent — do not edit files directly on Opus.**

**Model split in practice (user decision, 2026-09-24).** Most token cost is agents re-reading
their own context, not the model tier. So:
- **Haiku (`mechanical`)** takes every exact-spec, low-judgement task:
  - DDL migration files with specified columns;
  - YAML (thresholds, finding templates, plan entries) written to a given spec;
  - `.env.example`, `docs/CAPABILITY_MATRIX.md` and `docs/PLATFORM_REQUIREMENTS.md` updates;
  - recording user decisions in this file;
  - formatting reports and spreadsheets from numbers the coordinator computed;
  - turning a finished test report into the user's test script;
  - portability and developer-string sweeps, and renames.
- **Sonnet (`implement`)** is for implementation and debugging only, in **small, narrowly scoped
  batches**.
  - Run the tests you touched while iterating, and the full suite once before the final commit.
  - Split mechanical steps (screenshots, Delta checks) into separate short tasks.
- **Opus** is for design, gate reviews and coordination only. It does not edit files directly
  except for trivial fixes.

Opus kickoff is warranted for P1, P2, P6, P8 (design-only, then hand off).
Sonnet kickoff is sufficient for P3, P4, P5, P7, P9.

---

## 11. Day one — verify before building

Run this first, on Sonnet, before any design work. Record the results in §6 of this file.

### Platform existence check (run once, all phases)

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
print(w.current_user.me().user_name)                    # host + token + network policy
print([e.name for e in w.serving_endpoints.list()])     # the two endpoints
print([c.name for c in w.catalogs.list()])              # Unity Catalog access
print([wh.name for wh in w.warehouses.list()])          # SQL warehouse
```

Confirm in the workspace UI that these exist: **Databricks Apps**, a **serverless SQL warehouse**,
and **pay-per-token model serving**. Record the results. Jobs is not required — compute runs
inside the App (§2.1). Jobs is only needed if the risk-sensing module is built (§4.10).

### Phase gates (block only when the current phase needs the feature)

| Feature | Blocks if absent | Rationale |
|---|---|---|
| **Databricks Apps** | All phases | The entire application runs here |
| **Serverless SQL warehouse** | All phases | Every Delta read/write goes through it |
| **Unity Catalog** | All phases | System of record for runs and evidence |
| **Pay-per-token Model Serving** | **P6** (LLM layer) | P1A–P5 have no LLM calls; P6 is where `find`, `classify`, `export` first call an endpoint. If Model Serving is absent at P1A, note it and continue — it must be available before P6 starts |

If a feature marked "All phases" is missing, **stop and report** — it changes the plan, not the
code. If Model Serving is missing and the current phase is before P6, note it as a risk and
continue building.

### Endpoint parameter check (run once, before P6)

One chat completion against each endpoint, recording which parameters pass through
(`response_format`, `temperature`, `max_tokens`, reasoning-effort controls) into §6.

### Known environment issues

- **`databricks-sdk` may fail to import** with `pyo3_runtime.PanicException` from
  `cryptography` via `google.auth`. The system `cryptography` in `/usr/lib/python3/dist-packages`
  is broken. Fix with a venv or a pinned `cryptography` upgrade in `/usr/local`. A SessionStart
  hook should handle this.
- **Proxy diagnostics:** if outbound calls fail, `curl -sS "$HTTPS_PROXY/__agentproxy/status"`
  lists recent denials by host. A `403` on CONNECT is a network-policy denial; a `502` with
  `injection failed` is a misconfigured managed connector. Both are environment settings —
  report them, never work around them, and never disable TLS verification or unset `HTTPS_PROXY`.
- **SessionStart hook** (`.claude/hooks/session-start.sh`, remote only): reinstalls `cryptography`,
  `cffi` and `blinker` with `--ignore-installed` (Debian copies have no RECORD file), installs
  `requirements.txt`, and exports the gitignored `.env` into the session.

### Recorded results — 2026-09-23

| Check | Result |
|---|---|
| Identity / network | OK — SDK authenticates; no proxy denials |
| Unity Catalog | Metadata OK (catalogs `samples`, `system`, `test_workspace`; schema `test_workspace.audit_ledger` created). **Storage FAILS**: credential `test_workspace` (role `…-catalog-role`) cannot be assumed — Delta writes impossible |
| SQL warehouse | `Starter Warehouse` is **Pro, not serverless**. Launch FAILS: `WORKSPACE_CONFIGURATION_ERROR`, `sts:AssumeRole` AccessDenied on the workspace role |
| Databricks Apps | API reachable, 0 apps. Deploy **untested** — uploading source to workspace files fails with `Cannot access AWS bucket` |
| Model Serving | OK — `databricks-claude-sonnet-5`, `databricks-gpt-oss-120b` present (parameter matrix not yet run) |

Root cause for all three failures: the workspace's AWS IAM roles no longer trust Databricks.
That workspace was abandoned.

### Recorded results — 2026-09-23, current workspace (Databricks-managed storage)

The user supplied a second workspace; host and token live only in the gitignored `.env`.

> **Before ANY Databricks call (SDK, SQL, live tests, deploy): `set -a; source .env; set +a`.**
> The session's own `DATABRICKS_HOST`/`DATABRICKS_TOKEN` may still point at the abandoned first
> workspace (`Starter Warehouse`, `sts:AssumeRole` errors). If you see those errors, you forgot to
> source `.env` — it is not a platform outage.

| Check | Result |
|---|---|
| Identity / network | OK — PAT authenticates; host reachable through the session proxy |
| Unity Catalog | OK — catalog `orchestrationsuite` (Databricks-managed storage); schema `audit_ledger` created |
| Serverless SQL warehouse | OK — `Serverless Starter Warehouse`; cold start 8.5 s, warm query 1.2 s |
| Delta semantics | OK — CAS `UPDATE … WHERE v = expected` returns 1 then **0** affected rows; CHECK constraints enforced; deletion vectors + row tracking accepted; `DESCRIBE HISTORY` works; single-row write 2–8 s |
| Databricks Apps | OK — app created (139 s, compute MEDIUM) and deployed (9 s); app URL requires OAuth (PAT is redirected to login), so programmatic E2E needs an OAuth/M2M identity |
| App → warehouse | Warehouse attachable as an App resource (`CAN_USE`); App service principal granted via `GRANT … TO \`<sp client id>\`` |
| Model Serving | OK — `databricks-claude-sonnet-5`, `databricks-gpt-oss-120b`, `-20b` present (parameter matrix still to run before P6) |

### Recorded data decisions for SKILL-001 (from the user, 2026-09-23)

Source files are in `synthetic_data/`; row counts match `FILE_REGISTRY` exactly.

1. **ExCo population** — the prototype's 11 `EXCO_MEMBERS` names do not occur in the synthetic data.
   The user supplied the synthetic ExCo list; resolved to `Employee ID` from `Expense_Report_Combined`
   (prepared and cross-charge-approver IDs agree). The contract keys on these IDs (§0.5):
   Chen, Alex `52725` · Nakamura, Yuki `52394` · Okonkwo, Emeka `52622` · Johansson, Erik `52424` ·
   Mendes, Sofia `51867` · Kowalski, Jan `52660` · Thompson, Sarah `51606` ·
   Delacroix, Marie `52472` (443 claims) **and** `50040` (27 claims) — *which is ExCo is pending the
   user's decision* · van den Berg, Lucas `52674` · Ramirez, Carlos `52703` · Fitzgerald, Aoife `50079`.
2. **Column names** — use the names actually present in the files, not those referenced in
   `computation.py` (e.g. `Report Receipt Viewed` / `All Entry Receipts Viewed`).
3. **Whitespace** — trim header whitespace (`Advance Purchase Days `, `Currency `) as an explicit,
   declared contract rule. Not fuzzy matching.
4. **T3.1b join** — bookings carry no Travel Request ID or Employee ID. Join on
   **employee name + expense type** (booking `Booking Type` ↔ request `Expense Type`, via a declared
   mapping). Report the match rate.
5. **Country for T6.1d** — expense rows have no country. Derive it from
   **`City/Location` + `Transaction Currency`** via a declared lookup; unmapped rows are reported, never defaulted.
6. **Currency** — `Reimbursement Currency` is AUD on 100% of rows in every file. The contract requires
   AUD and fails the run otherwise. Per-diem rates are **SGD**. The user delegated the rate source.
   Decision: convert with the **RBA F11.1 monthly-average A$/S$ rate for the transaction's month**, read
   from the pinned snapshot `synthetic_data/reference/RBA_F11.1_exchange_rates_published_2026-09-22.csv`
   (sha256 `ab6fa92d…4cec9cec7`), whose hash goes into the run fingerprint. A single fixed rate was
   rejected: A$1 ranged S$0.806–0.915 over the audit period (period mean S$1 = A$1.1703, period-end
   S$1 = A$1.0965), a spread large enough to move per-diem exceptions near the limit.
7. **Oracle** — the user holds an auditor-confirmed exception list for this data; it is the Surface 1
   comparison set (§9).

### Operating directives from the user (2026-09-23) — these override §8 and §10 where they conflict

- **Run continuously** from phase to phase until the target end state (all phases, Tier A gates,
  deployed and working App, portable to the corporate workspace). Report at each phase gate but do
  not wait for approval. Still stop for: platform blockers, inputs only the user holds, and human
  sign-offs (audit-approved specs, named Skill reviewers) — mark those `pending approval` and continue.
- **Model allocation per §10** via the pinned subagents: design and gate reviews on Opus (`review`),
  implementation on Sonnet (`implement`), exact-spec mechanical work on Haiku (`mechanical`).
- **Push** to `claude/bold-faraday-w9gb8y` at every phase checkpoint. Never force-push, never `main`,
  no PRs unless asked.
- **`requirements.txt`**: add `openpyxl` and `databricks-sql-connector`; remove `langgraph` and
  `langchain-core` (NN1). Further dependencies may be added when a phase needs them — name each one in
  the phase report.
- **Workspace resources** may be created in `test_workspace` once it works: a small serverless SQL
  warehouse with auto-stop, the App, a Volume for uploads, tables in `audit_ledger`.
- **Skill reuse across audit projects (queued after the connected App works, user-approved
  2026-09-23):** explicit column mapping at run setup (recorded in the run fingerprint, never
  fuzzy); project-specific reference data (population of interest, supplier lists, lookups) as run
  parameters with provenance; per-test source requirements so a missing source makes only its
  tests `not_testable`; any mapping, parameter or override makes plan confirmation mandatory and is
  shown in finding provenance.
- **§9A defaults (revisit before corporate migration):** the App runs as its service principal and
  records the signed-in user from the forwarded identity as `run_owner` (Q1); CI runs Tier A against
  `LocalPersistence`, Delta-backed tests run inside the workspace (Q3).

### Approval decisions from the user (2026-09-23): "accept all defaults, allow self sign-off for now"

- **Self sign-off allowed until P7.** When the approver equals `run_owner` the sign-off is recorded
  with `self_approved: true, sod_enforced: false`, and the trace event, run page, `/runs`,
  `/workspace/tne` and the XLSX run metadata all label it "self-approved — segregation of duties
  not enforced". P7 replaces this with an enforced preparer/reviewer/approver rule; keep it
  data-driven so that is a config change.
- **Every default in `docs/specs/SKILL-001_test_specification.md` is accepted as stated**:
  ExCo ID 52472 (50040 excluded as a namesake), cancelled requests out of scope, the T3.1b
  class mapping with no date condition, gifts excluded from T3.3b, the T5.1 "every line under the
  limit" rule, `Parent Key` itemisation, the T6.1a instant-approval scope, and the T6.1d population
  and expense-type scope.
- **Thresholds and preferred-supplier lists stay `provenance: analyst-set`,
  `pending_policy_confirmation: true`.** No policy reference was supplied, so accepting the default
  values does not make them policy. The UI labels stay.
- **The RBA F11.1 monthly-rate method (decision 6) is accepted.**
- **Still outstanding (user-held):** the auditor-confirmed exception list for Surface 1, and a named
  reviewer to publish SKILL-001.

### Cost incident and fixes (2026-09-24)

The deployed App's executor polled Delta about 3,000 times an hour, even with nothing to run. That
kept the serverless SQL warehouse awake from 02:00 to 22:00 UTC on 2026-09-23, using about 230 DBU
(about $218 at list price). The App and the warehouse were stopped. The fixes, applied at the
user's request:
- No warehouse polling while idle. The executor wakes on in-process signals, plus a slow safety
  sweep; it polls every 30–60 s only while runs are active. UI intervals run only for active runs.
- A test on idle query volume.
- Warehouse auto-stop set to 1 minute. The bootstrap in the porting kit sets the same.
- A post-deploy check that the warehouse actually auto-stops while the App is idle.

Never restart the App on a build that lacks the idle-polling fix.

### Decisions from the user after the independent review (2026-09-24)

- **The headline is the amount at risk.** It keeps the prototype's "Potential exposure" label and
  wording.
  - Each distinct flagged transaction line counts once.
  - A line's value is the largest amount at risk that any finding attributes to it:
    - `spend` findings attribute the full amount of the line;
    - `excess` findings attribute only the at-risk part — the extra lines beyond the first in a
      duplicate group, or the over-limit part of a per-diem day, allocated pro rata to that day's
      lines;
    - `approved_not_spent` findings are excluded and reported separately.
  - Line identity is the row identity for row-grain sources. A declared `entry_key` applies only
    where the grain genuinely repeats (attendee rows), and a declared `entry_key` that is not unique
    fails the run.
- **"—" replaces a fabricated "$0"** wherever no figure exists: a run in progress, or a finding with
  no money involved. It appears in the same slot, in the same style.
- **The `/run/<id>` page is approved.** It is the run status page with the confirm, sign-off and
  resume buttons; every prototype page stays unchanged.
- **Build the PPTX export now** (§4.7). Do the P4 defect fixes and the P6 target structure
  immediately. The executive summary and themes slides use deterministic content, labelled as such,
  until the LLM layer lands; nothing is fabricated.
- **Remove the `/actions` line "Session-only persistence in demo mode — actions reset when the app
  restarts".** It is false on the Delta backend.
- **Correct two untrue phrases on `/workspace/tne`.** The risk-distribution subtitle drops
  "recurrence": nothing uses it until prior-period comparison exists. The action-panel subtitle
  becomes "Action ownership and responses are saved with this run."

### Corporate workspace assessment (Genie Code, 2026-09-24) — design consequences

Genie Code assessed the corporate workspace against `docs/PLATFORM_REQUIREMENTS.md`. It looked at
the **prototype** app already deployed there, so its env-var names (`UC_CATALOG`,
`DATA_VOLUME_PATH`, `LLM_ENDPOINT_NAME`, ...) are the prototype's. Ours stay `DBX_*`/`MODEL_*`; the
porting kit maps them. **No corporate names, ids or paths may be committed** (NN16): they live only
in the gitignored corporate `.env`.

Findings that change the design:
- **`ai_query()`, `ai_classify()` and `ai_gen()` are denied on the corporate warehouse.** `classify`
  (T4.3) calls Model Serving from Python in batches instead; §6's "`classify` via `ai_query()`" no
  longer applies. Sending row text to a model needs its own governance approval. Until then T4.3
  stays `not_testable`.
- **`system.query.history` and `system.serving.endpoint_usage` are not available.**
  - Idle-cost checks use `system.compute.warehouse_events`, or the warehouse state.
  - LLM usage comes from the inference tables and `llm_calls`.
  - Every system-table reader reports "not available" rather than failing.
- **Source data there is Excel files in a UC Volume, not Delta tables.**
  - Support Volume files as run sources, bound by explicit per-environment configuration (never
    filename guessing), pinned by SHA-256 and contract-validated.
  - Loading them into Delta stays an option.
  - The corporate Volume has no per-diem file.
- **Binary Python packages need vendored `manylinux` wheels.** Pure-Python packages install from
  PyPI.
- **Operations must run from a workspace cluster/notebook,** with notebook authentication and
  Git-folder deploys. Genie Code there edits files but cannot execute code.
- **Confirmed available:** Apps, a serverless warehouse (shared, 2X-Small, 1-min auto-stop), a UC
  catalog/schema/Volume, MLflow under `/Shared`, `system.access.audit`, billing and compute system
  tables, inference tables, UC tags, secret scopes (a new one must be requested), a READY Claude
  Sonnet 4.5 endpoint and GPT-OSS endpoints, and PII/safety guardrail endpoints.
- **Still to verify there:** Claude access from the App's service principal, the App compute
  size, the identity headers, upload and timeout limits, Delta constraint features, `VERSION AS OF`
  on sources, and data residency.
- **No external egress** (ServiceNow, Glean) until requested. Genie's claim that the App's
  "warehouse is not wired as a resource" is about the prototype; our deploy script attaches it. The
  Volume `/dbfs` FUSE path does not apply: we use the Files API.

User decisions on those findings (2026-09-24):
- **Read source files directly from the Volume.** An explicit per-environment mapping binds each file
  to a Skill contract source: an exact path, no guessing. The file's SHA-256 is pinned in the run
  fingerprint, and CSV/XLSX files are contract-validated. Delta loading remains optional.
- **Per-diem rates must be supplied in the Volume.** They are not bundled with the Skill. A missing
  file fails the run loudly, and there is no fallback.
- **T4.3 stays `not_testable` ("awaiting governance approval to send expense descriptions to a
  model")** until approved. The Python classify capability may be built, switched off by
  configuration.
- **Request a dedicated schema for the ledger at Optus.** The schema is configuration either way.

### Build-here directive (user decision, 2026-09-24)

**Build everything in the full vision here, fully tested against stand-ins, so Genie Code at the
corporate workspace only has to supply configuration, real data mapping and governance values.**
Genie Code there can edit files but cannot execute code, so nothing that needs an iterate-and-test
loop may be left for it. Build order, after the current deploy/test round and the user's
acceptance testing:
1. Fixes from both test rounds, then a final independent gate review closing P2–P5.
2. **Column mapping at run setup** (Skill reuse): explicit, recorded in the fingerprint, never
   fuzzy.
3. **Surface 1 harness**: drop in the auditor-confirmed exception list at the corporate workspace.
4. **Explorer Mode + LLM narration layer** (P6/P8): G11, G14, G15 and degraded mode.
5. **Skill authoring kit**: scaffold, validator runnable from a notebook, and a planted-fixture
   generator, so new Skills are YAML-only work.
6. **P7 review workflow**: preparer → reviewer → approver with segregation of duties, driven by
   configured group names; review notes; narrative edit trail. This replaces self sign-off.
7. **Lifecycle modules** per `docs/specs/LIFECYCLE_design.md` and the user-approved mockups:
   - research foundation;
   - Risk Assessment + impact analysis;
   - Audit Planning + template library;
   - Reporting & ServiceNow;
   - continuous monitoring;
   - control design assessment;
   - evidence & workpapers;
   - engagement home.

   Connectors are built against published APIs with recorded fixtures: ServiceNow REST, Glean
   REST + MCP transports (MCP off until approved), and a guardrail-endpoint hook for prompts.
8. **SKILL-002 GST** on planted synthetic AP data.
9. **Governance switches as configuration:**
   - export classification label/watermark;
   - run retraction (`runs.superseded_by`);
   - retention/VACUUM job;
   - on-behalf-of-user identity option.
10. **Operations as code**: health/queue/cost dashboard, alerts, and the nightly Tier A job.
11. **Porting kit last**: pre-flight checker, bootstrap, environment profiles, offline wheelhouse,
    `.gitlab-ci.yml`, a test-runner notebook, `docs/PORTING.md`, and a Genie Code runbook listing
    exactly which values and decisions remain.

### Further decisions from the user (2026-09-23)

- **UI is the prototype's, exactly.** Every page matches `reference_app/src/platform/pages.py` and
  `components.py`: same elements, text, ids and layout. Wire the existing UI to the real backend;
  never add, remove, relabel or restyle anything without asking the user first. A layout-parity test
  enforces this. The one approved deviation: a self-approved sign-off is stated in the UI, as text
  appended where the prototype already shows who signed off.
- **Deferred to the Optus port:** the Surface 1 auditor-confirmed exception list (it is real data and
  never enters this environment), threshold policy references, and the preferred-supplier lists
  (including hotels). Build the Surface 1 harness so the list can be dropped in there.
- **LLM calls in this development workspace are approved** for schemas, counts, null rates and other
  aggregates (never rows). Model region and governance are re-decided at the Optus port.
- **Skills stay in our own `skill_versions` ledger** (content-hash pinned per run). Unity Catalog
  Skills (Beta, 2026-08) hold assistant instructions, not executable methodology, document no
  version pinning and load over MCP; an optional "publish to UC Skills" step comes later, once GA and
  confirmed at Optus.
- **Lifecycle roadmap (approved 2026-09-23), in order:** (1) restore the prototype UI;
  (2) Explorer Mode with the P6 LLM layer (model client, `llm_calls`, `llm_cache`); (3) clickable
  mockups of the lifecycle modules (engagement home, risk register and assessment, planning/RCM,
  issues, risk sensing), built from existing components only, **approved by the user before any
  build** — existing pages never change; (4) research foundation: `JobsExecutor`, a
  `KnowledgeSourceAdapter` over a UC Volume document drop with document-level cache, and a bounded
  multi-agent research package (hard call/cost ceilings, every query and read logged, citations
  mandatory, G17) — confined to sensing/assessment per NN1's exception, never imported by fieldwork;
  (5) Risk Assessment v1 — agent-proposed, cited, four-dimension scoring shown as proposals, nothing
  accepted without an auditor; the scoring methodology stays the enterprise risk function's to
  supply. Includes **impact analysis** that combines qualitative evidence (cited document passages)
  with quantitative fieldwork evidence (exception rates, the de-duplicated flagged-spend headline,
  materiality) from full-population runs; (6) **Audit Planning v1 (approved 2026-09-23)** —
  `run_kind: planning`, structured-generation class (§4.10: one call, strict schema, validate, ≤1
  repair, human-confirmed, replayable from cache — not a research loop). From accepted, scored risks
  it proposes an RCM (risks → controls → tests, each test with `assertion: design|operating`), an
  audit scope memo, test steps and a document request list, from agreed templates. Test steps link
  to executable Skill tests/primitives wherever one fits, so an approved plan launches fieldwork
  runs; the auditor edits and approves before anything runs. Step (3)'s mockups include the impact
  analysis, RCM and planning screens. A **template library** (scope memo, test step and request
  list templates, versioned and owned like Skills) ships with this step.
  **Porting kit (approved 2026-09-23; build it only after the build's own end-to-end testing is done
  AND the user has finished acceptance testing and all their feedback is addressed — user decision
  2026-09-24).** Deployment
  stays **Databricks SDK-based** (`scripts/deploy_app.py`, confirmed allowed at Optus); Asset
  Bundles are optional and unconfirmed there. The kit covers:
  - a pre-flight checker that fills the Optus column of `docs/CAPABILITY_MATRIX.md`;
  - an idempotent bootstrap (schema, Volume, grants to the App SP, secret scope, source-contract
    check);
  - a `dev`/`optus` environment profile layout with `.env.example` per target;
  - post-deploy smoke checks and Layer 4 Delta checks (§9B);
  - an offline wheelhouse option built from `requirements.lock` (whether Optus Apps can reach PyPI or
    a mirror is unknown), vendoring `manylinux2014_x86_64` wheels;
  - a `.gitlab-ci.yml` running Tier A against `LocalPersistence` (Optus code lives in **GitLab**;
    no GitHub-specific tooling);
  - `docs/PORTING.md`, the step-by-step runbook including the open governance items. It excludes
    `.env`, tokens, dev-workspace runs and anything environment-specific; synthetic data is optional,
    for demos.

  Every later feature is deployed through this path.
  Further steps (approved 2026-09-23), each mocked up for approval before build:
  (7) **Reporting & ServiceNow integration** — the audit team is moving to ServiceNow and leadership
  will track issues and remediation there, so **this app does not build issue tracking or a
  remediation tracker**. It builds an issue-management agent that drafts issue descriptions and
  remediation plans from signed-off findings, a `ServiceNowAdapter` behind an `IssueTrackerAdapter`
  Protocol that submits them only after an explicit human approval (never automatically; "Preview —
  not submitted" until connected, NN13), stores the returned ServiceNow record ids on
  `issues`/`management_actions`, and reads status back for rollforward. Credentials live in a
  secret scope; transport is ServiceNow's REST API — no MCP (§7). Plus an audit committee report
  and a leadership view of engagements and themes (not issue tracking). Jira is superseded by
  ServiceNow. (8) **Continuous monitoring** — scheduled Skill runs as Databricks Jobs, each run
  compared with the previous one, key-risk-indicator thresholds raising alerts that feed Risk
  Assessment. (9) **Control design assessment** — design-effectiveness conclusions with rationale
  against the planning RCM (`assertion: design`). (10) **Evidence and workpapers** — request-list
  tracking, document evidence ingestion with citations and annotation, prior-year workpaper reuse,
  and workpaper generation; a separate subsystem, scoped on its own. The review workflow
  (preparer → reviewer → approver, review notes) stays in P7. Statistical sampling stays deferred.

---

## 12. What to do first

1. Run §11. Stop and report if anything fails.
2. Read the files in §0, in full.
3. Confirm you understand §0.2 — why the prototype is shaped the way it is, that `computation.py`
   is the best available behavioural reference (not an unquestionable oracle), its known semantic
   defects, and which behaviours must not survive into the build. State it back in your own words.
4. Produce a **one-page plan for P1A only** (the immutable run ledger): `RunState` field-by-field
   justification, the run fingerprint (`run_fingerprints` table), the explicit state machine with
   optimistic concurrency (CAS on `state_version`), Delta DDL for `runs`, `run_state`,
   `run_fingerprints`, `node_attempts`, `trace_events`, persistence contract, reaper, test
   approach (including failure-injection test for CAS + idempotent MERGE), risks.
5. Stop. Do not start P1B until P1A is reviewed.
