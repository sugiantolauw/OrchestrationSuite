# AI Audit Analyst — Claude Code cloud handoff

This folder is a handoff bundle for building the next iteration of AI Audit Analyst in Claude Code through the Claude website. It is packaged around the current working `tne_exco_app` prototype and excludes the synthetic-data workstream for now.

## What's in this folder

| File / folder | Purpose |
|---|---|
| `CLAUDE.md` | Primary build brief for Claude Code. |
| `README.md` | This file. Scope, folder map, and browser-Claude workflow. |
| `.env.example` | Placeholder environment variables for the eventual Databricks target workspace. |
| `.gitignore` | Standard Python + local-secret exclusions. |
| `requirements.txt` | High-level dependency set for the orchestrator build. |
| `scripts/setup_workspace.py` | Bootstrap script for the target Databricks workspace catalog, schema, volume, and Delta tables. |
| `prompts_seed/` | Placeholder location for starter prompt templates if Claude creates them. |
| `reference_app/` | Snapshot of the current `tne_exco_app` codebase for Claude to extend rather than rebuild. |

## Intended use in Claude Code on the web

1. Create a new Claude Code project in the browser.
2. Upload this entire folder.
3. Keep `CLAUDE.md` at the project root so Claude picks it up as the working brief.
4. Work inside `reference_app/` as the starting codebase unless you deliberately move it back to the root of the project.
5. Add a real `.env` only when you are ready to point the build at a specific Databricks workspace.

## Scope of this bundle

- Includes the current app snapshot as the baseline implementation.
- Includes the build brief and workspace bootstrap script.
- Excludes all synthetic-data generators, contracts, source files, outputs, and sample datasets.
- Excludes real Optus data, credentials, and Optus-specific identifiers.

## Practical notes for the next Claude session

- The starting point is an existing Dash app, not a greenfield build.
- Preserve the current UI, routes, and design system.
- Focus the next build stage on LangGraph orchestration, adapters, persistence, and governed Databricks integration.
- Treat all workspace-specific values as configuration only.

## What Claude should not assume

- No synthetic dataset package is present in this handoff.
- No local laptop paths or local shell wrappers should be assumed.
- No deployment wrapper script is included yet.
- No Optus connectivity should be assumed until you provide environment values.
