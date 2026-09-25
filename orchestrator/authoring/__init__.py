"""The Skill authoring kit (docs/specs/P7_mapping_authoring_design.md §2,
build-here item 5): scaffold a new Skill, validate it (collect-all, plus an
optional dry-run Surface 2 pass), generate planted-exception fixtures from
a hand-authored sidecar, and score them -- so a new Skill is YAML-only work,
checked from a notebook or `python -m orchestrator.authoring`. No UI
(D-P7-11). Submodules import lazily from one another where noted (checks.py
<-> score.py) so a B2a-only or B2b-only checkout still imports cleanly."""
