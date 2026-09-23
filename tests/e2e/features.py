"""Feature inventory for the reference_app (prototype) Dash UI.

This is the executable checklist CLAUDE.md §9B Layer 3 asks for: every
user-visible feature of the prototype, captured once as data, so the same
list can later be pointed at the rebuilt (P3-P5) app to prove
"functionally identical on /workspace/tne" (CLAUDE.md §3 non-negotiable 15,
§8 P4 DoD).

Each entry is (feature_id, route, description). `test_feature_coverage.py`
asserts every id here is referenced by at least one test in this package.
Do not delete an id without also removing the behaviour it names; do not
add a test that references an id not listed here (it would silently not be
checked for coverage).
"""

from __future__ import annotations

FEATURES: list[tuple[str, str, str]] = [
    # ── Routes render without error (CLAUDE.md §9B Layer 3 "Upload fixture / render") ──
    ("R1", "/", "Landing page ('Start an Audit') renders without a Dash error overlay or console error"),
    ("R2", "/workspace/tne", "T&E workspace renders without a Dash error overlay or console error"),
    ("R3", "/runs", "Audit Runs page renders without a Dash error overlay or console error"),
    ("R4", "/skills", "Skill Library page renders without a Dash error overlay or console error"),
    ("R5", "/skills/SKILL-001", "Skill methodology viewer renders the real T&E methodology (non-stub)"),
    ("R6", "/skills/SKILL-002..005", "Skill methodology viewer renders stub methodology for non-T&E skills"),
    ("R7", "/actions", "Management Actions page renders without a Dash error overlay or console error"),
    ("R8", "/trace", "Platform Trace page renders without a Dash error overlay or console error"),
    ("R9", "/no-such-route", "Unknown path falls back to the landing page (routing default), recorded not assumed"),

    # ── Navigation ──
    ("N1", "/", "Every platform nav link (Start an Audit, T&E Showcase, Audit Runs, Skill Library, "
                 "Management Actions, Platform Trace) is present and navigates to its route"),

    # ── /workspace/tne: top-level and nested tabs ──
    ("W1", "/workspace/tne", "Top-level tabs (Executive Brief, Findings & Actions, Audit Detail) switch content"),
    ("W2", "/workspace/tne", "Nested Findings & Actions tabs (Findings & Evidence, Management Actions) switch content"),
    ("W3", "/workspace/tne", "Nested Audit Detail tabs (Executive analysis, Detailed risk, Receipt & approver "
                              "review, Test catalogue) switch content"),
    ("W4", "/workspace/tne", "Audience-mode dropdown (Executive/Audit Manager/Investigator) switches the active "
                              "top-level tab per switch_audience_mode()"),

    # ── /workspace/tne: filters driving dependent charts/tables ──
    ("W5", "/workspace/tne", "Executive analysis (page1) date-range + ExCo member filters update KPIs, charts "
                              "and the missing-attendee table without a callback error"),
    ("W6", "/workspace/tne", "Detailed risk (page2) date-range + member + expense-type filters update KPIs, "
                              "charts and tables without a callback error"),
    ("W7", "/workspace/tne", "Receipt & approver review (page3) date-range + approver filters update KPIs, "
                              "charts and the approval detail table without a callback error"),

    # ── /workspace/tne: findings ──
    ("W8", "/workspace/tne", "Findings risk/category/sort filters update the filtered-findings-container"),
    ("W9", "/workspace/tne", "Findings view shows the top 3 priority findings plus a collapsed "
                              "'Show N additional findings' disclosure for the rest"),
    ("W10", "/workspace/tne", "'View exceptions' opens the exception drill-down drawer with row-level records"),
    ("W11", "/workspace/tne", "'View evidence' opens the evidence drawer with the metrics-cited table"),
    ("W12", "/workspace/tne", "Test catalogue table renders all 14 T&E tests"),
    ("W13", "/workspace/tne", "Management action modal: 'Edit' opens it pre-filled, 'Save action' updates the "
                               "tracker row and appends an audit-log entry"),
    ("W14", "/workspace/tne", "'Copy' on a finding card fires the clientside clipboard callback and opens the "
                               "confirmation toast"),

    # ── Exports ──
    ("X1", "/workspace/tne", "'Export PPTX' downloads a valid .pptx with slides and native charts"),
    ("X2", "/workspace/tne", "'Export Excel' downloads a valid .xlsx with the expected sheets"),

    # ── Platform pages: fixture-backed tables/cards ──
    ("P1", "/runs", "Audit Runs page renders one run-card per DEMO_AUDIT_RUNS fixture row, with KPI summary tiles"),
    ("P2", "/skills", "Skill Library renders one skill-card per DEMO_SKILLS fixture row, each linking to its "
                       "methodology page; filter dropdowns render (not wired to a callback in this prototype)"),
    ("P3", "/skills/SKILL-001", "Methodology viewer's section nav (Overview, Data sources, Test catalogue, "
                                 "Risk scoring, Outputs & evidence, Version history) all render content"),
    ("P4", "/actions", "Management Actions page renders one row per DEMO_MANAGEMENT_ACTIONS fixture row, "
                        "with KPI summary tiles"),
    ("P5", "/trace", "Platform Trace page renders one row per DEMO_TRACE_EVENTS fixture row"),
    ("P6", "/", "Jira ticket preview (create_jira_preview) exists in the adapter layer but is not reachable "
                "from any UI control in this prototype — recorded, not exercised as a UI flow"),

    # ── Landing page / platform workflow ──
    ("L1", "/", "Demo-data indicator is visible on the landing page (adapters.is_demo_mode() is True)"),
    ("L2", "/", "Governed-data search input filters the data-asset cards via search_data_assets()"),
    ("L3", "/", "File-upload dropzone renders (not wired to a callback in this prototype)"),
    ("L4", "/", "Proposed-workflow preview renders stage rows from adapters.propose_plan()"),
    ("L5", "/", "Run summary preview panel renders (mode, Skill, data mode, outputs)"),
    ("L6", "/", "'Start audit analysis' navigates to /workspace/tne (start_demo_run callback)"),
    ("L7", "/", "Landing-page skill cards render with working 'View methodology' links"),
]

FEATURE_IDS = {f[0] for f in FEATURES}
