# Capability matrix — development workspace vs corporate target

Required by CLAUDE.md §9C ("Free-to-Optus capability matrix": created in P1A, validated in P5).
Every row is something the build depends on. **Verified** means observed by a call from this
project, with the date; anything else is unverified and must not be assumed.

Development workspace: the Databricks-managed workspace in the gitignored `.env` (the first AWS workspace, whose IAM trust was broken, was abandoned on 2026-09-23).
Corporate target: to be filled by the platform owner before migration.

| # | Capability | Needed from | Development workspace | Corporate target | Notes |
|---|---|---|---|---|---|
| 1 | Databricks Apps (create + deploy) | P3 (restart test), P5 | **Verified** 2026-09-23 — created in 139 s (compute MEDIUM), deployed in 9 s | ? | App URL needs OAuth; PAT is redirected to login |
| 2 | App container CPU / memory limits | P3 | **Verified** 2026-09-23 — 4 vCPU, 15.6 GB RAM (MEDIUM); no cgroup limits visible below that | ? | Revisit if compute size changes |
| 3 | Container recycling / scale-to-zero behaviour | P3 | Observing — probe app writes a heartbeat (pid + boot epoch) every 30 s to `app_probe.heartbeat` | ? | §2.5: if frequent, revisit Apps-vs-Jobs before P3 |
| 4 | Background threads survive a multi-minute run | P3 | **Verified running** 2026-09-23 — daemon thread writing every 30 s; long-duration result pending | ? | |
| 5 | HTTP / gateway timeout on callbacks | P3 | Unverified | ? | Polling callbacks must stay under it |
| 6 | Serverless SQL warehouse | P1A (Delta tests) | **Verified** 2026-09-23 — cold start 8.5 s, warm 1.2 s; single-row writes 2–8 s | ? | |
| 7 | Warehouse attachable as App resource | P5 | **Verified** 2026-09-23 — `CAN_USE` resource; `valueFrom` injects the warehouse id; App SP writes Delta after `GRANT … TO \`<sp client id>\`` | ? | |
| 8 | Unity Catalog metadata | P1A | **Verified** 2026-09-23 — catalog `orchestrationsuite`, schema `audit_ledger` | ? | Names come from `DBX_CATALOG` / `DBX_SCHEMA` only |
| 9 | Unity Catalog managed storage (Delta writes) | P1A | **Verified** 2026-09-23 — Databricks-managed storage | ? | |
| 10 | Delta features: CHECK constraints, `appendOnly`, deletion vectors, row tracking | P1A | **Verified** 2026-09-23 — CHECK enforced; DV + row tracking accepted; CAS returns 1 then 0 affected rows | ? | appendOnly verified by the live contract suite |
| 11 | `DESCRIBE HISTORY` + `VERSION AS OF` on source tables | P3 / P5 | `DESCRIBE HISTORY` **verified**; `VERSION AS OF` pending | ? | |
| 12 | Volumes (uploads/, exports/) | P5 | Unverified (no Volume exists) | ? | Path from `DBX_VOLUME` only |
| 13 | Workspace files (App source upload) | P5 | **Verified** 2026-09-23 | ? | |
| 14 | Pay-per-token model serving — Sonnet class | P6 | **Verified present** 2026-09-23 (`databricks-claude-sonnet-5`) | ? | Invocation and parameter matrix not yet run (§11) |
| 15 | Pay-per-token model serving — GPT-OSS | P6 | **Verified present** 2026-09-23 (`databricks-gpt-oss-120b`, `-20b`) | ? | As above |
| 16 | Model region / cross-geo processing | P6 | Unverified | ? | §9A.6 — governance decision with lead time |
| 17 | `ai_query()` from SQL warehouse | P6 | Unverified | ? | `classify` batch path |
| 18 | AI Gateway + inference tables | P6 | Unverified | ? | Needs working storage (row 9) |
| 19 | MLflow tracking (per-run, per-node spans) | P3 | Unverified | ? | Independent trail (§2.3) |
| 20 | `system.access.audit` readable | P3 | Unverified (`system` catalog visible) | ? | Independent trail (§2.3) |
| 21 | `system.billing.usage` readable | P6 | Unverified | ? | Cost measurement after ten runs |
| 22 | Service principal for the App + on-behalf-of-user identity | P5 | App SP **verified** (OAuth M2M creds injected); on-behalf-of-user untested | ? | §9A.1; default: App runs as SP, records signed-in user as `run_owner` |
| 23 | Secret scopes | P5 | Unverified | ? | §9A.8 |
| 24 | Outbound network from the build environment | P1A | **Verified** 2026-09-23 — workspace host reachable through the session proxy | ? | Corporate CI will need its own route (§9A.3) |
| 25 | Jobs (only if risk sensing is built) | Not planned | Not required | ? | §2.5, §4.10 |

## Environment-specific values

All of these live in environment variables (`.env.example`), never in source (NN16):
`DATABRICKS_HOST`, `DBX_CATALOG`, `DBX_SCHEMA`, `DBX_VOLUME`, `DBX_WAREHOUSE_HTTP_PATH`,
`DBX_APP_NAME`, `MODEL_SONNET`, `MODEL_GPT_OSS`, `CODE_REVISION`.
