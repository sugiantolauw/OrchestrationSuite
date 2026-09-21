# AI Audit Analyst — Coding Agent Brief

You are building the next iteration of a working prototype called **AI Audit Analyst**, a domain-agnostic Internal Audit analytics platform running on Databricks Apps. The current version is a polished front-end shell with fixture data. Your job is to make it real by adding a working **LangGraph-based backend** that executes audit runs against synthetic/governed Databricks data, while preserving everything that works today.

Read this brief in full before proposing a plan. Do not skip sections. Ask clarifying questions only where a decision blocks progress; otherwise choose the sensible default and note it.

---

## 0. How to work on this codebase

Before making any changes:

- In this handoff bundle, the working app lives under `reference_app/`.
- **Read** `reference_app/app.py`, `reference_app/src/platform/pages.py`, `reference_app/src/platform/adapters.py`, `reference_app/src/platform/methodology.py`, `reference_app/src/test_catalogue.py`, and any existing `reference_app/src/orchestrator/` code in full. Do not rebuild from scratch — the existing prototype is the starting point.
- Preserve every existing UI component, route path, and design token. The workspace at `/workspace/tne` must remain functionally identical.
- Do not add features, refactor, or "improve" code that isn't required by the current phase.
- Do not add docstrings, comments, or type annotations to code you are not changing.
- Do not create new markdown documentation files unless the user asks.

While working:

- Commit at every stable checkpoint. Conventional commits: `feat(orchestrator): …`, `test(skills): …`, `fix(app): …`.
- Prefix every commit message with the phase number, e.g. `[P2] feat(orchestrator): add SqliteSaver checkpointer`.
- Run `python -m py_compile` on every changed Python file before commit.
- Run `pytest` before every commit. If tests fail, fix them before committing.
- Do not push to any remote unless explicitly asked.
- Do not connect to any Databricks workspace other than the one configured in `.env`.
- Ask before installing new dependencies or editing `requirements.txt`.
- When you finish a phase, **stop and report**. Do not automatically start the next phase.

Recommended Claude Code model workflow:

- **Phase kickoff and phase review** → Claude Fable 5.1 (top-tier planning and root-cause reasoning).
- **Phase implementation (writing code, tests, fixes)** → Claude Sonnet 4.6 (fast, cheap enough for sustained autonomous work).
- Switch model in Claude Code with `/model claude-sonnet-4-6` or `/model claude-fable-5-1`.
- Never leave Fable running through mechanical implementation work — burns Max quota unnecessarily.

---

## 1. Product vision (do not lose sight of this)

One governed application that supports multiple audit use cases through:

- Reusable **Skills** (versioned audit methodologies)
- **Playbook Mode** for established tests · **Explorer Mode** for new audits
- Governed data discovery from Unity Catalog + business-provided file upload
- LangGraph orchestration executing deterministic audit tests
- Evidence-linked findings with source-file traceability
- Persistent audit runs and management actions in governed Delta tables
- Excel, PowerPoint, and HTML exports
- Jira integration with an explicit approval step

The existing ExCo T&E prototype is the first working Skill. Future examples: T4.8 Input GST, Emergency Change, User Access Review, Software Licence Compliance.

The UX must be identical regardless of Skill. The Skill supplies domain-specific data sources, tests, metrics, charts, findings, and actions.

---

## 2. Current state (what already exists — do not rebuild)

### Repository structure

```
reference_app/
├── app.py                       # Dash entry point, URL routing, T&E workspace layout
├── app.yaml                     # Databricks Apps runtime config
├── requirements.txt             # Includes vendored Linux wheels
├── assets/
│   ├── theme.css                # Complete design system (~760 lines)
│   └── optus_template.pptx      # Optus-branded PPTX template
├── src/
│   ├── data_loader.py           # FILE_REGISTRY, EXCO_MEMBERS, ExCo population builders
│   ├── test_catalogue.py        # 14 T&E tests with rules and thresholds
│   ├── computation.py           # Metric calculators
│   ├── excel_export.py          # Styled Excel workbook generator
│   ├── pptx_export.py           # Optus-branded PowerPoint generator (uses vendored python-pptx)
│   ├── narrative.py             # Deterministic narrative builder
│   ├── guardrail.py             # Metric guardrails
│   ├── eval_gate.py             # Findings gate
│   ├── charts.py                # Plotly chart builders
│   └── platform/
│       ├── adapters.py          # Service interfaces (mostly mocked) — YOU WILL REPLACE THESE
│       ├── fixtures.py          # Demo data for Skills, runs, actions, trace events
│       ├── components.py        # Reusable UI components (kpi_card, skill_card, run_card, etc.)
│       ├── pages.py             # Platform pages: landing, skill library, methodology, runs, trace, actions
│       └── methodology.py       # Skill methodology assembly (real for T&E, stubs for others)
├── vendor/                      # Pre-built Linux wheels: lxml, pillow, python_pptx, typing_extensions, xlsxwriter
└── tests/                       # pytest suite for computation, eval_gate, guardrail
```

### Routes wired (all working)

| Path | Content |
|---|---|
| `/` | Platform landing page (Start an Audit) |
| `/workspace/tne` | Full working T&E ExCo audit workspace (executive brief, findings, exports) |
| `/runs` | Audit Runs list |
| `/skills` | Skill Library |
| `/skills/<skill_id>` | Skill methodology viewer |
| `/actions` | Cross-Skill Management Actions |
| `/trace` | Platform Trace (observable execution events) |

### Existing adapter interface (mocked today — you will implement)

`reference_app/src/platform/adapters.py` already exposes these function signatures. Keep the signatures stable. Replace the implementations.

```python
list_skills(filters)
get_skill(skill_id)
search_governed_data(query)
upload_audit_file(filename, content, run_id)
get_upload_base_path()
profile_sources(run_config)
propose_plan(run_config)
start_audit_run(run_config)     # ← today returns a mock run_id
get_run_status(run_id)
load_run(run_id)
list_audit_runs(filters)
list_management_actions(filters)
list_trace_events(run_id)
create_jira_preview(run_id)
is_demo_mode()
get_environment_label()
```

Keep every existing UI component and page working. Do not redesign the T&E workspace. Do not change route paths.

---

## 2a. Cloud Claude build context (this specific stage)

This handoff is intended for **Claude Code running through the Claude website**, not for direct work on the original local laptop. Real Optus data is still not accessible here. The immediate goal is to extend the current working prototype into a governed Databricks-backed build without relying on the separate synthetic-data package.

Implications:

- All environment-specific values (workspace host, catalog, schema, volume, model endpoint, app name) are **configuration**, never hardcoded. Read from env vars via `src/orchestrator/config.py`.
- **Do not** hardcode any Optus URLs, catalog names, volume paths, or model endpoints anywhere in the source. The migration to Optus must be an environment change, not a code change.
- Fixture data (`src/platform/fixtures.py`), existing demo content, and any governed/uploaded data you add later are the only inputs during this stage.
- Adopt six adapter interfaces so the Optus environment can supply its own implementations later: `DataSourceAdapter`, `ModelClient`, `PersistenceAdapter`, `PromptRepository`, `TracingAdapter`, `ExportStorageAdapter`.
- Each Skill commits a **data contract** at `src/orchestrator/skills/<skill_id>/contract.yaml` — required columns, types, keys, nullability, PII class. Later data sources should satisfy the contract without changing the orchestration code.
- `.env` is gitignored. Ship `.env.example` with placeholders.

---

## 3. Databricks environment (target workspace)

This section describes the eventual target workspace for the cloud-Claude build. All values below are placeholders read from environment variables.

| Setting | Env var | Personal (this build) | Optus (later migration) |
|---|---|---|---|
| Workspace host | `DATABRICKS_HOST` | target workspace URL | Optus workspace URL |
| CLI profile | `DATABRICKS_PROFILE` | e.g. `target` | e.g. `optus-dev` |
| Auth | `DATABRICKS_TOKEN` (PAT) or OAuth | target token | corporate SSO/PAT |
| Catalog | `DBX_CATALOG` | chosen governed catalog | Optus governed catalog |
| Schema | `DBX_SCHEMA` | `ai_audit_analyst` | `ai_audit_analyst` |
| Volume | `DBX_VOLUME` | `/Volumes/<catalog>/ai_audit_analyst/uploads` | Optus governed volume |
| App name | `DBX_APP_NAME` | e.g. `ai-audit-analyst` | Optus app name |
| Sync path | `DBX_SYNC_PATH` | `/Workspace/Users/<you>/…` | Optus workspace path |
| Model endpoint host | `MODEL_ENDPOINT_HOST` | `<workspace-host>/serving-endpoints` | Optus endpoint host |
| Model routing | `NODE_MODELS_JSON` | JSON overrides (see §4b) | JSON overrides |

### Deployment sequence

```powershell
# 1. Sync
databricks sync ./tne_exco_app $env:DBX_SYNC_PATH --full --profile $env:DATABRICKS_PROFILE

# 2. Deploy
databricks apps deploy $env:DBX_APP_NAME --mode SNAPSHOT --source-code-path $env:DBX_SYNC_PATH --profile $env:DATABRICKS_PROFILE

# 3. Verify
databricks apps get $env:DBX_APP_NAME --profile $env:DATABRICKS_PROFILE -o json
```

If you later add a deployment wrapper, keep it thin and env-driven.

### Known Databricks Apps constraints

- **No C-extension compilation at deploy time** — heavy deps must be vendored as pre-built `manylinux2014_x86_64` wheels in `vendor/`.
- **Databricks Model Serving IS available** — LLM inference is a first-class part of the pipeline (see §4b). Endpoint config-driven and swappable.
- **Concurrent Delta writes fail** — `CREATE OR REPLACE TABLE` in overlapping runs triggers `ConcurrentAppendException`. Guard with a locking pattern or run-scoped table names.
- **App must be started before deploy** — if in `UNAVAILABLE` state, `databricks apps start $env:DBX_APP_NAME --profile $env:DATABRICKS_PROFILE` first.
- Do not assume any specific local shell or laptop path layout in generated instructions.

### Environment restrictions

- **MCP is not permitted** in the target Optus environment. Do not add MCP dependencies. Use terminal-driven Databricks CLI and REST API only.
- **LLM inference is in scope** via Databricks Model Serving. Endpoint via `MODEL_ENDPOINT_HOST`. Assume an OpenAI-compatible chat completion API surface.
- **No real Optus data is available in this handoff.** If a task requires real data to progress, stop and report the dependency clearly.

---

## 4. What you are building — the LangGraph backend

### Architecture at a glance

**Shape:** Skill-driven pipeline graph. A single fixed LangGraph DAG whose node bodies are supplied by whichever Skill the user selected. Not a multi-agent swarm. Not a monolithic tool-using agent. A deterministic pipeline with LLM narration and LLM-assisted planning at specific, bounded points.

### Architecture defaults — challenge only with strong reasons

Two lists. **Non-negotiables** are commitments the rest of the design depends on; do not change without stopping and proposing to the user first. **Starting proposals** are working defaults you can revise silently while implementing, as long as the non-negotiables stay intact.

**Non-negotiables (propose before changing):**

1. **LangGraph as the orchestration framework.** No custom framework. No agent swarm. No supervisor pattern for the core pipeline. Confine LangGraph imports to `graph.py`; keep `state.py`, nodes, skills, LLM client, and persistence pure Python so the framework is thinly coupled.
2. **Deterministic computation + LLM narration boundary.** Numbers, thresholds, categories, and audit decisions come from Python computing on data. LLMs write prose *around* those numbers. `execute` never uses an LLM.
3. **One HITL gate between `plan` and `execute`.** Mandatory for Explorer Mode, optional for Playbook Mode. Implemented as `interrupt_before("execute")`.
4. **`Skill` protocol pattern.** Skills supply node bodies. The graph shape is universal across audits.
5. **`RunState` as the single source of truth.** Checkpointed at every node.
6. **Delta as system of record.** Runs, findings, actions, events, and LLM calls persisted to Delta. In-memory or fixture fallback only for demo mode, clearly labelled.
7. **MLflow observability.** Every graph run and every LLM call is traced via `mlflow.langchain.autolog()` plus manual per-node spans. Also the substrate for evaluation (§6).
8. **Model routing is config-driven.** Node → model mapping lives in `config.NODE_MODELS`, overridable by env var. No model names hardcoded in nodes.

**Starting proposals (open to alternatives with brief justification in the commit or PR):**

- The 9-node breakdown (`discover → profile → plan → execute → classify → find → prioritise → act → export`). If merging two nodes or splitting one is genuinely simpler, do it and explain why in the commit.
- `RunState` as `@dataclass`. Pydantic is fine if validation-heavy inputs justify it — same shape, different implementation.
- Checkpointer: `MemorySaver` for tests, `SqliteSaver` for dev, `PostgresSaver` (Databricks Lakebase) later. Swap freely as long as the persistence contract holds.
- Node retry policy. Choose per node.
- Prompt template format. `.txt` with `str.format` slots is a suggestion, not a mandate.
- Response cache backend. Dict is fine; upgrade if needed.

### Overall architecture

Introduce a new package `src/orchestrator/` implementing a LangGraph state machine that executes an audit run. Wire this behind the existing `start_audit_run(run_config)` adapter so the front end does not change.

```
src/orchestrator/
├── __init__.py
├── config.py            # NODE_MODELS, env-var-driven settings, adapter wiring
├── state.py             # RunState dataclass (typed run state) — no LangGraph imports
├── graph.py             # LangGraph StateGraph definition — the only LangGraph-importing module
├── nodes/
│   ├── discover.py      # Node: resolve data sources
│   ├── profile.py       # Node: profile sources, compute quality metrics + LLM narration
│   ├── plan.py          # Node: build execution plan (Playbook: config; Explorer: LLM)
│   ├── execute.py       # Node: run deterministic tests (NO LLM)
│   ├── classify.py      # Node: rules-first, LLM for ambiguous residual
│   ├── find.py          # Node: generate evidence-linked findings + narratives (+ optional critic)
│   ├── prioritise.py    # Node: risk scoring + optional 1-line rank tooltip
│   ├── act.py           # Node: draft management actions + remediation email drafts
│   └── export.py        # Node: PPTX / Excel / Jira previews + exec summary + chart captions
├── skills/              # Skill registry — Python modules per Skill
│   ├── __init__.py
│   ├── base.py          # Skill protocol / abstract base
│   ├── tne_exco/        # SKILL-001: T&E
│   │   ├── skill.py
│   │   ├── contract.yaml
│   │   └── prompts/     # per-Skill prompt overrides
│   └── gst_t48/         # SKILL-002: T4.8 Input GST
│       ├── skill.py
│       ├── contract.yaml
│       └── prompts/
├── adapters/            # Six adapter interfaces for portability (§10)
│   ├── data_source.py
│   ├── model_client.py
│   ├── persistence.py
│   ├── prompt_repository.py
│   ├── tracing.py
│   └── export_storage.py
├── persistence.py       # Delta table read/write for runs, findings, actions, events, llm_calls
├── llm.py               # LLMClient — one call surface; logs every request to Delta
├── prompts/             # Default prompt templates (Skills override in their own prompts/)
│   ├── profile_narrative.txt
│   ├── finding_narrative.txt
│   ├── exec_summary.txt
│   ├── chart_caption.txt
│   ├── remediation_draft.txt
│   ├── priority_rationale.txt
│   ├── classification_reasoning.txt
│   └── plan_suggestion.txt
└── events.py            # Emits observable trace events during execution
```

### Non-negotiable design rules

1. **Deterministic computation, LLM narration.** All *numbers* in a finding — dollar amounts, counts, ratios, thresholds, categories — come from Python computing on data. LLMs produce the *prose that wraps* those numbers, using template slots the node fills from `RunState`. LLMs never generate figures.
2. **Every LLM call is logged.** Prompt, response, model endpoint, temperature, token counts, latency, node name, run_id → Delta table `llm_calls`. This is the audit trail; non-negotiable.
3. **Temperature = 0 by default.** The only exception is the Explorer Mode `plan` node where a modest temperature (~0.3) helps the model suggest diverse tests. Reproducible workpapers depend on this.
4. **Response caching keyed on `(prompt_hash, model, temperature)`** — for demo stability and to make re-runs identical unless data changed.
5. **Every metric carries source-file provenance.** Follow the existing pattern in `app.py` where `EVIDENCE_PAYLOAD["metrics"]` values include `{value, unit, source_file}`.
6. **Human-in-the-loop is mandatory.** Explorer Mode plans must halt before execution until an auditor confirms. Playbook Mode plans should offer a "review approach" checkpoint. Every LLM-generated narrative in the UI has "regenerate" and "edit" affordances.
7. **No chain-of-thought exposed.** Platform Trace shows execution events only — timestamps, stages, statuses, short operational messages, durations. LLM reasoning is stored in `llm_calls` for audit but not shown in Trace.
8. **Narrative traceability.** Every LLM-generated paragraph shown in the UI must display which `RunState` fields (test results, metrics) supplied its numbers. A reviewer clicks a sentence and sees the source.
9. **Persistence must be Delta-backed.** Runs, findings, management actions, events, and LLM calls go to Delta tables in `${DBX_CATALOG}.${DBX_SCHEMA}`. Session-only fallback for demo mode is acceptable but must be clearly labelled.
10. **No fake successful integrations.** If Unity Catalog search fails, show a demo indicator. If Jira is not connected, show "Preview — not submitted". If the model endpoint is misconfigured, show the deterministic output alone and flag the narrative as "LLM unavailable" — never silently fabricate.
11. **Preserve existing T&E behaviour bit-for-bit.** The workspace at `/workspace/tne` must remain identical to what runs today. LLM narration is *additive* — it does not replace any existing computed content.
12. **Idempotent runs.** Restarting a failed run should not double-write. Use run_id partitioning. Cached LLM responses are reused on resume.
13. **Backwards-compatible fixtures.** The fixture data in `src/platform/fixtures.py` must still power the demo when Delta tables are empty or unavailable.

### LangGraph state contract

```python
@dataclass
class RunState:
    run_id: str
    skill_id: str | None                  # None for Explorer
    mode: Literal["playbook", "explorer"]
    audit_period: tuple[date, date]
    objective: str
    data_assets: list[dict]               # Unity Catalog references
    uploaded_files: list[dict]            # Volume references
    business_unit: str | None
    materiality: float | None
    options: dict                         # preview_plan, gen_actions, jira_preview

    # populated as the graph progresses
    profile_result: dict | None
    plan: dict | None
    plan_confirmed: bool
    test_results: list[dict]
    exceptions: list[dict]
    findings: list[dict]
    management_actions: list[dict]
    exports: dict                          # paths / artefact metadata

    # LLM-generated narrative (never contains raw numbers — always template-filled)
    profile_narrative: str | None
    plan_rationale: dict[str, str]         # test_id -> why suggested (Explorer Mode)
    classification_reasoning: dict[str, str]  # row_id -> why classified this way
    finding_narratives: dict[str, str]     # finding_id -> narrative paragraph
    priority_rationale: dict[str, str]     # finding_id -> why this rank
    remediation_drafts: dict[str, str]     # finding_id -> draft email
    exec_summary: str | None
    chart_captions: dict[str, str]         # chart_id -> caption

    # observability
    events: list[dict]                     # trace events emitted per node
    llm_calls: list[dict]                  # audit trail of every model call
    errors: list[dict]
    status: Literal["running", "awaiting_confirmation", "completed", "failed"]
```

### Node responsibilities

| Node | Reads | Writes | LLM use | Emits event |
|---|---|---|---|---|
| `discover` | `data_assets`, `uploaded_files` | resolved source refs | — | "Sources selected" |
| `profile` | resolved sources | row counts, schemas, quality flags | Narrate profile in plain English | "Data profiled" |
| `plan` | Skill definition + profile | ordered test plan | Explorer Mode only: suggest tests + rationale (temp > 0) | "Plan generated" |
| *(gate)* | `plan_confirmed` | halts if false | — | "Awaiting confirmation" |
| `execute` | plan + sources | test outputs | **Never** — pure Python | "Tests executed" |
| `classify` | test outputs | classified exceptions | Ambiguous rows only; deterministic rules first | "Exceptions classified" |
| `find` | classified exceptions | findings with evidence refs | Draft finding narrative around computed numbers | "Findings generated" |
| `prioritise` | findings | risk-scored, ordered findings | 1-line "why this rank" per finding | "Findings prioritised" |
| `act` | prioritised findings | draft management actions | Draft remediation email | "Actions drafted" |
| `export` | findings, actions, run metadata | PPTX/Excel/Jira previews | Exec summary + chart captions | "Exports prepared" |

Every node writes its event to Delta and appends to `RunState.events`. Every LLM call is logged to Delta and appended to `RunState.llm_calls`.

### 4b. Model routing per node

Goal: match model tier to task difficulty so cost stays reasonable without sacrificing narrative quality where it matters.

| Node | Model tier | Model (default) | Rationale |
|---|---|---|---|
| `discover` | none | — | Deterministic; resolve UC references from Skill config. |
| `profile` | Standard | Haiku 4.5 / Llama 3.3 70B | Structured, low-stakes prose. |
| `plan` (Playbook) | none | — | Skill supplies the test list from config. |
| `plan` (Explorer) | Premium | Sonnet 4.6 / Fable 5.1 | Only place where genuine reasoning about test selection is required. `temperature ~0.3`. |
| `execute` | none | — | Pure SQL/pandas. **Never** LLM. |
| `classify` | Standard (ambiguous rows only) | Haiku 4.5 | Rules-based classifier first; LLM handles the residual with reasoning + confidence. |
| `find` | Premium | Sonnet 4.6 | Finding narrative goes to the CAO briefing. Small volume × high visibility. |
| `prioritise` | Cheap or none | Llama 3.1 8B | Ranking is deterministic. LLM only used for optional 1-line "why this rank" tooltips. |
| `act` | Standard | Haiku 4.5 | Remediation email drafts. Auditor edits before sending. |
| `export` — exec summary | Premium | Sonnet 4.6 | Read by executives verbatim. |
| `export` — chart captions | Cheap | Llama 3.1 8B | One sentence per chart, highly templated. |

Routing rule of thumb per node:

1. Does an executive read the output verbatim? → Premium.
2. Does an auditor read and edit before it goes anywhere? → Standard.
3. Is it templated boilerplate or short structured text? → Cheap or no LLM.

Routing lives in `src/orchestrator/config.py`:

```python
NODE_MODELS: dict[str, str] = {
    "profile":         os.environ.get("MODEL_PROFILE",         "databricks-claude-haiku-4-5"),
    "plan_explorer":   os.environ.get("MODEL_PLAN_EXPLORER",   "databricks-claude-sonnet-4-6"),
    "classify":        os.environ.get("MODEL_CLASSIFY",        "databricks-claude-haiku-4-5"),
    "find":            os.environ.get("MODEL_FIND",            "databricks-claude-sonnet-4-6"),
    "prioritise":      os.environ.get("MODEL_PRIORITISE",      "databricks-meta-llama-3-1-8b-instruct"),
    "act":             os.environ.get("MODEL_ACT",             "databricks-claude-haiku-4-5"),
    "export_summary":  os.environ.get("MODEL_EXPORT_SUMMARY",  "databricks-claude-sonnet-4-6"),
    "export_caption":  os.environ.get("MODEL_EXPORT_CAPTION",  "databricks-meta-llama-3-1-8b-instruct"),
}
```

The model names above are placeholders. Confirm each against the endpoints actually available on the user's Databricks Free Edition workspace before committing. Do not silently substitute a different family (e.g. Mistral) — ask.

**Fallback policy:** on retry failure, fall back to the Cheap tier with a warning logged to MLflow. **Never** fall back upward — that's a cost leak. If cheap tier also fails, return `LLMResponse(text=None, error=...)` and let the UI show "LLM unavailable".

### 4c. Multi-agent scope (what to build, what NOT to build)

The primary Audit Agent is the single LangGraph pipeline described above. In this brief, "agent" = one LangGraph state machine.

**In scope now:**

- **One pipeline agent** — the 9-node DAG. This is *the* Audit Agent.

**Optional (default OFF, enable via config only when metrics justify it):**

- **Findings critic** — a second LLM call inside the `find` node that reviews each generated narrative for grounding (every number in the prose must appear in `RunState`). Standard tier. Controlled by `FINDINGS_CRITIC_ENABLED=true`. Only enable if narrative-faithfulness evaluation (§6) shows failures the regex-based check misses.

**Out of scope (do NOT build, do NOT propose):**

- Multi-agent swarm.
- Supervisor/worker pattern across the pipeline.
- Explorer Mode planner + skeptic pair.
- Post-run conversational companion agent. (May come later; not now.)
- Any agent that decides which node runs next. The DAG is fixed.

If a phase seems to "need" a second agent, stop and ask before building it. In most cases the answer is: it doesn't; a different prompt or a rule change will solve it.

### LLM client contract (`src/orchestrator/llm.py`)

One module. One class. Every node imports from it.

```python
class LLMClient:
    def __init__(self, endpoint_host: str | None = None):
        # endpoint_host from env MODEL_ENDPOINT_HOST; None => degraded mode
        ...

    def complete(
        self,
        prompt: str,
        *,
        node: str,             # e.g. "find", "export_summary" — used to look up NODE_MODELS
        run_id: str,
        model: str | None = None,   # explicit override; defaults to NODE_MODELS[node]
        temperature: float = 0.0,
        max_tokens: int = 800,
        cache: bool = True,
    ) -> LLMResponse: ...
```

- Uses the Databricks Model Serving OpenAI-compatible endpoint via the `openai` client pointed at the workspace serving URL.
- If the endpoint is unset or the call fails on both primary and cheap-tier fallback, return `LLMResponse(text=None, error=...)`. Nodes must handle this gracefully and leave the narrative field `None`. UI shows "LLM unavailable — deterministic output only".
- Every call inserts a row into `${DBX_CATALOG}.${DBX_SCHEMA}.llm_calls` synchronously before returning.
- Response cache is an in-process dict keyed on `(sha256(prompt), model, temperature)`. Optional Delta-backed cache later.

### Prompt template contract

Prompts live in `src/orchestrator/prompts/` as plain `.txt` files with Python `str.format`-style slots. Each node loads its template, fills slots from `RunState`, and calls the client. Skills override by placing a same-named file in `src/orchestrator/skills/<skill_id>/prompts/`.

Example slot convention — `finding_narrative.txt`:

```
You are drafting the narrative for one audit finding. Use the numbers exactly as given.
Do not invent figures. Do not speculate about causes not present in the evidence.

Skill: {skill_name}
Finding title: {finding_title}
Materiality threshold: {materiality}
Computed metrics: {metrics_json}
Supporting test IDs: {test_ids}

Write 2–3 sentences suitable for a Chief Audit Officer briefing.
```

The node computes `metrics_json` from `RunState.test_results`; the model cannot introduce numbers of its own.

### Delta persistence schema

All DDL is templated on `${DBX_CATALOG}` and `${DBX_SCHEMA}` env vars. Do not hardcode `sdpt_gia` or any Optus name. Provide migrations as idempotent `CREATE TABLE IF NOT EXISTS` statements in `src/orchestrator/migrations/`.

Required tables:

- `runs` — one row per audit run, partitioned by `skill_id`.
- `findings` — evidence-linked findings, keyed by `run_id` + `finding_id`.
- `management_actions` — cross-Skill actions.
- `trace_events` — one row per node execution event.
- `uploaded_files` — provenance for volume uploads.
- `llm_calls` — full audit trail: prompt, response, model, temperature, tokens, latency, node, run_id.
- `evaluation_runs` — MLflow-linked evaluation results per prompt/model version (see §6).

Example skeleton:

```sql
CREATE SCHEMA IF NOT EXISTS ${DBX_CATALOG}.${DBX_SCHEMA};

CREATE TABLE IF NOT EXISTS ${DBX_CATALOG}.${DBX_SCHEMA}.runs (
  run_id STRING NOT NULL,
  skill_id STRING,
  skill_name STRING,
  mode STRING,
  audit_period_start DATE,
  audit_period_end DATE,
  run_owner STRING,
  data_mode STRING,
  status STRING,
  findings_count INT,
  high_risk_count INT,
  potential_exposure DOUBLE,
  open_actions INT,
  run_config STRING,   -- JSON
  started_at TIMESTAMP,
  updated_at TIMESTAMP
) USING DELTA
PARTITIONED BY (skill_id);
```

Design the remaining schemas from the field lists shown in the existing UI cards (`run_card`, `action_row`, `trace_event_row`, `data_asset_card`) and from the `RunState` narrative fields.

### Skill protocol

```python
class Skill(Protocol):
    skill_id: str
    name: str
    domain: str
    version: str

    def required_sources(self) -> list[dict]: ...
    def build_test_plan(self, run_config, profile_result) -> list[TestDef]: ...
    def run_tests(self, sources, plan) -> list[TestResult]: ...
    def classify_exceptions(self, test_results) -> list[Exception]: ...
    def build_findings(self, exceptions, metrics) -> list[Finding]: ...
    def draft_actions(self, findings) -> list[ManagementAction]: ...
    def render_workspace(self, run_id) -> dash.html.Div: ...  # returns the run-scoped workspace layout
```

For `SKILL-001` (T&E), `render_workspace` returns the existing `tne_workspace_layout` from `app.py` parameterised by run_id. Extract that function from `app.py` into `src/orchestrator/skills/tne_exco.py`.

---

## 5. Governed data discovery

The implementation must speak Unity Catalog APIs so it works cleanly once connected to a real governed workspace.

```python
from databricks.sdk import WorkspaceClient

def search_governed_data(query: str) -> list[dict]:
    w = WorkspaceClient()   # uses DATABRICKS_HOST/TOKEN from env
    # search across catalogs the current user has access to
    ...
```

Until governed source tables are available, keep the discovery flow compatible with fixture data, uploaded files, and future governed tables. Do not assume a bundled synthetic-data package exists in this handoff.

Handle no-access catalogs gracefully — return the asset with `access="Restricted"` rather than hiding it. For volume file listing use `WorkspaceClient().files.list_directory_contents(...)`.

---

## 6. Evaluation (five surfaces — do NOT collapse into one)

All five run in CI or on demand. Each answers a different question. Do not treat them as one benchmark.

### Surface 1 — Deterministic parity (pytest, every commit)

**Question:** does the new orchestrator produce identical *computed* findings to today's `app.py` on the same fixture data?

- Load fixture data used by current `app.py`.
- Run both code paths.
- Assert `findings_legacy.numbers == findings_new.numbers` and `findings_legacy.categories == findings_new.categories`.
- **Narrative fields are excluded from parity** — they are LLM-generated and non-deterministic by design.
- Any deviation is a regression. Blocks merge.

### Surface 2 — Test correctness on synthetic data (pytest, every commit)

**Question:** do individual audit tests correctly flag planted exceptions?

- Use synthetic datasets with known plants (e.g. 47 duplicate T&E claims among 1,000 rows).
- For each test, compute precision and recall against the plants.
- Ship threshold: **precision ≥ 0.98, recall ≥ 0.95**. Below that, the test is broken.

### Surface 3 — Narrative faithfulness (pytest, every commit)

**Question:** does the LLM only quote numbers that exist in `RunState`?

- Regex-extract all numeric literals from LLM prose.
- Extract the set of numbers the node was allowed to reference from `RunState`.
- Assert `narrative_numbers.issubset(allowed_numbers)`. Any surplus = hallucination.
- Runs in milliseconds. Catches ~90% of what matters.

### Surface 4 — Narrative quality (MLflow evaluate, weekly)

**Question:** is the writing accurate, clear, and appropriate for a CAO audience?

- MLflow `mlflow.evaluate()` with two judges:
  - **LLM-as-judge (Fable 5.1 or Sonnet 4.6)** scoring: grounding, clarity, tone, absence of speculation, correct citation of test IDs. 1–5 rubric.
  - **Human review** on a rolling sample (~10 findings per week during the build).
- **Golden set:** ≈50 curated examples per Skill stored as a Delta table under `${DBX_CATALOG}.${DBX_SCHEMA}.golden_examples`. Version it.
- Track mean judge score per node per model in the `evaluation_runs` table.

### Surface 5 — End-to-end runs (pytest nightly + pre-demo)

**Question:** does a full audit run produce the expected top-level metrics?

- Curated scenarios: dataset A → expected 8 findings with $X total exposure.
- Run the full graph. Assert top-level metrics within tolerance.
- Catches integration bugs, persistence bugs, node ordering bugs.

### Model routing decision loop (per node, per model change)

1. Deploy pipeline with **all nodes on Premium** (Sonnet 4.6 or Fable 5.1 everywhere) → record Surfaces 3 & 4 scores. This is the **quality ceiling**.
2. For each LLM-using node, downgrade one tier (Premium → Standard, Standard → Cheap).
3. Re-run golden set.
4. Keep the downgrade if quality ≥ **95% of ceiling**. Revert if not.
5. Lock the resulting `NODE_MODELS` config.
6. Re-run the loop when prompts or models change.

---

## 7. File upload

Replace mock `upload_audit_file` with a real Volume write, path templated from env:

```python
def upload_audit_file(filename, content, run_id):
    dest = f"{os.environ['DBX_VOLUME']}/runs/{run_id}/input/{filename}"
    w = WorkspaceClient()
    w.files.upload(file_path=dest, contents=io.BytesIO(content), overwrite=False)
    ...
```

Enforce max size, validate mime type against Skill's required-source list, update row's validation status through `Uploaded → Profiling → Ready` (or `Warning`, `Failed`).

---

## 8. Jira integration (deferred)

Jira submission is **not in scope** for the personal Free Edition build. Keep `create_jira_preview` returning a stub preview object and mark the UI as "Preview — not submitted". Do not implement submission until Phase 8 during the Optus integration stage.

---

## 9. Deployment & vendored dependencies

Add LangGraph and the model-serving client to `requirements.txt`. Vendor transitive deps if any require compilation:

```
langgraph>=0.2.0
langchain-core>=0.3.0
databricks-sdk>=0.30.0
mlflow>=2.16.0
openai>=1.40.0        # for Databricks Model Serving OpenAI-compatible client
```

Test the deploy path. If any dep fails to install on Databricks Apps runtime, download the correct `manylinux2014_x86_64` wheel to `vendor/` and reference it explicitly in `requirements.txt`.

### Model endpoint configuration

Endpoint set via env vars. Do NOT hardcode names. Config values:

- `MODEL_ENDPOINT_HOST` — workspace serving URL, e.g. `https://<workspace-host>/serving-endpoints`
- `NODE_MODELS_JSON` — optional JSON blob overriding the defaults in `config.NODE_MODELS`
- Individual overrides: `MODEL_PROFILE`, `MODEL_FIND`, etc. (see §4b)

If `MODEL_ENDPOINT_HOST` is unset, the app runs in degraded mode: no narratives, UI shows "LLM unavailable — deterministic output only".

---

## 10. Portability contract (personal → Optus migration)

Design for portability from commit one. The Optus migration must be an **environment change**, not a code rewrite.

### Environment abstraction

- Every workspace-specific value in `src/orchestrator/config.py`, read from env vars. `.env` gitignored; ship `.env.example` with placeholders.
- No literal `sdpt_gia`, `adb-*`, `optus.com.au`, or personal workspace names anywhere in the source.

### Six adapter interfaces

Define in `src/orchestrator/adapters/` as `Protocol`s. This build should remain portable across fixture-backed, upload-backed, and governed-data implementations without changing the graph or nodes.

- `DataSourceAdapter` — read tables/volumes for a Skill.
- `ModelClient` — wraps `LLMClient`; swappable endpoint host.
- `PersistenceAdapter` — Delta writes for runs, findings, actions, events, llm_calls.
- `PromptRepository` — load prompt template by name (filesystem now, MLflow Prompt Registry later).
- `TracingAdapter` — MLflow spans + Delta trace events.
- `ExportStorageAdapter` — write PPTX/XLSX to volume; return URL.

### Data contracts per Skill

`src/orchestrator/skills/<skill_id>/contract.yaml`:

```yaml
skill_id: SKILL-001
name: T&E ExCo
sources:
  - logical_name: tne_claims
    required_columns:
      - name: claim_id
        type: string
        nullable: false
        pk: true
      - name: submitter_id
        type: string
        nullable: false
      - name: amount_aud
        type: decimal(18,2)
        nullable: false
      - name: claim_date
        type: date
        nullable: false
      - name: category
        type: string
        allowed: ["Travel", "Meals", "Accommodation", "Entertainment", "Other"]
      - name: description
        type: string
        pii: true
    business_keys: [claim_id]
    date_semantics: claim_date
    currency: AUD
    quality_checks:
      - non_null: [claim_id, submitter_id, amount_aud, claim_date]
      - unique: [claim_id]
```

The contract stays stable even as the underlying source moves from fixtures to governed views. Nothing else should need to change.

---

## 11. Working style and cadence

- Work on `master` branch or feature branches per phase. Commit at every stable checkpoint.
- Prefix commits with phase number: `[P2] feat(orchestrator): ...`.
- Before any Databricks deploy, run: `python -m py_compile app.py src/**/*.py` and the pytest suite.
- Never bypass safety flags (`--no-verify`, `--force`).
- Never overwrite `.databrickscfg` or store tokens in source.
- Never touch files outside the packaged project folder.
- After each meaningful change: syntax-check, run tests, sync, deploy, confirm deployment succeeded via `databricks apps get`.
- Show the deployment ID after every successful deploy so the user can roll back.
- When you finish a phase, **stop and report**. Do not auto-start the next phase.

---

## 12. Phased delivery (implement in this order)

1. **Phase 1 — Scaffolding & parity.** Create `src/orchestrator/` package. Move the T&E workspace layout out of `app.py` into `skills/tne_exco/skill.py`. Write the parity test. Deploy. Confirm the workspace still works identically.
   *Gate:* Surface 1 parity test passes.

2. **Phase 2 — LangGraph shell + MLflow.** Define `RunState`, `graph.py`, and node stubs that emit events but wrap the existing T&E computation as a single monolithic node. Wire `start_audit_run` to invoke the graph. Wire `mlflow.langchain.autolog()` at graph entry. Wire `MemorySaver`/`SqliteSaver` checkpointer. Persist events to Delta. Trace page shows real events.
   *Gate:* Surface 1 still passes; a run appears in the MLflow Experiment.

3. **Phase 3 — Delta persistence + adapters.** Create Delta schemas via idempotent migrations. Implement `PersistenceAdapter`. Replace fixture list endpoints with Delta reads. Fall back to fixtures if schema is missing.
   *Gate:* Runs, findings, actions, events all round-trip via Delta.

4. **Phase 4 — Decomposed nodes.** Split the monolithic T&E node into `discover → profile → plan → execute → classify → find → prioritise → act → export`. Each node emits its own MLflow span.
   *Gate:* Surface 1 still passes with decomposed nodes.

5. **Phase 5 — Unity Catalog + Volume upload.** Implement `DataSourceAdapter` and file upload against governed tables or uploaded files and a configured Volume. Handle no-access + quota gracefully.
  *Gate:* Discoverable data sources appear via UI; a real file uploads to the configured Volume.

6. **Phase 6 — Second Skill (GST T4.8).** Implement `skills/gst_t48/` with 3–4 real tests over a **synthetic** AP dataset persisted as a Delta table. Full lifecycle from discover to export.
   *Gate:* Explorer + Playbook modes both work end-to-end for GST on synthetic data.

7. **Phase 7 — Human-in-the-loop.** Implement plan confirmation via `interrupt_before("execute")`. Runs pause at the gate; UI shows plan; auditor confirms; run resumes from checkpoint.
   *Gate:* A paused run resumes correctly across a browser refresh.

8. **Phase 7a — LLM integration.** Build `llm.py`, prompt templates in `src/orchestrator/prompts/`, `NODE_MODELS` config, `llm_calls` Delta table, degraded mode. Wire narrative fields into `profile`, `find`, `prioritise`, `act`, `export` nodes. Add "regenerate" and "edit" affordances + narrative-traceability tooltip.
   *Gate:* Surface 3 (faithfulness) passes 100%. Surface 4 (quality) baseline recorded.

9. **Phase 7b — Findings critic (optional).** Only build if Phase 7a's Surface 3 misses hallucinations OR Surface 4 quality is below target on `find` node. Otherwise skip.
   *Gate:* Critic reduces hallucinations without hurting run cost budget by more than 10%.

10. **Phase 7c — Model routing loop.** Run the routing decision loop (§6). Downgrade nodes tier-by-tier while maintaining ≥95% quality. Lock resulting `NODE_MODELS`.
    *Gate:* Total per-run inference cost ≤ 30% of all-Premium baseline at ≥95% quality.

11. **Phase 8 — Jira preview (deferred).** Do not build during the personal-laptop stage. Reserved for post-Optus-migration.

12. **Phase 9 — Hardening.** Idempotency, concurrent-run protection (run-scoped table names), error recovery, retry policies, comprehensive logging, observability polish. Prepare Optus migration checklist.
    *Gate:* Full nightly run of Surfaces 1–5 passes for 3 consecutive nights.

At the end of each phase: commit, deploy to the personal workspace, verify the app runs, report deployment ID + a one-paragraph summary of what changed.

---

## 13. Definition of done (cloud-Claude handoff build)

The personal build is ready to migrate to Optus when:

- A user can open the app URL and see the platform landing page.
- The user can search the configured catalog for governed tables and select them.
- The user can upload a real file to the personal Volume and see validation progress.
- The user can select the T&E Skill and run it against synthetic ExCo data — computed findings match the current prototype exactly (Surface 1 parity).
- The user can select the T4.8 GST Skill and run it against a synthetic AP dataset with real tests producing real findings.
- The user can enter Explorer Mode, describe an objective, see the proposed plan, and confirm before execution (HITL gate resumes correctly).
- Every audit run is persisted to Delta and remains selectable across sessions.
- Every graph run is visible in the MLflow Experiment with per-node spans.
- Platform Trace shows real execution events (not fixture data) after any run.
- Management Actions page shows real actions from Delta across all Skills.
- Skill Methodology Viewer still shows the real T&E methodology and honest "under development" panels for other Skills.
- Existing PPTX and Excel exports work for any completed run.
- **Surface 1 parity test** passes: legacy `app.py` computed findings == new orchestrator computed findings.
- **Surface 2 test correctness** passes: precision ≥ 0.98, recall ≥ 0.95 per test.
- **Surface 3 narrative faithfulness** passes: no LLM-produced narrative contains a number not in `RunState`.
- **Surface 4 narrative quality** baseline recorded in MLflow.
- **Surface 5 end-to-end** passes on curated scenarios.
- Model routing config produces ≥ 95% of all-Premium quality at ≤ 30% of all-Premium cost.
- Every LLM call is logged to `${DBX_CATALOG}.${DBX_SCHEMA}.llm_calls`.
- With `MODEL_ENDPOINT_HOST` unset, the app runs in degraded mode with a clear "LLM unavailable" indicator.
- No hardcoded workspace URLs, catalog names, volumes, or model endpoints anywhere in source.
- `.env.example` present; `.env` gitignored.
- README documents the Optus migration path (env swap + adapter re-implementation + governed views satisfying the data contracts).
- No fake successful integrations shown to the user.
- Jira submission NOT built (deferred to Optus stage).

---

## 14. What to do first

1. Read `app.py`, `src/platform/pages.py`, `src/platform/adapters.py`, `src/platform/methodology.py`, and `src/test_catalogue.py` in full.
2. Treat this package as synthetic-data-free. If test data is needed later, introduce it deliberately rather than assuming a bundled generator exists.
3. Confirm you understand the current adapter surface and the T&E computation flow.
4. Reply with a **one-page plan for Phase 1 only**: file list, extraction approach, test approach, expected risk points. Do not proceed to Phase 2 until Phase 1 is deployed and confirmed working on the personal Free Edition workspace.

Do not touch `assets/`, `vendor/`, or `templates/` unless explicitly required. Do not change the design system. Do not add new UI patterns beyond those already in `src/platform/components.py`.

If any instruction here conflicts with what you observe in the code, ask before assuming.

Begin.
