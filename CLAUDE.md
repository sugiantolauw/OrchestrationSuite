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

### 0.2 What does NOT work — read this twice

**The app does not compute most of its audit tests. It counts flags it did not produce.**

`app.py:compute_evidence_payload` (line 315) derives most metrics by summing pre-existing
`RF_*` columns — `flag_count("RF_CS_SplitClaims_SameDay")` and similar (lines 324–362).
The detection logic that produced those columns lived in an upstream notebook that is **not
in this repository**. In demo mode the flags are random: `rng.random(n) < p` (lines 243–261).

**`src/computation.py` is never imported by `app.py`.** Check the import block at lines 24–49.
Neither are `narrative.py` or `guardrail.py`. All three are orphans.

**The two implementations disagree** where they overlap:
- T6.1d daily spend: per-diem-rate-driven in `computation.py:568`; hardcoded `$1,000` in `app.py:415`.
- T3.3a late booking: `Advance Purchase Days` in `computation.py:147`; `RF_CS_LateBooking` flag in `app.py:388`.

**`computation.py` contains memorised results from a prior audit, presented as computation:**
- `compute_test_4_3_cached()` returns `flagged: 132` (line 288)
- `compute_test_6_1c()` returns `exceptions: 0`, "Based on audit findings" (line 529)
- `intl_exceptions: 2, # From audit findings` (line 590)
- `total_records: 152_921`, `total_files: 8` (lines 641–642)
- `aus_rate = 500  # Fallback reasonable ATO rate` (line 576)

**`app.py` silently fabricates data when columns are missing.** `_standardise_combined`,
`_standardise_pre`, `_standardise_approval` (lines 107–226) insert `"Unknown"`, `0`, and
`2025-01-01` so the UI never breaks. `_find_col` (lines 65–77) matches any column whose name
*contains* a candidate substring — so `"amount"` can match the wrong column. In an audit tool,
a silent default is a fabricated record.

**Genuine bug:** `computation.py:390` — `same_day_splits if 'same_day_splits' in dir() else ...`
works by accident. Fix it when you port.

**Hardcoded values that violate the portability contract (§3.16).** `app.yaml` was fixed in P0;
these remain and are yours to remove in the phase that touches each file:
- `src/platform/adapters.py:71` — `_UPLOAD_BASE = "/Volumes/sdpt_gia/ep_temp/taxgovernance"`.
  Must come from `DBX_VOLUME`. Fix in **P5** when you implement the real upload.
- `src/platform/fixtures.py` — `sdpt_gia.*` table names, `data-engineering@optus.com.au` owners
  throughout the demo data. Fix in **P3** when fixtures are replaced by real Delta reads.
- `src/narrative.py:23` — `"You are an internal audit analyst for Optus."` This module is
  orphaned; it is kept only as reference for the T&E prompt content and the test-ID framework
  when you write the real prompts in **P6**. Do not import it. Delete it in P6.

A CI grep test for these patterns is part of P9. Until then, the scan is:
`grep -rniE 'dvlp_11|ia_dart|sdpt_gia|optus|adb-[0-9]' --include=*.py --include=*.yaml .`

**Wrong docstring:** `src/eval_gate.py` claims the pipeline uses
`narrative.generate_findings()` + `guardrail.verify_all_findings()` "directly from app.py".
It does not. That file is deleted in P0.

### 0.3 The exposure number on the executive brief is wrong

`app.py:1625` shows "Potential financial exposure of $X" where X is
`sum(financial_exposure)` across findings, and each finding's exposure is
`max(amount over cited metrics)` (`app.py:919`). Populations overlap — the same $6,000 claim
is counted in `hv_amount`, `daily_over_amount` and possibly `missing_receipt_amount`.
The risk score `sev*30 + recurrence*0.4 + exposure_pct*0.3` (`app.py:926`) has no stated basis.
Both are fixed in P3.

---

## 1. Product vision

One governed application serving multiple audit use cases through:

- Reusable **Skills** — versioned audit methodologies, mostly declarative
- **Playbook Mode** (run a saved Skill) and **Explorer Mode** (author a new one)
- Governed data discovery from Unity Catalog plus business file upload
- A deterministic pipeline with LLM narration at bounded points
- Evidence-linked findings with source-file traceability
- Persistent runs and management actions in Delta
- Excel / PowerPoint / HTML exports
- Jira integration behind an explicit approval step (deferred — not built now)

The UX is identical regardless of Skill. The Skill supplies sources, tests, thresholds,
prompts and the workspace layout. T&E ExCo is SKILL-001; T4.8 Input GST is SKILL-002.

---

## 2. Architecture

### 2.1 Runtime — three components, one channel

```
Databricks App (Dash)  ──run_now(run_id, phase)──▶  Job run (serverless)
  • UI, routing, forms                                • the pipeline loop
  • start_audit_run                                   • nodes, Skill tests, LLM calls
  • polls Delta, renders                              • exports to Volume
  • plan confirm / findings sign-off
        │ read                                                  │ write
        ▼                                                       ▼
   Delta: runs · run_state · findings · management_actions · trace_events
          uploaded_files · llm_calls · llm_cache · narrative_edits · evaluation_runs
   Volume: uploads · exports      MLflow: per-node spans      Model Serving: 2 endpoints
```

**The App never runs a node. The Job never renders anything. They communicate only through Delta.**

The App does run Python — Dash callbacks, chart filtering, building export bytes on download.
The rule: *nothing that takes more than a couple of seconds, and nothing that produces audit
evidence, runs in the App.* Evidence is produced by a job run with a run number, or it is not evidence.

### 2.2 No orchestration framework

**Do not use LangGraph, Prefect, Dagster, or any agent framework.** v1 mandated LangGraph; that
was wrong given this runtime. Databricks Jobs is the durable executor, Delta is the state store,
MLflow is the tracer. The pipeline is a plain Python loop:

```python
state = persistence.load_state(run_id)
for node in NODES_FOR_PHASE[phase][state.next_node_index:]:
    state = node(skill, state)
    persistence.save_state(state)
    if state.status in ("awaiting_confirmation", "awaiting_signoff"):
        return
```

Nine nodes, linear, no cycles, no conditional routing, no fan-out. A framework would add a second
persistence layer that fights Delta, 40+ transitive dependencies, and nothing else.

If a future phase needs a genuine loop (an interactive Explorer refinement, a multi-round critic),
introduce a framework **for that node only**, and ask first.

### 2.3 Executor abstraction

`Executor.start(run_id, phase)` with two implementations:

- `ThreadExecutor` — `ThreadPoolExecutor`, semaphore(2), for local dev and pytest.
- `JobsExecutor` — `w.jobs.run_now(job_id, job_parameters={"run_id":…, "phase":…})`, for deployed.

The pipeline function is identical under both. Selected by env var. This is what lets you build
and test the whole engine with no workspace access, and it is the fallback if Job permissions
are blocked in the target environment.

**Three rules that make either executor safe:**
1. Every node is idempotent — re-running it for the same `run_id` overwrites its own outputs, never appends.
2. A reaper runs on App start: any run still `running` was orphaned by a restart → mark `interrupted`, offer resume.
3. Concurrency is capped; further runs sit in Delta as `queued`.

### 2.4 Human-in-the-loop gates — process boundaries, not interrupts

| Phase parameter | Nodes | Ends with |
|---|---|---|
| `plan` | discover → profile → plan | `awaiting_confirmation` |
| `execute` | execute → classify → find → prioritise → act | `awaiting_signoff` |
| `export` | export | `completed` |

- Plan confirmation: **mandatory in Explorer**, optional in Playbook.
- **Findings sign-off before export: mandatory in both modes.** This is the control that matters
  for a defensible workpaper — a human signs off on what leaves the system. v1 only had the plan
  gate; that was the wrong gate.
- Sign-off records approver identity and timestamp in `trace_events`.

---

## 3. Non-negotiables

Change none of these without stopping and proposing to the user first.

1. **No orchestration framework for the core pipeline.** Plain Python loop (§2.2).
2. **Deterministic computation, LLM narration.** Every number — amounts, counts, ratios,
   thresholds, categories — comes from Python computing on data. LLMs write prose *around*
   numbers the node fills into template slots. `execute` never calls an LLM.
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
8. **Reproducibility comes from the Delta response cache,** not from `temperature`. Cache key:
   `(prompt_sha256, endpoint, served_model_version, params_json)`. Sampling parameters are
   per-endpoint config, tested, never assumed.
9. **Model routing is config-driven.** Two endpoints (§6). No model names in node code.
10. **Every metric carries source-file provenance** — `{value, unit, source_file}`, per the
    existing `EVIDENCE_PAYLOAD["metrics"]` pattern.
11. **No chain-of-thought exposed.** Trace shows execution events only — timestamps, stages,
    statuses, durations. Reasoning (including any `reasoning_content` from GPT-OSS) is logged
    to `llm_calls` and never rendered.
12. **Narrative traceability.** Every LLM paragraph in the UI shows which `RunState` fields
    supplied its numbers.
13. **No fake successful integrations.** UC search fails → demo indicator. Jira not connected →
    "Preview — not submitted". Endpoint misconfigured → deterministic output only, labelled
    "LLM unavailable". Never silently fabricate.
14. **No silent defaults for missing data.** Contract violation → run fails. `_find_col` and the
    `_standardise_*` defaulting behaviour are deleted, not ported.
15. **`/workspace/tne` stays functionally identical on the same data.** LLM narration is additive.
    Note "functionally", not "bit-for-bit": v1 said bit-for-bit, which would have fossilised the
    fabricated constants in §0.2.
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
├── thresholds.yaml    # every threshold, with policy reference + effective date
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

Promotion `draft → published` requires Surface 2 precision/recall on planted synthetic data plus
a named reviewer.

**Do not build:** a tool-using planner, a planner that emits SQL or pandas, a multi-agent swarm,
a supervisor pattern, a planner/skeptic pair, or a conversational companion agent. If a phase
seems to need one, stop and ask — the answer is usually a better prompt or a rule change.

---

## 5. Evaluation

Tiered. Build Tier A now; do not attempt all sixteen gates at once.

### Tier A — in CI from the phase that introduces them

| Gate | Asserts | Phase |
|---|---|---|
| **G6 Reconciliation** | tested-population rows, Σamount, min/max date == source totals; variance 0; persisted per run | P3 |
| **G7 Contract conformance** | contract violation → `status=failed`. No fuzzy column matching, no defaults | P3 |
| **G8 Threshold consistency** | catalogue thresholds == `thresholds.yaml` == code; every threshold has a policy ref | P2 |
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
  human). **Not** parity against legacy `app.py` — that would enshrine the flag-counting in §0.2.
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
DBX_CATALOG  DBX_SCHEMA  DBX_VOLUME  DBX_WAREHOUSE_HTTP_PATH  DBX_APP_NAME  DBX_JOB_ID
MODEL_ENDPOINT_HOST  MODEL_SONNET  MODEL_GPT_OSS
EXECUTOR=thread|jobs   DEMO_MODE=true|false
```

**Six adapter Protocols** in `orchestrator/adapters/`: `DataSourceAdapter`, `ModelClient`,
`PersistenceAdapter`, `PromptRepository`, `TracingAdapter`, `ExportStorageAdapter`.
`DataSourceAdapter` must expose "give me the tested population" without the caller knowing whether
pandas or Spark produced it — if a dataset later exceeds container memory, only that adapter changes.

**Data access:** attach a serverless SQL warehouse as an App resource (this also grants the App's
service principal access); read/write Delta through `databricks-sql-connector`; Volumes through the
Files API.

**Environment restrictions:** MCP is not permitted in the target corporate environment — do not add
MCP dependencies. Databricks Apps cannot compile C extensions at deploy time; if a dependency fails
to install, vendor a `manylinux2014_x86_64` wheel and reference it explicitly.

---

## 8. Phases

Each phase ends with **stop and report**. Do not auto-start the next.

| Phase | Deliverable | Definition of done |
|---|---|---|
| **P0** | Foundation reset | Done — this file, `.claude/` config, `.env.example`, `.gitattributes`, dead code removed |
| **P1** | `RunState` + Delta persistence | JSON round-trip test green; migrations create all tables; `LocalPersistence` + `DeltaPersistence` both pass the same contract test; reaper test green |
| **P2** | Primitives + T&E Skill | Surface 2 ≥0.98/≥0.95 per test on planted data; G8 green; **zero memorised constants** (grep test); contract describes raw sources |
| **P3** | Pipeline loop + nodes + ThreadExecutor | G6, G7, G9, G10 green; full run on fixtures → findings in Delta; `/trace` shows real events; MLflow per-node spans; exposure double-count fixed |
| **P4** | App rewired to run-scoped data | Surface 1 vs hand-verified oracle; G13 green; no module-level globals; `/workspace/tne` functionally identical; all routes unchanged |
| **P5** | JobsExecutor + UC + Volume upload | `run_now` round-trip works; a run survives an App redeploy (reaper + resume); real file uploads and profiles; deployment ID reported |
| **P6** | LLM layer | G11 100% on a 30-case golden set; G14, G15 green; degraded mode works; per-endpoint parameter matrix recorded in §6; `classify` via `ai_query` |
| **P7** | HITL gates | Paused run resumes across browser refresh **and** App restart; export blocked until sign-off; both events in `trace_events` |
| **P8** | GST Skill + Explorer | GST end-to-end on synthetic data via primitives (no bespoke test functions); Explorer proposes → validates → confirms → runs → saves draft Skill; planner cannot emit code (schema test) |
| **P9** | Eval harness + hardening | Tier A in CI; nightly green 3 consecutive nights; judge κ recorded; migration checklist written |

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

**`build_demo_data` (`app.py:229–287`) is deleted in P3.** Random `RF_*` flags are noise, not
synthetic data. Demo mode becomes "a completed run over a realistic dataset, persisted in Delta
like any other run, clearly labelled".

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

Opus kickoff is warranted for P1, P2, P6, P8 (design-only, then hand off).
Sonnet kickoff is sufficient for P3, P4, P5, P7, P9.

---

## 11. Day one — verify before building

Run this first, on Sonnet, before any design work. Record the results in §6 of this file.

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
print(w.current_user.me().user_name)                    # host + token + network policy
print([e.name for e in w.serving_endpoints.list()])     # the two endpoints
print([c.name for c in w.catalogs.list()])              # Unity Catalog access
print([wh.name for wh in w.warehouses.list()])          # SQL warehouse
```

Then confirm, in the workspace UI, that all three platform capabilities exist:
**Databricks Apps**, **serverless Jobs**, **pay-per-token model serving**. The architecture
assumes all three. If any is missing, **stop and report** — it changes the plan, not the code.

Then: one hello-world `jobs.run_now` round-trip, and one chat completion against each endpoint
recording which parameters pass through.

### Known environment issues

- **`databricks-sdk` may fail to import** with `pyo3_runtime.PanicException` from
  `cryptography` via `google.auth`. The system `cryptography` in `/usr/lib/python3/dist-packages`
  is broken. Fix with a venv or a pinned `cryptography` upgrade in `/usr/local`. A SessionStart
  hook should handle this.
- **Proxy diagnostics:** if outbound calls fail, `curl -sS "$HTTPS_PROXY/__agentproxy/status"`
  lists recent denials by host. A `403` on CONNECT is a network-policy denial; a `502` with
  `injection failed` is a misconfigured managed connector. Both are environment settings —
  report them, never work around them, and never disable TLS verification or unset `HTTPS_PROXY`.

---

## 12. What to do first

1. Run §11. Stop and report if anything fails.
2. Read the files in §0, in full.
3. Confirm you understand §0.2 — the flag-counting problem, the orphaned modules, the memorised
   constants. State it back in your own words.
4. Produce a **one-page plan for P1 only**: file list, `RunState` field-by-field justification,
   Delta DDL outline, test approach, risks.
5. Stop. Do not start P2 until P1 is reviewed.
