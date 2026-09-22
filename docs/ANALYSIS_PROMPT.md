# Frontier LLM Analysis Prompt — AI Audit Analyst Platform

Use this prompt with any frontier LLM (GPT-4o, Gemini, Claude, etc.) to get an independent
analysis of the build plan. Either give the model access to the repo, or paste the listed files
after the prompt.

---

## Prompt

I'm building an AI-powered Internal Audit analytics platform on Databricks. The goal is to
match the capability of PwC's Internal Audit Orchestration Suite
(https://www.pwc.com/us/en/services/consulting/risk-regulatory/enterprise-risk-management-controls/internal-audit-services/internal-audit-orchestration-suite.html)
— starting with a working prototype on Databricks Free Edition, then porting to a corporate
Databricks environment.

A working Dash prototype already exists (built by an agentic coding tool under constraints).
The build brief describes turning it into a governed, auditable platform with a real execution
engine. The brief was written collaboratively with an AI assistant across a multi-hour
architecture session.

I need you to analyse the plan critically. I want to know what's strong, what's weak, what's
missing, and what will break.

### Repository

**GitHub:** https://github.com/sugiantolauw/OrchestrationSuite

### Files to read (in this order)

Read these files in full before answering. Each one is essential context.

| File | What it is | Why it matters |
|---|---|---|
| `CLAUDE.md` | The build brief (~1,380 lines). The single source of truth for the entire build | Contains the architecture, non-negotiables, pipeline design, evaluation gates, model-serving strategy, 10 phases with definitions of done, and all open questions |
| `reference_app/app.py` | The existing Dash prototype (~2,468 lines) | Shows what already works, what's wired up, and what's a workaround |
| `reference_app/src/computation.py` | The canonical detection engine (~692 lines) | Real detection logic for 14 T&E audit tests — the code the build restores |
| `reference_app/src/narrative.py` | The intended LLM layer (~173 lines) | Never wired in (no model serving). Test-ID framework and tone spec for future prompts |
| `reference_app/src/pptx_export.py` | PPTX export (~596 lines) | Current export with known defects documented in the brief |
| `docs/DATABRICKS_ARCHITECTURE.md` | Architecture doc for a Databricks Solutions Architect | Platform features used, data flow, governance trail, prioritised SA questions |
| `docs/PRODUCT_POSITIONING.md` | Gap analysis vs PwC's Orchestration Suite | Where we match, where we differ, recommended moves |
| `docs/SESSION_KICKOFF.md` | The prompt that will be used to start the P1 build session | Shows how the next builder will be briefed |
| `.claude/settings.json` | Claude Code config | Model pinning, subagent definitions, permission allowlist |
| `.env.example` | All environment variables | What the runtime expects |

### What I need from you

Produce **six sections**. Be blunt and specific — cite file names, line numbers, section
numbers. Don't soften things.

---

#### 1. Plan quality assessment

Grade the brief on a scale of 1–10 across these dimensions, with one sentence justifying each:

| Dimension | What you're grading |
|---|---|
| Completeness | Does it cover everything a builder needs to start without guessing? |
| Internal consistency | Do the sections agree with each other? Are there contradictions? |
| Feasibility | Can this actually be built in the stated phases with the stated constraints? |
| Risk awareness | Does the brief acknowledge the real risks, or does it wave them away? |
| Specificity | Are deliverables and gates concrete enough to be tested, or are they vague? |
| Prioritisation | Is the build order right? Are the dependencies correct? |

#### 2. Architecture critique

Evaluate the key technical decisions. For each, say whether you agree, disagree, or think it's
a bet that needs a fallback:

- **No orchestration framework** (plain Python loop over 9 nodes, not LangGraph/LlamaIndex)
- **Apps-only compute** (ThreadPoolExecutor inside the Dash process, no Jobs for the pipeline)
- **Delta as state store** (RunState serialised to Delta, not a database or Redis)
- **findings.yaml** (declarative rules with trigger expressions, restricted evaluator — not eval())
- **Two-model strategy** (Sonnet 4.5 for prose, GPT-OSS-120B for batch/judge)
- **Explorer mode** (LLM composes primitives, cannot emit code or SQL)
- **Authoring-time vs runtime LLM split** (LLM authors rules at Skill creation, runtime LLM only narrates)
- **Response cache keyed on prompt hash** (reproducibility via caching, not temperature=0)

#### 3. What will break

The five things most likely to cause rework, blocked phases, or a prototype that fails when
real data arrives. For each:

1. **What goes wrong** — be specific
2. **Which phase it surfaces in**
3. **Why the brief doesn't prevent it** — what's missing or underspecified
4. **What to do about it** — a concrete change to the plan, not "think about it more"

#### 4. What's missing from the brief

Things a build brief of this scope should cover that this one doesn't. Consider:

- Error recovery and partial failures (node 4 of 9 fails — then what?)
- Data lineage end-to-end (PPTX figure → finding → metric → source row)
- Multi-engagement isolation and concurrent runs
- The actual migration path from Free Edition to corporate Databricks
- Security beyond secrets (prompt injection via uploaded data, PII in model calls, PII in exports)
- Observability and alerting beyond MLflow tracing
- Performance budgets (how fast must a run complete? what's the latency budget for the UI?)
- Data quality framework (what happens when source data is malformed, not just missing?)
- Rollback and versioning (can you revert to a previous Skill version? previous findings?)

#### 5. Comparison to PwC's Orchestration Suite

Based on the PRODUCT_POSITIONING.md and the brief:

- Where does this plan credibly match PwC's offering?
- Where is the gap unbridgeable without enterprise resources?
- What's the smartest order to close the gaps — which capabilities give the most credibility
  with the least build effort?
- Is the "data-first" vs PwC's "document-centric" distinction real, or is it a rationalisation
  for not having document processing yet?

#### 6. If you were the builder

If you were the AI assistant starting the P1 build session tomorrow:

- What would you clarify before writing any code?
- What would you change about the P1 scope?
- What would you push back on?
- What's the single most important thing to get right in P1 that the brief underweights?

---

## After receiving the analysis

1. Save the response alongside this file for the record (or in the "Review responses" section
   of `docs/REVIEW_PROMPT.md`).
2. For any structural contradictions or stale sections identified, fix them in `CLAUDE.md`
   before starting P1.
3. For missing areas, decide: does it go in the brief, in the phase's planning session, or is
   it a conscious deferral? Note the decision.
4. For the "what will break" items, add mitigations to the relevant phase's definition of done
   if they're actionable now.
