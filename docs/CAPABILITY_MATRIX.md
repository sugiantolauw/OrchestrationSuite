# Capability matrix — development workspace vs corporate target

Required by CLAUDE.md §9C ("Free-to-Optus capability matrix": created in P1A, validated in P5).
Every row is something the build depends on. **Verified** means observed by a call from this
project, with the date; anything else is unverified and must not be assumed.

Development workspace: `DATABRICKS_HOST` in the session environment (AWS, `ap-southeast-2`).
Corporate target: to be filled by the platform owner before migration.

| # | Capability | Needed from | Development workspace | Corporate target | Notes |
|---|---|---|---|---|---|
| 1 | Databricks Apps (create + deploy) | P3 (restart test), P5 | **Blocked** 2026-09-23 — Apps API reachable, 0 apps; source upload to workspace files fails (`Cannot access AWS bucket`) | ? | Deploy needs workspace files; see row 13 |
| 2 | App container CPU / memory limits | P3 | Unverified | ? | §2.5 open fact; the probe app (`app.py` in scratch) reports cgroup limits once deploy works |
| 3 | Container recycling / scale-to-zero behaviour | P3 | Unverified | ? | §2.5: if frequent, revisit Apps-vs-Jobs before P3 |
| 4 | Background threads survive a multi-minute run | P3 | Unverified | ? | Probe app runs a 5 s heartbeat thread to measure this |
| 5 | HTTP / gateway timeout on callbacks | P3 | Unverified | ? | Polling callbacks must stay under it |
| 6 | Serverless SQL warehouse | P1A (Delta tests) | **Not present** — only `Starter Warehouse` (Pro); launch fails `WORKSPACE_CONFIGURATION_ERROR` 2026-09-23 | ? | Creation approved by user once storage works |
| 7 | Warehouse attachable as App resource | P5 | Unverified | ? | Grants the App service principal `CAN_USE` |
| 8 | Unity Catalog metadata | P1A | **Verified** 2026-09-23 — catalogs listed, schema `audit_ledger` created | ? | Names come from `DBX_CATALOG` / `DBX_SCHEMA` only |
| 9 | Unity Catalog managed storage (Delta writes) | P1A | **Blocked** 2026-09-23 — storage credential validation fails (`sts:AssumeRole` denied) | ? | Root cause of rows 1, 6, 13 |
| 10 | Delta features: CHECK constraints, `appendOnly`, deletion vectors, row tracking | P1A | Unverified (DDL written, not applied) | ? | Row tracking gives row-level concurrency for CAS updates |
| 11 | `DESCRIBE HISTORY` + `VERSION AS OF` on source tables | P3 / P5 | Unverified | ? | Needed for fingerprint source versions (§4.1) |
| 12 | Volumes (uploads/, exports/) | P5 | Unverified (no Volume exists) | ? | Path from `DBX_VOLUME` only |
| 13 | Workspace files (App source upload) | P5 | **Blocked** 2026-09-23 | ? | Same AWS trust failure |
| 14 | Pay-per-token model serving — Sonnet class | P6 | **Verified present** 2026-09-23 (`databricks-claude-sonnet-5`) | ? | Invocation and parameter matrix not yet run (§11) |
| 15 | Pay-per-token model serving — GPT-OSS | P6 | **Verified present** 2026-09-23 (`databricks-gpt-oss-120b`, `-20b`) | ? | As above |
| 16 | Model region / cross-geo processing | P6 | Unverified | ? | §9A.6 — governance decision with lead time |
| 17 | `ai_query()` from SQL warehouse | P6 | Unverified | ? | `classify` batch path |
| 18 | AI Gateway + inference tables | P6 | Unverified | ? | Needs working storage (row 9) |
| 19 | MLflow tracking (per-run, per-node spans) | P3 | Unverified | ? | Independent trail (§2.3) |
| 20 | `system.access.audit` readable | P3 | Unverified (`system` catalog visible) | ? | Independent trail (§2.3) |
| 21 | `system.billing.usage` readable | P6 | Unverified | ? | Cost measurement after ten runs |
| 22 | Service principal for the App + on-behalf-of-user identity | P5 | Unverified | ? | §9A.1; current default: App runs as SP, records signed-in user as `run_owner` |
| 23 | Secret scopes | P5 | Unverified | ? | §9A.8 |
| 24 | Outbound network from the build environment | P1A | **Verified** 2026-09-23 — workspace host reachable through the session proxy | ? | Corporate CI will need its own route (§9A.3) |
| 25 | Jobs (only if risk sensing is built) | Not planned | Not required | ? | §2.5, §4.10 |

## Environment-specific values

All of these live in environment variables (`.env.example`), never in source (NN16):
`DATABRICKS_HOST`, `DBX_CATALOG`, `DBX_SCHEMA`, `DBX_VOLUME`, `DBX_WAREHOUSE_HTTP_PATH`,
`DBX_APP_NAME`, `MODEL_SONNET`, `MODEL_GPT_OSS`, `CODE_REVISION`.
