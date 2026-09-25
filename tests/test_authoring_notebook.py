"""docs/specs/P7_mapping_authoring_design.md §2.2 "Notebook" / §2.4 L-K1:
`ops/notebooks/skill_authoring.py` is a Databricks notebook source (needs a
real `dbutils`) and cannot run on a workspace cluster from here, but its
cells' LOGIC is exactly `orchestrator.authoring`'s own functions -- this
executes every cell against a stubbed `dbutils`, over the mini fixture
Skill, so a change that breaks the notebook's actual call sequence (widget
names, argument order, the optional final cell's guard) fails loudly here
rather than only being discovered on a cluster. The one Delta write (the
final cell) is skipped by leaving `record_actor` blank, exactly as an
auditor who has not yet reviewed the scores would."""

from __future__ import annotations

from pathlib import Path

NOTEBOOK_PATH = Path(__file__).parent.parent / "ops" / "notebooks" / "skill_authoring.py"
MINI = Path(__file__).parent / "fixtures" / "skills" / "mini"
REPO_ROOT = Path(__file__).parent.parent


class _Widgets:
    def __init__(self, values: dict[str, str]):
        self._values = dict(values)

    def text(self, name: str, default: str = "", label: str = "") -> None:
        self._values.setdefault(name, default)

    def dropdown(self, name: str, default: str, choices: list[str], label: str = "") -> None:
        self._values.setdefault(name, default)

    def get(self, name: str) -> str:
        return self._values[name]


class _DBUtils:
    def __init__(self, values: dict[str, str]):
        self.widgets = _Widgets(values)


def _cells(source: str) -> list[str]:
    body = source.split("# Databricks notebook source", 1)[-1]
    return [c for c in body.split("# COMMAND ----------") if c.strip()]


def test_notebook_cells_run_against_the_mini_fixture(tmp_path: Path):
    fixtures_dir = tmp_path / "fixtures"
    widget_values = {
        "repo_root": str(REPO_ROOT),
        "skill_dir": str(MINI),
        "do_scaffold": "no",
        "plants_path": str(MINI / "plants.yaml"),
        "fixtures_dir": str(fixtures_dir),
        "record_actor": "",
    }
    namespace: dict = {"dbutils": _DBUtils(widget_values), "__file__": str(NOTEBOOK_PATH)}

    source = NOTEBOOK_PATH.read_text()
    for cell in _cells(source):
        if cell.strip().startswith("# MAGIC"):
            continue
        exec(compile(cell, str(NOTEBOOK_PATH), "exec"), namespace)  # noqa: S102 -- trusted repo file

    assert namespace["report"].ok, namespace["report"].to_dict()
    assert namespace["written"]  # {source: rows}
    assert "T1" in namespace["scores"]
    assert (fixtures_dir / "claims.csv").is_file()
    assert (fixtures_dir / "register.csv").is_file()
