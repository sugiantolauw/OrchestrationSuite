# AI Audit Analyst — Databricks Architecture

**Audience:** Databricks Solutions Architect / platform owner
**Purpose:** confirm the platform features this application depends on are available and
permissible in the target corporate workspace, and settle the open governance questions in §7.
**Status:** built and proven on Databricks Free Edition; not yet deployed to a corporate workspace.

---

## 1. What the application does

An Internal Audit analytics platform. An auditor selects a **Skill** (a versioned audit
methodology — e.g. Travel & Entertainment executive review, GST input-tax testing), points it at
governed tables or uploads a file, and runs it. The run executes a fixed sequence of deterministic
control tests, produces evidence-linked findings, and generates narrative prose around the computed
numbers using a model-serving endpoint. Output is a set of findings, management actions, and
PowerPoint / Excel workpapers.

Two design properties drive every platform choice:

- **Deterministic results.** Which exceptions exist, and every number, is computed by Python. The
  LLM writes the prose around those numbers and groups findings into themes. It never invents a
  finding, a figure or a severity. Two runs over the same data produce the same findings.
- **Everything is evidence.** Every run, finding, management action, execution event and model call
  is persisted to Delta with a run ID. The workpaper must be defensible to external audit.

---

## 2. Architecture

```
┌────────────────────────────┐      jobs.run_now(run_id, phase)      ┌───────────────────────────┐
│  Databricks App            │ ───────────────────────────────────▶  │  Job run (serverless)     │
│  Python / Dash             │                                       │                           │
│                            │                                       │  • pipeline, 9 nodes      │
│  • UI, routing             │                                       │  • deterministic tests    │
│  • starts runs             │                                       │  • model-serving calls    │
│  • polls Delta, renders    │                                       │  • writes exports         │
│  • HITL approval gates     │                                       │                           │
└─────────┬──────────────────┘                                       └────────────┬──────────────┘
          │ read (SQL warehouse)                                                  │ write
          ▼                                                                       ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│  Unity Catalog                                                                                │
│    Delta tables:  runs · run_state · findings · management_actions · trace_events            │
│                   uploaded_files · llm_calls · llm_cache · narrative_edits · evaluation_runs  │
│    Volume:        uploads/  exports/                                                          │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│  Mosaic AI Model Serving (pay-per-token FM APIs)  ·  AI Gateway + inference tables            │
│  MLflow experiment (per-run, per-node spans)      ·  system.billing / system.access.audit     │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

**The App never executes audit logic. The Job never renders UI. They communicate only through
Delta.** The App starts a job run and then reads state; a job run reads state, advances it, and
writes back. Nothing produces audit evidence except a job run, which carries a permanent run record.

**Long-running work is not in the web tier.** Dash callbacks are synchronous HTTP; an audit run
takes minutes. The App returns a `run_id` immediately and the UI polls.

**Human-in-the-loop gates are job boundaries, not in-process pauses.** A run is up to three job
runs: `plan` (stops for plan confirmation), `execute` (stops for findings sign-off), `export`.
State lives in Delta between them, so an approval can happen hours later, from a different browser,
after an app restart.

**No orchestration framework.** No LangGraph, Airflow, Prefect or Dagster. Databricks Jobs is the
durable executor; Delta is the state store; MLflow is the tracer. The pipeline is a plain Python
loop over nine linear nodes with no branching. This was a deliberate decision to avoid a second
state store competing with Delta.

---

## 3. Databricks features used

| # | Feature | Used for | Criticality | If unavailable |
|---|---|---|---|---|
| 1 | **Databricks Apps** (Python/Dash) | The entire front end | **Blocking** | No supported alternative in-platform. Would require hosting the web tier outside Databricks, which moves governed data out of the workspace. |
| 2 | **Serverless Jobs** — triggered via `jobs.run_now` with job parameters, not on a schedule | Pipeline execution | **High** | Falls back to an in-App `ThreadPoolExecutor` (already implemented behind an `Executor` interface). Loses durable run history, isolation and scale. Acceptable for a pilot, not for production. |
| 3 | **Serverless SQL warehouse**, attached as an App resource | All Delta reads/writes from the App via `databricks-sql-connector` | **Blocking** | — |
| 4 | **Unity Catalog** — one schema, ~10 managed Delta tables | System of record for runs and evidence | **Blocking** | — |
| 5 | **UC Volume** | File uploads from auditors; generated PPTX/XLSX | **High** | Could use workspace files, but loses UC governance on uploads. |
| 6 | **Mosaic AI Model Serving** — pay-per-token Foundation Model APIs | Narrative generation, classification, Explorer planning | **High** | Application runs in a degraded mode: all deterministic output present, narrative fields blank, UI labelled "LLM unavailable". Genuinely usable, materially less valuable. |
| 7 | **AI Gateway** with **inference tables** on the serving endpoints | Independent, platform-written request/response log; rate limits; PII guardrails | **High** (governance) | Application-written `llm_calls` table still exists, but the audit trail is then self-reported rather than independently captured. |
| 8 | **`ai_query()`** from SQL | Batch row-level classification (the one workload that scales with row count) | Medium | Falls back to a Python loop of HTTP calls — slower, more expensive, and rows leave Delta. Strongly preferred. |
| 9 | **MLflow experiment + tracing** | Per-run and per-node spans; evaluation harness | Medium | Trace events still land in Delta; loses the MLflow UI and the eval integration. |
| 10 | **System tables** — `system.billing.usage`, `system.access.audit` | Cost measurement; independent access audit | Low (build) / High (assurance) | — |
| 11 | **Databricks secret scopes** | Job-side credentials | Medium | — |

**Explicitly NOT used:** MCP servers (prohibited in the target environment), classic clusters,
Delta Live Tables, Databricks SQL dashboards, Genie, Lakebase, Model Serving custom models,
external data sources outside UC, any non-Databricks LLM provider.

### 3.1 Models

Two pay-per-token Foundation Model endpoints, selected by task, with names read from environment
variables (no model name appears in source):

| Workload | Endpoint tier | Volume per run |
|---|---|---|
| Finding narratives, executive summary, profile, remediation drafts, Explorer planning | Claude Sonnet class | ~40 short calls |
| Row-level expense classification (via `ai_query`), chart captions, schema-repair, grounding judge | GPT-OSS 120B class | 1 batch statement + ~8 short calls |

Reasoning traces returned by any endpoint are logged but never displayed.

### 3.2 Dependencies

Standard Python: `dash`, `pandas`, `numpy`, `plotly`, `databricks-sdk`, `databricks-sql-connector`,
`openai` (as the client for the OpenAI-compatible serving surface), `mlflow`, `python-pptx`,
`xlsxwriter`, `openpyxl`. Databricks Apps cannot compile C extensions at deploy time, so any
dependency needing compilation is vendored as a pre-built `manylinux2014_x86_64` wheel.

---

## 4. Data flow and residency

1. Auditor selects governed tables (Unity Catalog) and/or uploads a file to a UC Volume.
2. A job run reads the tested population through the SQL warehouse.
3. Tests run in-process (pandas) inside the job. **No audit data leaves Databricks.**
4. Model-serving calls send **computed aggregates and metric values** — not raw rows — for
   narrative generation. The one exception is row-level classification, which runs via `ai_query`
   so rows are processed inside the platform.
5. Findings, actions, events and model calls are written to Delta, partitioned by `run_id`.
6. Exports (PPTX/XLSX) are written to a UC Volume and downloaded by the auditor.

**Personal information.** Expense data names individuals, including executives. The Skill's data
contract classifies columns as PII; PII-classed columns never enter a prompt unless the Skill
explicitly whitelists them, and the whitelist is recorded per model call. AI Gateway PII guardrails
provide a platform-enforced backstop.

---

## 5. Governance and audit trail

| Concern | Mechanism |
|---|---|
| What ran, when, on what data | `runs` + `run_state` Delta tables, one row per run, plus the Databricks job-run record |
| Which tests ran and why | Skill version pinned per run; `plan.yaml` + `findings.yaml` versioned in git |
| Where every number came from | Every metric carries `{value, unit, source_file}`; population reconciliation (row count, sum, date range) asserted and persisted per run |
| Whether the LLM invented anything | Every number in generated prose must map to a metric that finding cites, unit-aware, checked automatically; failures fall back to a deterministic template and are flagged |
| Every model call | `llm_calls` (application-written, synchronous) **and** AI Gateway inference tables (platform-written) |
| Human edits to narrative | `narrative_edits` — who, when, before/after diff |
| Human approval | Plan confirmation and findings sign-off recorded with identity and timestamp; export is blocked until sign-off |
| Platform-level access | `system.access.audit` |

---

## 6. Cost profile

- **Compute:** serverless job runs of a few minutes, a handful of times per week per auditor.
  Serverless SQL warehouse for the App, auto-stopping.
- **Inference:** roughly 45 short calls per run (~1–2K tokens in, ~300 out) plus one batch
  classification. Dominated by the batch classification if row volume is high — which is why it
  runs on the cheaper endpoint via `ai_query` rather than per-row HTTP.
- **Measurement:** `system.billing.usage` and `system.serving.endpoint_usage`, filtered to the
  app's endpoints, after the first ten real runs. No cost tuning is done before that measurement.
- A workspace or budget policy cap is welcome; the application has no need to exceed it.

---

## 7. Questions for the Solutions Architect

**Blocking — needed before design is final:**

1. **Are Databricks Apps, serverless Jobs and pay-per-token Model Serving all enabled** in the
   target workspace, for this team?
2. **Identity model for the App.** Should it run as a service principal, or on behalf of the
   signed-in user? If on-behalf-of: how should user context propagate to a Job the App triggers,
   given the job run executes under its own identity? This determines whether Unity Catalog row
   filters and column masks on executive data are enforced per auditor or bypassed — it is our
   largest open governance question.
3. **May the App's identity trigger job runs** (`CAN_MANAGE_RUN`), and what is the approved pattern
   — a managed Job definition, or `jobs.submit()` one-time runs with no persistent job object?
4. **Model endpoint region.** Are the pay-per-token endpoints served in-region, or does inference
   cross geography? If cross-geo, what approval is required for expense data containing executive
   names?

**Important — needed before production:**

5. **Delta retention and `VACUUM` policy** for tables holding personal information. Time travel
   means deleted rows remain readable until vacuumed.
6. **AI Gateway:** may we enable inference tables on our endpoints? Where do they land, and who
   owns them? Are PII guardrails available and recommended?
7. **Rate limits / quotas** on the serving endpoints, and the expected behaviour of `ai_query`
   under throttling.
8. **Secret scope** for job-side credentials.
9. **Egress classification** for generated PPTX/XLSX containing individuals' names and spend.
10. **CI:** Tier-A regression gates need to run against a workspace. Is there an approved pattern
    for CI inside Databricks, or should the Delta-backed tests run as a nightly job while
    external CI covers only the local-persistence path?

**Nice to know:**

11. Is there an existing governed catalog/schema convention we should adopt rather than creating
    our own?
12. Any platform standard for Apps deployment (source-code path, CI/CD, rollback) we should follow?

---

## 8. What we are not asking for

To scope the conversation: no classic clusters, no GPU, no custom model deployment, no external
network egress from the App or Job, no MCP servers, no third-party LLM provider, no write access
to any existing production table. The application creates and writes its own schema, reads
governed tables read-only, and calls model-serving endpoints.

---

## 9. Current status

Built and running on Databricks Free Edition with synthetic data. Architecture and evaluation
design have been through independent technical review. The build plan is phased with explicit
gates; the full engineering brief is `CLAUDE.md` in this repository.
