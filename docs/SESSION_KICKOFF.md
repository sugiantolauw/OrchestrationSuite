# Session kickoff prompts

Paste the relevant block as the first message of a new Claude Code session.
`CLAUDE.md` loads automatically; these prompts tell the session what to do with it.

---

## P1 — first session of the build (run on **Opus**, switch to Sonnet after the plan)

```
Read CLAUDE.md in full before doing anything else. It is the authoritative build brief
and it supersedes the README, every docstring in this repo, and anything you may have
seen in an earlier version of the brief.

This is P1 of a phased build. Work in this order and stop where told.

STEP 1 — Verify the environment. Run the checks in CLAUDE.md §11:
  - WorkspaceClient().current_user.me()
  - serving_endpoints.list(), catalogs.list(), warehouses.list()
If the SDK import panics on `cryptography`, fix it per §11 (venv or pinned upgrade).
If any call fails with a proxy 403 or 502, do NOT work around it — report it and stop.
Also confirm in the workspace UI that Databricks Apps, a serverless SQL warehouse, and
pay-per-token model serving all exist. The architecture assumes all three. Jobs is NOT
required -- compute runs inside the App (CLAUDE.md 2.1). If any of the three is missing,
STOP AND REPORT before designing anything.

STEP 2 — Read the code. In full, not skimmed:
  reference_app/app.py, src/computation.py, src/test_catalogue.py,
  src/platform/adapters.py, src/platform/pages.py
Then state back in your own words: (a) why computation.py -- the canonical detection
engine -- was never wired into app.py, (b) what app.py does instead and why that cannot
continue, (c) which behaviours in the prototype were correct for a demo but must not survive
into a governed audit run. If you cannot explain all three, re-read CLAUDE.md §0.2 before
continuing. The prototype was built under real constraints; read that section as context,
not as a list of mistakes.

STEP 3 — Produce a one-page plan for P1 ONLY:
  - RunState: every field, with a one-line justification each, and why it is JSON-safe
  - Delta DDL outline for runs, run_state, findings, management_actions, trace_events,
    uploaded_files, llm_calls, llm_cache, narrative_edits, evaluation_runs
  - PersistenceAdapter contract, and how LocalPersistence and DeltaPersistence both satisfy it
  - The reaper, and the status state machine
  - Test approach, including the JSON round-trip test
  - Risks and open questions

STEP 4 — STOP. Present the plan. Do not write code until I approve it.

After I approve: switch to Sonnet (/model claude-sonnet-5) or delegate to the `implement`
subagent, and build P1. Do not start P2.

Constraints that override any instinct to be helpful:
  - Do not introduce LangGraph or any orchestration framework.
  - Do not preserve the silent-default behaviour in app.py's _standardise_* or _find_col.
  - computation.py is the canonical detection engine. Restore it; do not rewrite it, and do
    not treat app.py's flag-counting as a second valid implementation to choose between.
  - Do not port any hardcoded result value from computation.py (the 132, the 0, the 2,
    the 152,921, the 500 fallback). Each becomes a real computation or an explicit
    not_testable with a reason string.
  - Do not push to any remote unless I ask.
```

---

## Later phases (run on **Sonnet** unless the table in CLAUDE.md §10 says Opus)

```
Read CLAUDE.md in full. We are starting P<N>.

Re-read §<relevant sections> and the definition of done for P<N> in §8.
Confirm P<N-1>'s definition of done still holds (run the tests).

Produce a task list for P<N> only, then implement it. Run pytest and py_compile before
every commit. Commit as [P<N>] <type>(<scope>): <summary>.

Stop and report when P<N>'s definition of done is met. Do not start P<N+1>.
```

---

## Phase-gate review (run on **Opus**)

```
Read CLAUDE.md. Review the work done in P<N> against its definition of done in §8.

Read the diff since the last phase tag. For each item in P<N>'s DoD, state whether it is
met, partially met, or not met, with file:line evidence. Run the phase's gates yourself —
do not trust a summary.

Then list, in priority order, anything that will cost us later if not fixed now.
Be blunt. Do not write code.
```
