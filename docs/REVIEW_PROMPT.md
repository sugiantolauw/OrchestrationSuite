# External Review Prompt — Build Brief v2

Paste the entire contents of `CLAUDE.md` after this prompt. The reviewer should be a strong
model (Opus or equivalent) with no prior context about this project.

---

## Prompt

You are reviewing a build brief for an AI-powered Internal Audit analytics platform being built
on Databricks. The brief was collaboratively written by a human audit professional and an AI
assistant across a multi-hour architecture session. It is the sole source of truth for the build.

Read the entire brief, then produce **four sections**. Be direct. If something is wrong, say so;
if something is missing, name it; if something is overengineered for a prototype, say that too.

### 1. Structural integrity

Does the brief hold together as a buildable specification?

- Are there contradictions between sections (e.g., a phase references something another section
  says was removed)?
- Are the non-negotiables (section 3) actually enforced by the phase definitions of done?
- Is any section stale — describing something the rest of the brief has moved past?
- Are the `RunState` fields, the primitives, the evaluation gates, and the phase deliverables
  consistent with each other?

### 2. Architecture risk

Evaluate the technical decisions against the stated constraints (Apps-only compute, two model
endpoints, no LangGraph, plain Python loop, Delta as state store, MLflow as tracer).

- Is the Apps-only execution model (ThreadPoolExecutor inside the Dash process) sound, or will
  it hit walls that the brief doesn't acknowledge? The brief includes a trade-off analysis in
  section 2.5 — evaluate whether it is honest.
- Is the `findings.yaml` approach (declarative rules with trigger expressions, evaluated by a
  restricted evaluator) a real improvement over the frozen-template status quo, or is it just
  moving the complexity?
- Are there scaling, reliability, or governance risks that the brief downplays?
- Is the three-workload classification (deterministic pipeline, structured generation, bounded
  research) a useful distinction or a false taxonomy?

### 3. What is missing

What should a brief of this scope cover that this one does not?

Consider:
- Error recovery and partial-failure handling (what happens when node 4 of 9 fails?)
- Data lineage beyond the run (can you trace a PPTX figure back to a source row?)
- Multi-tenancy / multi-engagement isolation
- Upgrade path from prototype to production (the brief says "port to Optus" — what does that
  actually require?)
- Security beyond secrets management (e.g., prompt injection via uploaded data, PII in findings)
- Observability and alerting (beyond MLflow tracing)
- The gap between the current prototype (described in section 0) and what P1 actually delivers —
  is P1's scope realistic for a single phase?

### 4. Top 5 things that will bite hardest

Rank the five most likely failure modes during the build — not theoretical risks, but the things
that will actually cause rework, blocked phases, or a prototype that doesn't hold up when real
data arrives.

For each, state:
1. What goes wrong
2. Which phase it surfaces in
3. What the brief should say (or say differently) to prevent it

---

## How to use this

1. Open a fresh conversation with a strong model (Opus, or GPT-4o for cross-family perspective).
2. Paste this prompt first, then the full `CLAUDE.md` below it.
3. Save the response alongside this file for the record.
4. If the reviewer identifies structural contradictions or stale sections, fix them in `CLAUDE.md`
   before starting P1.
5. If it identifies missing areas, decide whether they belong in the brief or in the relevant
   phase's planning session, and note the decision here.

## Review responses

(Paste responses below as they are received, with the model and date.)
