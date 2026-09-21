# Product positioning — target shape and the gap to close

**Purpose:** record what the platform is aiming at, measured against PwC's Internal Audit
Orchestration Suite, and which differences are gaps to close versus deliberate divergences.
**Source:** PwC's public product page for the Internal Audit Orchestration Suite, read 2026-09-21.
Everything in §1 is from that page; §2 onward is our analysis.

---

## 1. What PwC's suite is

A *"modular, AI-first platform"* delivered in waves, organised around the internal audit
lifecycle. It *"embeds PwC domain expertise into AI agents, accelerating execution while helping
keep auditors firmly in control."* Modules can be taken standalone or as an integrated platform.

| Module | Status | Stated capability |
|---|---|---|
| **Fieldwork** | Shipped | Two parts. **Dynamic Testing** — develops test procedures and control attributes, organises and analyses supporting evidence, identifies potential exceptions *"with clear linkage to source documentation"*. **Control design assessment AI toolkit** (part of Risk Link) — assesses alignment between risks and controls, identifies design gaps, documents conclusions with supporting rationale. |
| **Risk Assessment** | Coming soon | Uses quantitative and qualitative inputs; AI *"assists in identifying, scoring, and prioritizing risks based on impact and materiality"*, moving beyond *"static, point-in-time assessments"*. |
| **Audit Planning** | Coming soon | From prioritised risks, AI generates *"risk and control Matrices (RCMs), tailored audit scope memos, control test steps, and document request lists aligned to agreed templates"*. |
| **Risk Sensing** | Coming soon | Connects to *"internal and external data sources"*; agents identify key risk indicators and surface patterns or anomalies; insights feed risk assessment and planning. |
| **Audit Reporting & Issues** | Coming soon | Aggregates testing results, risk insights and issues into structured reports and dashboards. An *"issue management agent"* drafts issue descriptions and remediation plans. Leadership visibility into status, emerging themes and remediation progress. |

Governance posture throughout: *"auditors guide the review, assess outputs, and apply professional
judgment"*; *"preserving strong human oversight and auditor-led governance"*.

---

## 2. The structural difference

**PwC's suite is organised around the audit lifecycle. Ours is currently organised around a single
analytic run.**

That is a deliberate sequencing choice — get one lifecycle stage genuinely working before spanning
five — not a disagreement about the target. `CLAUDE.md` §4.8 and §4.9 exist so the lifecycle model
can be adopted without a migration.

| PwC module | Our equivalent today | Assessment |
|---|---|---|
| Fieldwork — Dynamic Testing | `execute` → `classify` → `find` | **Different substrate** (§3.1) |
| Fieldwork — control design assessment | none | Gap. `assertion: design` is reserved (§4.9) but nothing implements it |
| Audit Planning | Explorer Mode | Partial — produces a test plan; no scope memo, no document request list. RCM comes free once risks/controls are modelled |
| Reporting & Issues | `export`, `act`, management actions | Closest match. `issues` table added in P1 (§4.9) |
| Risk Assessment | none | Gap — upstream of everything we do |
| Risk Sensing | none | Gap — headroom only (§4.9), deliberately unbuilt |

Our human-in-the-loop gates and evidence traceability are a strong match for their
*"auditor-led governance"* positioning — arguably stronger, since ours are enforced in the schema
and in CI gates rather than described in marketing copy.

---

## 3. Three substantive differences

### 3.1 Document-centric vs data-centric — a divergence, not a gap

Dynamic Testing *"organises and analyses supporting evidence"* with *"clear linkage to source
documentation"*. It reads invoices, contracts and approvals — AI-assisted sample-based testing.

Ours tests structured tables across the **full population**. "We tested every transaction in the
period" is a stronger statement to an audit committee than "we tested a sample and AI read the
evidence", and it is reproducible in a way document interpretation is not.

**Treat this as our differentiator, not a deficiency.** If document handling is wanted later it is
a subsystem of its own — ingestion, extraction, evidence linking — and should be scoped as such,
not bolted onto the pipeline.

### 3.2 Design vs operating effectiveness — a real gap

Every test in the current catalogue asks *did the control work on these transactions* (operating
effectiveness). PwC ships a separate toolkit for *is this control capable of preventing the risk*
(design). They are different conclusions and both are reported.

Mitigation in place: `assertion: design | operating` is mandatory on every test from P2 (§4.9), so
the distinction is recorded from the first run even though only operating tests exist. Design
assessment itself is unscheduled.

### 3.3 Risk-first vs data-first — the gap that shapes everything

Their lifecycle starts from a risk register: sense risks → score them → plan audits against them →
test the controls. Ours starts from data: pick a dataset → run tests → get findings.

Their `prioritise` ranks **risks**; ours ranks **findings**. Different objects entirely.

Mitigation in place: §4.9 adds `risks`, `controls` and `risk_assessments` in P1, and requires every
test to cite the control and risk it addresses. That makes the chain
**Risk → Control → Test → Finding → Issue → Action** complete in the schema from the first commit,
even though only the middle is exercised.

---

## 4. Roadmap implication

Not five modules. Two moves capture most of the positioning:

1. **Model risks and controls now** (P1–P2, §4.9). Delivers an RCM view — the central artifact of
   audit planning — makes findings traceable to risks, and gives Risk Assessment and Risk Sensing
   somewhere to write when they arrive. Roughly half a day of schema work.
2. **Extend Explorer Mode** (post-P8). It already produces a test plan from an objective and a data
   profile; adding a scope memo and a document request list is a prompt and a template, not a
   subsystem. That closes most of the Audit Planning gap.

Deliberately deferred: risk sensing implementation (governance timeline, and it is PwC's
least-shipped module too), risk scoring methodology (the enterprise risk function's existing
methodology must dictate it — a second conflicting methodology is worse than none), and
document-centric evidence testing (a subsystem, and arguably not where our advantage lies).

---

## 5. Honest summary

We are building one lifecycle stage — fieldwork — with better determinism, traceability and
evaluation discipline than a marketing page can claim, on a substrate (full-population analytics
over governed data) that differs from theirs by design. The lifecycle model is the destination;
the schema work in `CLAUDE.md` §4.8–4.9 is what keeps the road open.
