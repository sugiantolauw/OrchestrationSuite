# Capability matrix — development workspace vs corporate target

Required by CLAUDE.md §9C ("Free-to-Optus capability matrix": created in P1A, validated in P5).
Every row is something the build depends on. **Verified** means observed by a call from this
project, with the date; anything else is unverified and must not be assumed.

Development workspace: the Databricks-managed workspace in the gitignored `.env` (the first AWS workspace, whose IAM trust was broken, was abandoned on 2026-09-23).
Corporate target: filled from the corporate-workspace assessment (CLAUDE.md §11 "Corporate workspace
assessment", 2026-09-24) and the user decisions recorded there. Deliberately generic wording —
**no corporate workspace name, catalog, schema, volume, endpoint name or id appears in this file**
(CLAUDE.md non-negotiable 16); those values live only in the gitignored corporate `.env`. Status
key: **Confirmed** (the assessment found it present/working), **Limited** (present but with a
material restriction noted), **Not available** (found denied/absent), **Still to verify** (not yet
checked there).

| # | Capability | Needed from | Development workspace | Corporate target | Notes |
|---|---|---|---|---|---|
| 1 | Databricks Apps (create + deploy) | P3 (restart test), P5 | **Verified** 2026-09-23 — created in 139 s (compute MEDIUM), deployed in 9 s | **Confirmed** | Deployment stays Databricks SDK-based (`scripts/deploy_app.py`); Asset Bundles optional/unconfirmed there |
| 2 | App container CPU / memory limits | P3 | **Verified** 2026-09-23 — 4 vCPU, 15.6 GB RAM (MEDIUM); no cgroup limits visible below that | **Still to verify** | Revisit if compute size changes |
| 3 | Container recycling / scale-to-zero behaviour | P3 | **Observed** 2026-09-23 — one container, one pid for 53 min with zero HTTP traffic: no recycling, no idle scale-down. Explicit stop→start took ~140 s, and the new container reached `app.py` (reaper ran) ~56 s after the SDK reported "started" | **Still to verify** | A redeploy/restart kills in-flight runs; reaper + Resume verified live (RUN-F7DFE14191D7) here |
| 4 | Background threads survive a multi-minute run | P3 | **Verified** 2026-09-23 — daemon thread wrote 102 heartbeats over 53 min, max gap 33 s | **Still to verify** | |
| 5 | HTTP / gateway timeout on callbacks | P3 | Unverified — first /workspace/tne load was ~38 s before row snapshots (now ~8–12 s measured from outside the App) with no timeout observed from the platform side | **Still to verify** | Upload and timeout limits named explicitly as unverified there |
| 6 | Serverless SQL warehouse | P1A (Delta tests) | **Verified** 2026-09-23 — cold start 8.5 s, warm 1.2 s; single-row writes 2–8 s | **Confirmed** | Shared, smallest size tier, 1-minute auto-stop |
| 7 | Warehouse attachable as App resource | P5 | **Verified** 2026-09-23 — `CAN_USE` resource; `valueFrom` injects the warehouse id; App SP writes Delta after `GRANT … TO \`<sp client id>\`` | **Confirmed** | An external review of the prototype there read the warehouse as "not wired as a resource" — that finding was about the prototype's own deploy, not a platform limit; this project's deploy script attaches it |
| 8 | Unity Catalog metadata | P1A | **Verified** 2026-09-23 — catalog `orchestrationsuite`, schema `audit_ledger` | **Confirmed** | A catalog/schema/Volume are available; a dedicated schema for the ledger is to be requested there (configuration, not code) |
| 9 | Unity Catalog managed storage (Delta writes) | P1A | **Verified** 2026-09-23 — Databricks-managed storage | **Confirmed** | |
| 10 | Delta features: CHECK constraints, `appendOnly`, deletion vectors, row tracking | P1A | **Verified** 2026-09-23 — CHECK enforced; DV + row tracking accepted; CAS returns 1 then 0 affected rows | **Still to verify** | Named explicitly as unverified there |
| 11 | `DESCRIBE HISTORY` + `VERSION AS OF` on source tables | P3 / P5 | **Verified** 2026-09-23 — versions resolved at run creation and every read pinned with `VERSION AS OF`; UC reads equal the source files exactly (8/8 tables) | **Still to verify** | Applies to any source loaded into Delta there; the primary T&E sources are files instead (row 26) |
| 12 | Volumes (uploads/, exports/) | P5 | **Verified** 2026-09-23 — `orchestrationsuite.audit_ledger.files`: XLSX workpapers and per-run Parquet row snapshots written by the App SP and read back with matching sha256; `tne_source.raw` used for source loading | **Confirmed** | A Volume is available for uploads/exports; the `/dbfs` FUSE path does not apply there — this project uses the Files API, which does |
| 13 | Workspace files (App source upload) | P5 | **Verified** 2026-09-23 | **Confirmed** | An assistant with workspace access there can edit files but not execute code — ops (deploy/bootstrap/idle-cost check) must run from a cluster/notebook instead (row 27) |
| 14 | Pay-per-token model serving — Sonnet class | P6 | **Verified present** 2026-09-23 (`databricks-claude-sonnet-5`) | **Confirmed** | A READY Sonnet-class endpoint is present; access from the App's own service principal is still to verify; invocation and parameter matrix not yet run there either way |
| 15 | Pay-per-token model serving — GPT-OSS | P6 | **Verified present** 2026-09-23 (`databricks-gpt-oss-120b`, `-20b`) | **Confirmed** | GPT-OSS-class endpoints present; PII/safety guardrail endpoints also present there (not evaluated by this project yet) |
| 16 | Model region / cross-geo processing | P6 | Unverified | **Still to verify** | Data residency named explicitly as unverified there; §9A.6 — governance decision with lead time |
| 17 | `ai_query()` / `ai_classify()` / `ai_gen()` from SQL warehouse | P6 | Unverified | **Not available** | Denied on the corporate warehouse — `classify` (T4.3) calls Model Serving from Python in batches instead of `ai_query()` (independent review 2026-09-24 items 3–4, `orchestrator/llm/`); sending row text to a model needs its own governance approval there regardless (T4.3 stays `not_testable` until then) |
| 18 | AI Gateway + inference tables | P6 | Unverified | **Confirmed** | Inference tables present there (restricted access) |
| 19 | MLflow tracking (per-run, per-node spans) | P3 | **Verified** 2026-09-23 — deployed App, live run RUN-8486A15C5985: experiment `/Shared/ai-audit-analyst-audit-runs` (created by `scripts/deploy_app.py`, App SP granted `CAN_MANAGE`) holds 1 parent run tagged `orchestrator_run_id=<run_id>` (params `run_id`, `skill_id`, `skill_version`, `fingerprint_id`, `code_revision` all populated) plus 8 nested `mlflow.parentRunId`-linked runs, one per node (`discover, profile, plan, execute, classify, find, prioritise, act`), all `FINISHED`; matches the 8 `node_started`/`node_completed` pairs in `trace_events` for the same run, with zero "tracing unavailable" events | **Confirmed** | MLflow available under a Shared path there |
| 20 | `system.access.audit` readable | P3 | Unverified (`system` catalog visible) | **Confirmed** | Independent trail (§2.3) |
| 21 | `system.billing.usage` / compute system tables readable | P6 | Unverified | **Confirmed** | Billing and compute system tables present; `system.query.history` and `system.serving.endpoint_usage` are NOT (row 25) |
| 22 | Service principal for the App + on-behalf-of-user identity | P5 | App SP **verified** (OAuth M2M creds injected); on-behalf-of-user untested | **Still to verify** | Identity headers named explicitly as unverified there; §9A.1 |
| 23 | Secret scopes | P5 | Unverified | **Limited** | Present, but a new scope must be requested for this project there — not self-service |
| 24 | Outbound network from the build environment | P1A | **Verified** 2026-09-23 — workspace host reachable through the session proxy | **Still to verify** | Corporate CI will need its own route (§9A.3); no external egress (ServiceNow, Glean) is available there until requested (row 28) |
| 25 | `system.query.history` / `system.serving.endpoint_usage` readable | P6/P9 | Unverified | **Not available** | `scripts/check_idle_cost.py` uses `system.compute.warehouse_events` there instead (independent review 2026-09-24 item 2); LLM usage comes from inference tables + `llm_calls` |
| 26 | T&E source data shape (Delta tables vs Volume files) | P2/P5 | Delta tables loaded via `scripts/load_tne_sources_to_uc.py` | **Limited** | Sources there are Excel files in a Volume, not Delta tables — bound via `SOURCE_BINDINGS` (independent review 2026-09-24 item 1, `orchestrator/source_bindings.py`), read via the Files API, contract-validated and sha256-pinned like any other source. The Volume there has no per-diem-rate file; a missing configured source fails the run loudly, with no bundled fallback |
| 27 | Binary Python package installation at deploy time | P5/P9 | pip install from PyPI, compiled locally when needed | **Limited** | Apps there cannot compile C extensions at deploy time; binary dependencies need a vendored `manylinux2014_x86_64` wheelhouse (independent review 2026-09-24 item 6, `scripts/build_vendor_wheelhouse.py`); pure-Python packages install from PyPI (whether the Apps runtime there can reach PyPI at all is itself unconfirmed) |
| 28 | Ops execution model (deploy / bootstrap / idle-cost check) | P9 | Run locally, or from this session, against the workspace via PAT | **Limited** | An assistant with workspace access there can edit files but cannot execute code; ops scripts must run from a cluster/notebook with notebook authentication instead (independent review 2026-09-24 item 7 — `ops/notebooks/*.py`, `main(argv)` entry points, `--source-code-path` for a Git-folder-based deploy) |
| 29 | External egress (ServiceNow, Glean, other third-party APIs) | P7+ (deferred features) | Not used | **Not available** | No external egress is available there until explicitly requested; affects only features not yet built (issue-tracker integration, knowledge-source adapter) |
| 30 | Jobs (only if risk sensing is built) | Not planned | Not required | Not required | §2.5, §4.10 |

## Environment-specific values

All of these live in environment variables (`.env.example`), never in source (NN16):
`DATABRICKS_HOST`, `DBX_CATALOG`, `DBX_SCHEMA`, `DBX_VOLUME`, `DBX_WAREHOUSE_HTTP_PATH`,
`DBX_APP_NAME`, `MODEL_SONNET`, `MODEL_GPT_OSS`, `CODE_REVISION`, `SOURCE_BINDINGS`,
`DBX_APPS_PYTHON_VERSION`.

## Corporate assessment — still to verify there (2026-09-24)

Not yet confirmed one way or the other by the corporate-workspace assessment; treat each as
**unverified**, not as either present or absent, until checked directly:

- Model access from the App's own service principal (rows 14–15 confirm the endpoints exist;
  whether this project's App identity can call them is separate).
- The App compute size available there.
- Identity headers forwarded to the App (row 22 — on-behalf-of-user identity, §9A.1).
- Upload size and HTTP/gateway timeout limits (row 5).
- Delta constraint features beyond basic table creation (row 10).
- `VERSION AS OF` behaviour on whatever sources do land in Delta there (row 11).
- Data residency / region for model inference (row 16).
