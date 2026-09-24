"""One place for the catalogue-grain test-grouping rule (independent review
2026-09-24 gap #5). A CATALOGUE test (skills/<skill>/catalogue.yaml -- 14 for
SKILL-001) can resolve to several PLAN-grain test_results, one per primitive
instance sub-test (catalogue "T3.2a" covers plan sub-tests "T3.2a_air_dom",
"T3.2a_car_int", ...). Counting sub-tests directly over-counts (21 for
SKILL-001, not 14).

The rule, already implemented independently by `app/src/workspace_tne.py`'s
`_catalogue_test_statuses` (UI) and `orchestrator/pptx_export.py`'s test
coverage slide: a catalogue test's combined status is "exception" if ANY of
its sub-tests has status "exception"; otherwise "not_testable" if ALL of its
sub-tests are not_testable; otherwise "pass". A catalogue test whose every
sub-test is not_testable is never reported as a clean pass.

`app/src/workspace_tne.py` keeps its own copy deliberately (CLAUDE.md
working rules: it already works, leave app/ alone). `orchestrator/
pptx_export.py` and `orchestrator/narration/run_values.py` both use this
module instead of keeping their own copies."""

from __future__ import annotations

_STATUS_PRIORITY = {"exception": 0, "pass": 1, "not_testable": 2}

__all__ = [
    "catalogue_id_for",
    "results_for",
    "combined_status",
    "catalogue_test_statuses",
    "catalogue_test_counts",
]


def catalogue_id_for(plan_test_id: str, catalogue_ids: set[str]) -> str:
    """Exact match, or the catalogue id a "<id>_<suffix>" plan test_id
    belongs to. Falls back to `plan_test_id` itself when it matches no
    catalogue id."""
    if plan_test_id in catalogue_ids:
        return plan_test_id
    for cid in catalogue_ids:
        if plan_test_id.startswith(f"{cid}_"):
            return cid
    return plan_test_id


def results_for(catalogue_test_id: str, test_results: list[dict]) -> list[dict]:
    """Every plan-grain test_result belonging to `catalogue_test_id`: an
    exact test_id match, or one whose test_id starts with
    "<catalogue_test_id>_"."""
    return [
        r for r in test_results
        if r.get("test_id") == catalogue_test_id or str(r.get("test_id", "")).startswith(catalogue_test_id + "_")
    ]


def combined_status(results: list[dict]) -> dict:
    """The single result a catalogue test reports: exception over pass over
    not_testable, with exception_units summed across every exception
    sub-test (not just the winning one)."""
    if not results:
        return {}
    best = min(results, key=lambda r: _STATUS_PRIORITY.get(r.get("status"), 3))
    total_exceptions = sum(r.get("exception_units") or 0 for r in results if r.get("status") == "exception")
    return {**best, "exception_units": total_exceptions if best.get("status") == "exception" else best.get("exception_units")}


def catalogue_test_statuses(catalogue_tests: list[dict], test_results: list[dict]) -> dict[str, dict]:
    """{catalogue test_id: combined_status(...)} for every catalogue test
    that has one -- `catalogue_tests` is a catalogue.yaml `tests` list
    (each a dict with a `test_id` key), never a plan.yaml one."""
    return {
        test.get("test_id", ""): combined_status(results_for(test.get("test_id", ""), test_results))
        for test in catalogue_tests if test.get("test_id")
    }


def catalogue_test_counts(catalogue_tests: list[dict], test_results: list[dict]) -> dict[str, int]:
    """{"total", "with_exceptions", "not_testable"} at CATALOGUE grain --
    the counts a run-level narration payload must use, matching the UI's and
    the PPTX coverage slide's own counts. `total` is the number of catalogue
    tests present in `catalogue_tests`, never the (larger) number of
    plan-grain sub-tests in `test_results`."""
    statuses = catalogue_test_statuses(catalogue_tests, test_results)
    return {
        "total": len(statuses),
        "with_exceptions": sum(1 for s in statuses.values() if s.get("status") == "exception"),
        "not_testable": sum(1 for s in statuses.values() if s.get("status") == "not_testable"),
    }
