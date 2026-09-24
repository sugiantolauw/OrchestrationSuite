# AI Audit Analyst — what we are building, and what it needs from the Databricks platform

**Purpose of this document.** This application is being built in a development Databricks
workspace and will be ported to a corporate Databricks workspace. This document describes, in
full, what the product is trying to achieve (today and in the long-term vision) and **every
capability it needs from the platform**, so that the corporate workspace can be checked against it
*before* the port. The goal is to find every gap early and design around it now, so that porting is
a configuration change, not a rebuild.

Status as of 2026-09-24. Branch `claude/bold-faraday-w9gb8y`. Authoritative build brief: `CLAUDE.md`.

---

## How to use this document (instructions for the reviewer, e.g. Genie Code)

You are reviewing this against **the corporate Databricks workspace you have access to**. For each
requirement in Section 5 (IDs like `APP-01`, `UC-03`), please:

1. **Check it in the workspace** — by inspecting settings, listing objects, reading workspace/account
   configuration you can see, or running a small harmless probe (e.g. list, describe, a `SELECT 1`).
   Do not create, modify or delete anything unless the item says a probe is safe.
2. **Answer with one status:**
   - `AVAILABLE` — works as described.
   - `AVAILABLE WITH LIMITS` — works, but with a restriction (state it exactly: size, quota, region,
     permission needed, approval process…).
   - `NOT AVAILABLE` — cannot be done in this workspace.
   - `NEEDS APPROVAL` — possible, but requires a request to a platform owner or security team (say who, if you know).
   - `UNKNOWN` — cannot be determined from inside the workspace (say who would know).
3. **Give evidence** — what you checked and what you saw (setting name, error message, API result).
4. **For anything not `AVAILABLE`,** suggest the closest workaround that exists in this workspace.

Please return your answer in the table format of **Section 9**, followed by a short list of the
items you consider blocking. Nothing in this document is secret; no credentials are included.

---

## Corporate assessment results (2026-09-24)

A first assessment of the corporate workspace against this document has been done (by an
assistant with workspace access, not a human) and its findings recorded. The full, row-by-row
result lives in **`docs/CAPABILITY_MATRIX.md`**'s "Corporate target" column; the raw findings and
the design decisions made in response to them are recorded in `CLAUDE.md` §11 ("Corporate
workspace assessment" and the user decisions immediately after it). This section is a short
pointer, not a duplicate — **no corporate workspace name, catalog, schema, volume, endpoint name
or id appears here or in `CAPABILITY_MATRIX.md`** (non-negotiable 16); those values live only in
the gitignored corporate `.env`.

**Confirmed available:** Databricks Apps; a serverless SQL warehouse (shared, smallest size tier,
1-minute auto-stop); a Unity Catalog catalog/schema/Volume; MLflow under a Shared path;
`system.access.audit`; billing and compute system tables; AI Gateway inference tables; Unity
Catalog tags; secret scopes (a new one must be requested); a ready Sonnet-class model-serving
endpoint and GPT-OSS-class endpoints; PII/safety guardrail endpoints.

**Findings that changed the design**, each already built (independent review 2026-09-24 items
1–7, this checkout):
- `ai_query()` / `ai_classify()` / `ai_gen()` are **denied** on the corporate warehouse — row-level
  LLM classification (T4.3) calls Model Serving from Python in capped batches instead
  (`orchestrator/llm/classify.py`), built but off by default pending governance approval.
- `system.query.history` and `system.serving.endpoint_usage` are **not available** — the idle-cost
  check uses `system.compute.warehouse_events` instead, and reports "not available" rather than
  failing when even that table cannot be read (`scripts/check_idle_cost.py`).
- The T&E source data there is **Excel files in a Volume, not Delta tables** — supported through an
  explicit per-environment source-binding configuration (`SOURCE_BINDINGS`,
  `orchestrator/source_bindings.py`), read via the Files API, contract-validated and sha256-pinned
  in the run fingerprint like any other source. A missing configured source (per-diem rates,
  named explicitly) fails the run loudly, with no bundled fallback.
- **Binary Python packages need vendored `manylinux` wheels** — Apps there cannot compile C
  extensions at deploy time (`scripts/build_vendor_wheelhouse.py`).
- **Operations must run from a workspace cluster/notebook**, not from a local/session shell — an
  assistant with workspace access there can edit files but not execute code
  (`ops/notebooks/*.py`, and `main(argv)` entry points on `deploy_app.py` / `setup_workspace.py` /
  `check_idle_cost.py`; `deploy_app.py --source-code-path` supports deploying from a Git-folder
  checkout instead of uploading a bundle).
- No external egress (ServiceNow, Glean, other third-party APIs) is available there until
  explicitly requested — affects only features not yet built.

**Still to verify there** (see `docs/CAPABILITY_MATRIX.md`'s closing section for the full list):
model access from this project's own App identity, the App's compute size, identity headers
forwarded to the App, upload/timeout limits, Delta constraint features beyond table creation,
`VERSION AS OF` behaviour, and data residency for model inference.

---

## 1. What we are trying to achieve

### 1.1 The product in one paragraph

An **internal audit analytics platform that runs inside Databricks**. An auditor picks governed
data (Unity Catalog tables) or uploads files, chooses an audit methodology (a **Skill**) or
describes a new audit objective, and the platform tests **100% of the population** — every
transaction, not a sample — producing evidence-linked findings, a reconciliation back to source, an
Excel workpaper and a PowerPoint pack. Every number is computed deterministically by code from the
data; AI (LLMs) is used only to *author* methodologies ahead of time (reviewed by a human) and to
*write prose around numbers the code already fixed*. A human confirms the plan and signs off the
findings before anything leaves the system. Every run is reproducible and auditable: exact source
table versions, file hashes, code revision, Skill content hash and configuration are recorded, and
the whole run history lives in Delta.

### 1.2 The long-term vision — the full audit lifecycle

The target is the capability set of a commercial "internal audit orchestration suite", organised
around the audit lifecycle rather than a single analysis:

```
Risk sensing ─▶ Risk assessment ─▶ Audit planning ─▶ Fieldwork ─▶ Reporting & issues
 (agents read      (cited risks,       (RCM, scope       (full-population    (issue drafting,
  company           impact scoring,     memo, test        testing — BUILT     ServiceNow, audit
  knowledge)        human accepts)      steps, request    TODAY)              committee report)
                                        list)
        ▲                                                        │
        └──────────── continuous monitoring (scheduled Skill runs, KRI alerts) ◀──┘
```

The data model already carries the whole chain **Risk → Control → Test → Finding → Issue →
Action**, so later modules plug in without migrations.

### 1.3 What exists today (built and deployed in the development workspace)

- A **Databricks App** (Python / Dash) — the whole application, including a background executor that
  runs audit pipelines inside the App.
- **SKILL-001: Travel & Entertainment (ExCo)** — 14 control tests, expressed as data (YAML), run by
  a generic engine of 8 reusable test primitives.
- A **run ledger in Delta** (Unity Catalog managed tables) with optimistic concurrency, leases,
  orphan recovery, and an immutable per-run fingerprint.
- **Unity Catalog discovery** of source tables, **file upload** to a UC Volume with profiling.
- **Two human gates**: plan confirmation and findings sign-off.
- **Exports**: Excel workpaper (built); PowerPoint pack (being built now).
- **MLflow** tracing: one MLflow run per audit run, one nested run per pipeline step.
- A test suite of ~900 automated tests (runs without any workspace), plus live Delta tests and
  browser end-to-end tests.

### 1.4 What is planned (approved roadmap, in order)

1. Explorer Mode — an LLM proposes a new audit plan (composed from the 8 primitives) from an
   objective and a *data profile* (aggregates only, never rows); the auditor confirms; it can be
   saved as a new draft Skill.
2. Clickable mockups of the lifecycle modules (for approval before any build).
3. Research foundation — long-running jobs, a knowledge-source connector (**Glean**), a bounded
   multi-agent research package with citations.
4. Risk Assessment v1 — agent-proposed, cited risks with impact analysis; nothing accepted without
   an auditor.
5. Audit Planning v1 — RCM, scope memo, test steps, document request list from agreed templates.
6. Reporting & **ServiceNow** integration — issue drafting pushed to ServiceNow after human approval
   (ServiceNow owns issue tracking; the app does not build a tracker).
7. Continuous monitoring — scheduled Skill runs with KRI alerts.
8. Control design assessment.
9. Evidence & workpapers (document evidence, request-list tracking).
10. A **porting kit** (pre-flight checker, bootstrap, runbook, GitLab CI) — built after acceptance testing.

---

## 2. Architecture (as built)

```
┌────────────────────────── Databricks App (Python/Dash) ──────────────────────────┐
│ web tier: UI, routing, forms, gates    │  executor: in-process thread pool (max 2) │
│ reads Delta to render                  │  runs pipeline steps, writes Delta+Volume │
└───────────────┬────────────────────────┴───────────────┬─────────────────────────┘
                │ databricks-sql-connector (SQL)          │ Files API, MLflow, Model Serving
                ▼                                         ▼
  Serverless SQL warehouse (App resource)       UC Volume (uploads/, exports/, snapshots)
  Unity Catalog: <catalog>.<schema> ledger      MLflow experiment (/Shared/…)
  tables + governed source tables               Model Serving endpoints (pay-per-token)
```

Key design choices that shape platform needs:

- **The App is the whole application.** No Jobs are used for fieldwork; the pipeline runs on a
  background thread inside the App. (Jobs are reserved for long-running research/monitoring later.)
- **Delta is the system of record.** All state goes through the SQL warehouse; the App keeps nothing
  important in memory.
- **The App runs as its service principal** and records the signed-in user (from the forwarded
  identity header) as the run owner. Whether it should instead run *on behalf of the user* is an open
  governance question (Section 7).
- **Everything workspace-specific is an environment variable** (catalog, schema, volume, warehouse,
  endpoints, app name). No names are hard-coded in source.

---

## 3. What the pipeline does at run time (so the reviewer can judge load and permissions)

1. **discover** — resolves the chosen tables/files; records each UC table's current version
   (`DESCRIBE HISTORY`) and each upload's SHA-256 in the run fingerprint.
2. **profile** — row counts, null rates, cardinality (aggregates).
3. **plan** — the Skill's test plan (or, in Explorer, an LLM proposal); optional human confirmation.
4. **execute** — reads each source population **pinned with `VERSION AS OF`**, runs the tests in
   pandas inside the App, writes metrics, flagged rows and Parquet snapshots. Never calls an LLM.
   Fails loudly if a population exceeds a configured cell ceiling (default 20M cells).
5. **classify / find / prioritise / act** — findings from declared rules, severity, amount at risk,
   draft management actions. (LLM narration will be added here, prose only.)
6. **human sign-off**
7. **export** — Excel workpaper (and PowerPoint) written to the Volume; downloads stream the file.

Typical load today: ~150K source rows across 8 tables; a run takes ~5–7 minutes; a few runs per week
per auditor; at most 2 concurrent runs per App.

---

## 4. Lessons from the development workspace (already fixed, relevant to the port)

- **Idle cost:** an early build polled Delta ~3,000 times/hour even when idle, keeping the serverless
  warehouse running 24/7 (~230 DBU/day). Fixed: the App now queries the warehouse only when a user
  acts or a run is active; warehouse auto-stop is set to 1 minute. The corporate workspace should
  confirm auto-stop can be set that low and whether idle warehouses are billed.
- **Model access:** in the development workspace all proprietary model endpoints (Claude, GPT-5/6,
  Gemini) return `403 … rate limit of 0`; only open-weights models (GPT-OSS, Llama, Qwen) answer.
  The app is configured to use GPT-OSS for development and switch back by configuration.
- **App URL access:** the App URL requires interactive OAuth; a personal access token cannot call
  it. Automated browser tests therefore need a service-principal/OAuth route or run against a local
  instance.

---

## 5. Platform requirements (please assess each)

Legend for "Needed": **NOW** = the built product depends on it; **ROADMAP n** = needed at roadmap
step n (Section 1.4); **PORT** = needed to deploy/operate/port.

### 5.1 Databricks Apps

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback if missing |
|---|---|---|---|---|---|
| APP-01 | Databricks Apps enabled; we can create and deploy an App | The entire application | NOW | Verified | None — blocking |
| APP-02 | Deploy via the Databricks Python SDK (`w.apps.create`, `w.apps.deploy`, workspace file upload of the source bundle) from a developer machine or CI | `scripts/deploy_app.py` | PORT | Verified | Manual deploy from the UI |
| APP-03 | App compute size **MEDIUM** or larger (~4 vCPU / ~16 GB) | Pipeline runs in-process with pandas | NOW | Verified MEDIUM = 4 vCPU / 15.6 GB | Smaller size forces more SQL pushdown |
| APP-04 | Background threads survive for the full length of a run (≥15 min) and the container is not recycled when idle | In-App executor | NOW | Verified: 53 min, no recycling | Move execution to Jobs (bigger change) |
| APP-05 | App start command `python app.py` (single process) | Dash server + executor | NOW | Verified | — |
| APP-06 | App resource: a SQL warehouse attached with `CAN_USE` (warehouse id injected via `valueFrom`) | All Delta access | NOW | Verified | Warehouse id via env var |
| APP-07 | The App's **service principal** can be granted Unity Catalog privileges (`USE CATALOG`, `USE SCHEMA`, `SELECT`, `MODIFY`, `CREATE TABLE`, `READ/WRITE VOLUME`) | Ledger writes, source reads | NOW | Verified (`GRANT … TO \`<sp client id>\``) | Grants done by a platform admin |
| APP-08 | Signed-in user identity forwarded to the App (`X-Forwarded-Email` / `X-Forwarded-User` headers) | Run owner, sign-off approver | NOW | Verified | None — required for audit trail |
| APP-09 | **On-behalf-of-user** authorization for Apps (user token usable by the App) | So Unity Catalog row filters/masks and Glean permissions apply per auditor | ROADMAP 3 (and governance decision) | Untested | Service principal only (governance risk) |
| APP-10 | App can install Python packages at deploy time from PyPI **or** an internal mirror (Artifactory), or accept vendored wheels (`manylinux2014_x86_64`) | Dependencies (Section 6) | NOW | Verified (PyPI) | Offline wheelhouse in the bundle |
| APP-11 | Request/upload size limit for the App (HTTP body) ≥ 100 MB | File uploads through the UI | NOW | Unverified | Upload directly to the Volume |
| APP-12 | HTTP/gateway timeout for App requests ≥ 30 s | Page loads of large runs | NOW | Unverified (loads take 8–12 s) | Smaller pages |
| APP-13 | App outbound network access to: the workspace itself; later ServiceNow REST API, Glean (MCP or REST) | Integrations | ROADMAP 3, 6 | Workspace only | Proxy/allow-list request |
| APP-14 | Programmatic access to the App URL for automated tests (OAuth M2M / service principal with `CAN_USE` on the App) | Post-deploy browser tests | PORT | Untested (PAT is redirected to login) | Test a local instance + verify via Delta |
| APP-15 | App restart / redeploy behaviour: how long, and whether deploys can be scheduled | Runs in flight are interrupted and resumed | NOW | Verified: stop→start ~140 s | — |

### 5.2 SQL warehouse

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| SQL-01 | A **serverless** SQL warehouse usable by the App | Every read/write | NOW | Verified (cold start 8.5 s) | Pro warehouse (slower start, higher idle cost) |
| SQL-02 | Warehouse **auto-stop = 1 minute** (or the lowest allowed) | Cost control | NOW | Verified: accepted 1 min via API | Longer auto-stop = higher idle cost |
| SQL-03 | `databricks-sql-connector` 4.5.0 with named parameters and typed binding (`DoubleParameter`), OAuth via the SDK's credentials provider | Persistence layer | NOW | Verified | — |
| SQL-04 | Statement result sizes: reading populations of ~100K rows × ~30 columns through the connector | execute step | NOW | Verified | Push aggregation into SQL |
| SQL-05 | `ai_query()` available on the warehouse | Future batch classification (T4.3) | ROADMAP (P6) | Unverified | Row batches through Model Serving |
| SQL-06 | Who pays for / can size the warehouse; any policy on dedicated vs shared warehouses | Cost and isolation | PORT | — | Shared warehouse |

### 5.3 Unity Catalog and Delta

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| UC-01 | A catalog + schema we can create tables in (or one provided to us) | Ledger: runs, run_state, run_fingerprints, node_attempts, trace_events, run_leases, engagements, findings, issues, management_actions, skill_versions, risks, controls, risk_assessments, review_notes, uploaded_files, run_metrics (+ later llm_calls, llm_cache, narrative_edits, evaluation_runs) | NOW | Verified (managed storage) | Platform creates tables from our DDL |
| UC-02 | Delta features on those tables: **CHECK constraints**, **deletion vectors**, **row tracking**, `delta.appendOnly` table property, **column mapping mode 'name'**, `MERGE`, conditional `UPDATE … WHERE state_version = ?` returning affected-row counts | Optimistic concurrency, immutability, idempotency | NOW | Verified | Weaker guarantees (not recommended) |
| UC-03 | `DESCRIBE HISTORY` and **`VERSION AS OF`** time travel on the *source* tables we read, with retention ≥ the time a run may be resumed or reproduced (days to weeks) | Pinned, reproducible reads | NOW | Verified | Snapshot source into our schema at run start |
| UC-04 | Read access (`SELECT`) for the App service principal on the governed **source** tables/views (T&E: 8 sources) | Audit populations | NOW | Verified | — |
| UC-05 | Source data can be exposed as views that satisfy the Skill's **data contract** (exact column names/types; see Section 8) | Contract validation fails the run loudly on mismatch | NOW | Verified with synthetic data | Column mapping at run setup (roadmap item) |
| UC-06 | Listing catalogs/schemas/tables and table metadata (owner, updated time, comment, tags) via the SDK, with no-access items returning a permission error | Data discovery UI ("Restricted" badge) | NOW | Verified | Fixed list of tables |
| UC-07 | Unity Catalog **tags** (e.g. PII classification) readable via SDK/SQL | PII masking for LLM prompts | ROADMAP 1 | Unverified | Classification declared in the Skill contract |
| UC-08 | **Row filters / column masks** on executive expense data — are they used, and must they apply per auditor? | Governance; affects APP-09 | Governance | n/a | — |
| UC-09 | Data retention / `VACUUM` policy for Delta history containing personal information | Legal/retention | PORT (governance) | n/a | Platform policy |
| UC-10 | Predictive optimisation / auto maintenance allowed on our schema | Housekeeping | PORT | Verified (running) | Manual OPTIMIZE/VACUUM |

### 5.4 Volumes and files

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| VOL-01 | A UC **Volume** in our schema, read/write for the App SP | uploads/, exports/, per-run Parquet snapshots | NOW | Verified | — |
| VOL-02 | Files API upload/download from the App (`w.files.upload/download`) with SHA-256 verification | Uploads, Excel/PPTX exports, snapshots | NOW | Verified | — |
| VOL-03 | File size limits for Volume uploads (≥ 100 MB per file) | Business-provided files | NOW | Unverified | Split files |
| VOL-04 | Any DLP/classification scanning on files written to Volumes, or on downloads of exports (Excel/PPTX name executives and their spend) | Export egress control | Governance | n/a | Classification label on exports |

### 5.5 Model Serving and AI

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| AI-01 | Pay-per-token Foundation Model endpoints: a **Claude Sonnet-class** model (planning, narratives, executive summary) | Explorer planner; narration | ROADMAP 1 | Present but **disabled (403, rate limit 0)** | GPT-OSS by configuration |
| AI-02 | A **GPT-OSS-class** open model (`databricks-gpt-oss-120b`) | Repair round, captions, classification, judges | ROADMAP 1 | Verified | — |
| AI-03 | Endpoint parameter support: `response_format` json_schema `strict`, `max_tokens`, `temperature`, `reasoning_effort` | Structured outputs | ROADMAP 1 | GPT-OSS verified; Sonnet untestable | Client-side validation + 1 retry |
| AI-04 | Access via `WorkspaceClient().serving_endpoints.get_open_ai_client()` from the App SP | Model client | ROADMAP 1 | Verified | — |
| AI-05 | **Data residency**: where the models are served; whether requests may leave the region (cross-geo) | Governance for personal data | Governance (before ROADMAP 1) | Unknown | Only in-region models |
| AI-06 | **AI Gateway**: usage tracking + **inference tables** (platform-written copy of every request/response), with restricted access | Independent audit trail of LLM calls | ROADMAP 1 | Usage tracking on; inference tables off | App-side `llm_calls` table only |
| AI-07 | Rate limits / quotas per endpoint and what happens when throttled | Batch classification | ROADMAP 1 | Unknown | Queue and retry; never partial results |
| AI-08 | Premium/high-effort model availability for long research (Opus/Fable-class) | Risk sensing | ROADMAP 3–4 | Disabled | Sonnet-class |

### 5.6 MLflow

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| ML-01 | Workspace MLflow tracking (`MLFLOW_TRACKING_URI=databricks`), experiment at an absolute path (e.g. `/Shared/<app>-audit-runs`) | Independent per-run trace | NOW | Verified | Experiment path supplied by admin |
| ML-02 | App SP can be granted `CAN_MANAGE` on that experiment | Writes runs | NOW | Verified | — |

### 5.7 System tables (independent evidence and cost control)

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| SYS-01 | Read access to `system.access.audit` | Independent log of every SQL statement the App issued (audit trail) | NOW (evidence) | Visible | — |
| SYS-02 | Read access to `system.query.history` | Idle-cost check after deploy | PORT | Verified | Warehouse monitoring UI |
| SYS-03 | Read access to `system.billing.usage` + `list_prices` | Cost measurement per run | PORT | Verified | Billing reports |
| SYS-04 | Read access to `system.serving.endpoint_usage` | LLM usage/cost | ROADMAP 1 | Verified | AI Gateway UI |

### 5.8 Jobs (not used today; needed for the roadmap)

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| JOB-01 | Serverless Jobs; the App SP can trigger `run_now` (`CAN_MANAGE_RUN`) | Long-running research, sensing | ROADMAP 3 | Unverified | Cannot run hours-long work in the App |
| JOB-02 | Scheduled jobs (cron) | Continuous monitoring (scheduled Skill runs) | ROADMAP 7 | Unverified | Manual runs |
| JOB-03 | Job compute gets its own UC grants; identity hand-off rules | Security | ROADMAP 3 | — | — |

### 5.9 Secrets, identity, groups

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| SEC-01 | Databricks **secret scopes** readable by the App (ServiceNow credentials, Glean credentials) | Integrations | ROADMAP 3, 6 | Unverified | Env vars (not recommended) |
| SEC-02 | Workspace groups for **preparer / reviewer / approver** roles (SCIM-synced) readable by the App | Segregation of duties at sign-off (replaces today's self sign-off) | ROADMAP (P7) | n/a | App-managed role table |
| SEC-03 | Personal access tokens allowed for developers, or OAuth U2M for the SDK/CLI | Deploy tooling | PORT | Verified (PAT) | OAuth U2M |

### 5.10 Integrations

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| INT-01 | **ServiceNow** (audit/IRM or ticketing module): REST API reachable from the App, a service account, which tables (issues, remediation tasks, engagements?) | Push approved issues; read status back | ROADMAP 6 | n/a | "Preview — not submitted" only |
| INT-02 | **Glean**: MCP endpoint (planned) or REST API; per-user permissions (on-behalf-of-user) | Company policies/processes as cited context | ROADMAP 3 | n/a | Policy documents dropped into a UC Volume |
| INT-03 | Is **MCP** permitted from Databricks Apps / Jobs (currently assumed *not*)? | Glean transport | ROADMAP 3 | n/a | Glean REST |

### 5.11 Source control, CI and developer tooling

| ID | Requirement | How we use it | Needed | Dev workspace | Fallback |
|---|---|---|---|---|---|
| DEV-01 | Code hosted in **GitLab**; Databricks Git folders connected to GitLab (optional) | Source of truth | PORT | GitHub today | Upload bundle from CI |
| DEV-02 | A GitLab CI runner that can run Python 3.11 tests (no workspace access needed for the core suite) | Tier A quality gates | PORT | n/a | Run tests locally |
| DEV-03 | Can any CI runner reach the workspace API (for live Delta tests and deploys)? | Live tests, deploy | PORT | n/a | Live tests as a Databricks Job inside the workspace |
| DEV-04 | Databricks SDK / CLI usable from developer machines (network, auth) | Deploy, bootstrap | PORT | Verified | Deploy from inside the workspace |
| DEV-05 | Genie Code (or another assistant) can edit/run this repo in the workspace | Continued development at Optus | PORT | n/a | — |
| DEV-06 | Headless Chromium/Playwright available to CI for browser tests | E2E tests | PORT | Verified in dev container | Skip browser layer |

---

## 6. Python dependencies (must install in the App)

Runtime (from `requirements.txt`; exact versions are locked in `requirements.lock`):
`dash==2.17.1`, `dash-bootstrap-components==1.6.0`, `plotly>=5.18`, `pandas>=2.1`, `numpy>=1.26`,
`pyyaml`, `python-dotenv`, `jsonschema`, `openai>=1.40`, `databricks-sdk`, `databricks-sql-connector==4.5.0`,
`mlflow>=2.16` (may switch to `mlflow-skinny`), `python-pptx`, `xlsxwriter`, `openpyxl`, `pillow`,
`lxml`, `pyarrow>=17`.
Dev/test only: `pytest`, `pytest-cov`, `playwright`.

Questions: can the App reach PyPI or an internal mirror? Are any of these packages blocked by
security policy? Are pre-built wheels needed (Apps cannot compile C extensions at deploy time)?

---

## 7. Governance decisions the corporate environment must make

These are not code; each changes how the platform must be configured.

1. **Whose identity runs the audit** — the App's service principal (today) or on behalf of each
   auditor (so UC row filters/masks and Glean permissions apply per person).
2. **Retention**: how long run records, uploads, exports, prompts/responses and Delta history are
   kept; when `VACUUM` removes deleted personal data.
3. **Export classification**: required label/watermark on Excel/PPTX exports; any DLP.
4. **Model region / cross-geo processing** for any data sent to models (only aggregates and policy
   text are ever sent; never transaction rows).
5. **Segregation of duties**: who may prepare, review and approve; enforced where (UC groups, App).
6. **Cost ownership** of the App, warehouse and model usage.
7. **Change control** for deploying the App and for publishing a Skill (a named reviewer is
   required to move a Skill from draft to published).

---

## 8. Data the first Skill expects (T&E ExCo) — for checking source availability

Eight sources, each exposed as a UC table or view with the exact columns in
`skills/tne_exco/contract.yaml`:
`expense_report`, `booking_detail`, `travel_request_segment`, `travel_requests_no_expense`,
`missing_receipt`, `attendee_validity`, `approval_aging`, `per_diem_rates`.
Plus reference data shipped with the Skill: ExCo member **employee IDs**, city→country and
currency→country lookups, preferred supplier lists, and an RBA exchange-rate snapshot (per-diem
rates are in SGD; reimbursement currency must be AUD or the run fails).

Questions: do equivalent governed tables/views exist (Concur/travel data)? Are the column names the
same? Is Employee ID available on every expense line? Are there row filters on these tables?

---

## 9. Response template (please fill in)

```
| ID     | Status                | Evidence / what you checked            | Limit or workaround |
|--------|-----------------------|----------------------------------------|---------------------|
| APP-01 | AVAILABLE             | …                                      | …                   |
| APP-02 |                       |                                        |                     |
| …      |                       |                                        |                     |
```

Then:

- **Blocking items** (cannot port without a decision or a change):
- **Items needing approval** (who approves, typical lead time if known):
- **Anything in this workspace that this document did not ask about but will affect the port**
  (e.g. network restrictions, cluster policies, workspace-level settings, compliance controls):
