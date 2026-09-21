# AI Audit Analyst

A domain-agnostic Internal Audit analytics platform for Databricks. An auditor selects a **Skill**
— a versioned audit methodology — points it at governed data, and runs it. The run executes
deterministic control tests, produces evidence-linked findings, and generates narrative prose
around the computed numbers. Output is findings, management actions, and PowerPoint / Excel
workpapers.

Two properties drive the design:

- **Deterministic results.** Which exceptions exist, and every number, is computed by Python.
  The model writes prose around those numbers and groups findings into themes. It never invents
  a finding, a figure or a severity. Two runs over the same data produce the same findings.
- **Everything is evidence.** Every run, finding, action, execution event and model call is
  persisted to Delta with a run ID. The workpaper has to be defensible to external audit.

## Status

**Pre-build.** `reference_app/` is a working Dash prototype with a complete UI, a 14-test T&E
control catalogue, and Excel/PowerPoint exporters — but no backend: it computes its metrics from
flag columns produced by an upstream notebook that is not in this repository, and it has never
called a model at runtime. The build replaces that with a real governed execution engine while
keeping the UI.

`CLAUDE.md` §0 documents exactly what works and what does not. Read it before trusting anything
in `reference_app/`.

## Start here

| File | What it is |
|---|---|
| **`CLAUDE.md`** | **The build brief.** Architecture, non-negotiables, phases, gates. Claude Code loads it automatically. Authoritative over this README and over every docstring in the repo. |
| `docs/SESSION_KICKOFF.md` | Paste-able prompts for starting a build session, a later phase, or a phase-gate review. |
| `docs/DATABRICKS_ARCHITECTURE.md` | Platform architecture written for a Databricks Solutions Architect — features used, criticality, fallbacks, and the governance questions that need a platform owner. |

## Starting a build session

1. Create a Claude Code environment with this repository as its source.
2. Set the environment variables in `.env.example` (host, token, catalog, schema, volume,
   warehouse path, model endpoints).
3. Allow the workspace host in the environment's network policy.
4. Set the session model to **Opus**, paste the P1 block from `docs/SESSION_KICKOFF.md`.
5. After the plan is approved, switch to **Sonnet** for implementation.

`.claude/settings.json` defaults the project to Sonnet so implementation work does not run on
Opus by accident. Three subagents are defined with pinned models: `implement` (Sonnet),
`mechanical` (Haiku), `review` (Opus).

## Architecture in one paragraph

A Databricks App hosts the UI and starts runs; a serverless Job executes the pipeline; Delta is
the only channel between them. The pipeline is a plain Python loop over nine linear nodes — no
orchestration framework. Human approval gates are job boundaries, not in-process pauses, so an
auditor can confirm a plan or sign off findings hours later from a different browser. Two model
serving endpoints handle narration and row-level classification. See `CLAUDE.md` §2.

## Repository layout

```
CLAUDE.md                  the build brief — start here
docs/                      kickoff prompts, Databricks architecture
.claude/                   Claude Code settings and model-pinned subagents
.env.example               every environment variable, documented
requirements.txt           orchestrator dependency set
scripts/setup_workspace.py Databricks catalog / schema / volume bootstrap
reference_app/             the existing Dash prototype (see CLAUDE.md §0)
├── app.py                 entry point, routing, T&E workspace
├── src/                   computation, test catalogue, exporters, platform UI
├── assets/                design system and PPTX template
└── tests/                 pytest suite
```

## Running the prototype locally

```bash
cd reference_app
pip install -r requirements.txt
python app.py            # http://localhost:8050
python -m pytest tests/
```

With no data volume configured it falls back to generated demo data. That fallback is removed in
P3 — see `CLAUDE.md` §9.
