# Session kickoff prompts

Paste the relevant block as the first message of a new Claude Code session.
`CLAUDE.md` loads automatically; these prompts tell the session what to do with it.

---

## P1A — first session of the build (run on **Opus**, switch to Sonnet after the plan)

```
Read CLAUDE.md in full before doing anything else. It is the authoritative build brief
and it supersedes the README, every docstring in this repo, and anything you may have
seen in an earlier version of the brief.

This is P1A of a phased build (the immutable run ledger). Work in this order and stop
where told.

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
Then state back in your own words: (a) why computation.py -- the best available behavioural
reference -- was never wired into app.py, (b) what app.py does instead and why that cannot
continue, (c) which behaviours in the prototype were correct for a demo but must not survive
into a governed audit run, (d) which known defects in computation.py (§0.2) must not be
reproduced when porting. If you cannot explain all four, re-read CLAUDE.md §0.2 before
continuing. The prototype was built under real constraints; read that section as context,
not as a list of mistakes.

STEP 3 — Produce a one-page plan for P1A ONLY (the immutable run ledger):
  - RunState: every field, with a one-line justification each, and why it is JSON-safe
  - The run fingerprint: run_fingerprints table with source version/hash, Skill content hash,
    code revision, dependency lock hash, runtime config hash, endpoint config, prompt template
    version. Per-LLM-call provenance (served model version, tokens) deferred to llm_calls (P6).
  - Optimistic concurrency: state_version on every transition, CAS enforcement
    (zero-rows-affected = rejected), deterministic node execution keys, idempotent MERGE into
    node_attempts
  - Explicit run-status state machine: all valid states and transitions, including
    invalid-transition rejection
  - Delta DDL outline for runs, run_state, run_fingerprints, node_attempts, trace_events
  - PersistenceAdapter contract, and how LocalPersistence and DeltaPersistence both satisfy it
  - The reaper: marks `interrupted`, never deletes (audit runs are evidence)
  - Test approach, including the JSON round-trip test, failure-injection test (CAS + idempotent
    MERGE), and one trivial end-to-end run
  - Risks and open questions

P1B (engagement scoping + suite tables) is a separate plan after P1A passes.

STEP 4 — STOP. Present the plan. Do not write code until I approve it.

After I approve: switch to Sonnet (/model claude-sonnet-5) or delegate to the `implement`
subagent, and build P1A. Do not start P1B.

Constraints that override any instinct to be helpful:
  - Do not introduce LangGraph or any orchestration framework.
  - Do not preserve the silent-default behaviour in app.py's _standardise_* or _find_col.
  - computation.py is the best available behavioural reference for detection logic, but it
    contains known semantic defects (§0.2). Do not reproduce them. P2 ports against an
    audit-approved test specification, not against computation.py output.
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
