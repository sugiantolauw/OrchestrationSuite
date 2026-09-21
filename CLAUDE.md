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
- flag cross-test patterns no single rule can see
- propose a severity *alongside* the computed one, with a reason

It may not add a finding outside the rule set, remove one, change a computed severity, or emit a
number. Themes are stored on `RunState.findings[].theme_id` plus a `themes` list, and the auditor
confirms them at sign-off.

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
| **P4** | App rewired to run-scoped data | Surface 1 vs hand-verified oracle; G13 green; no module-level globals; `/workspace/tne` functionally identical; all routes unchanged; PPTX overflow, zero-findings and private-API defects fixed (§4.7) |
| **P5** | JobsExecutor + UC + Volume upload | `run_now` round-trip works; a run survives an App redeploy (reaper + resume); real file uploads and profiles; deployment ID reported |
| **P6** | LLM layer + export rebuild | G11 100% on a 30-case golden set; G14, G15 green; degraded mode works; per-endpoint parameter matrix recorded in §6; `classify` via `ai_query`; PPTX restructured per §4.7 with bounded slide count, written exec summary, themes slide and run-id footer |
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

## 9A. Open questions — answer before the corporate migration, not after

These are unresolved and shape the schema or the deployment. Raise them early; several need a
platform owner's decision, not a code change. `docs/DATABRICKS_ARCHITECTURE.md` is the version of
this list written for a Databricks Solutions Architect.

1. **Whose identity runs the audit?** A Databricks App can run as its service principal, or on
   behalf of the signed-in user. This is the biggest open governance question:
   - As a service principal, every auditor sees whatever the SP can see. Unity Catalog row filters
     and column masks on executive expense data are bypassed, and `runs.run_owner` is a value the
     app asserts rather than an identity the platform verified.
   - On behalf of the user, UC permissions apply per auditor and the identity is real — but a
     serverless Job triggered by the app runs as the job's own identity, so the user context has
     to be carried into `RunState` and re-checked, or the job inherits the SP's reach anyway.
   Decide before P5. `RunState` must carry a verified `run_owner` either way.
2. **Can a run be retracted?** Findings leave the system as PPTX and XLSX. If a run is later found
   wrong, there is currently no way to mark it superseded, and no way to tell which exports came
   from it. Add `runs.superseded_by` and stamp every export with `run_id` + timestamp. Cheap now,
   very expensive later.
3. **Where does CI run?** Tier A gates need to execute somewhere that can reach a workspace for
   the Delta-backed tests. GitHub Actions will not reach a corporate Databricks workspace. Either
   the gates run against `LocalPersistence` only in CI (and the Delta path is covered by a nightly
   job inside the workspace), or CI moves into Databricks. Decide in P9; design `PersistenceAdapter`
   in P1 so the first option is possible.
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
8. **Secrets in the Job.** The App reads env vars, but a serverless Job should read a Databricks
   secret scope, not environment variables. Wire this in P5.

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
