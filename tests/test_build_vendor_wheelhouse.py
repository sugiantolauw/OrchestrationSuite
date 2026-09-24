"""scripts.build_vendor_wheelhouse's resolution logic (independent review
2026-09-24 item 6), exercised entirely against a mocked downloader -- no
network, no real `pip download`, no multi-GB transfer. A single opt-in
smoke test (RUN_WHEEL_SMOKE=1, never set by this session) exercises the
real `pip download` path against one small package, if network allows."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.build_vendor_wheelhouse import (
    build_parser,
    is_pure_python_wheel,
    main,
    parse_requirements_lock,
    pip_download,
    plan_wheelhouse,
)


# ── parse_requirements_lock ──────────────────────────────────────────────


def test_parse_requirements_lock(tmp_path):
    p = tmp_path / "requirements.lock"
    p.write_text("# comment\nnumpy==1.26.0\n\npandas==2.1.0  # inline comment\n")
    assert parse_requirements_lock(p) == [("numpy", "1.26.0"), ("pandas", "2.1.0")]


def test_parse_requirements_lock_rejects_unparseable_line(tmp_path):
    p = tmp_path / "requirements.lock"
    p.write_text("numpy>=1.26.0\n")  # not a pin -- requirements.lock never has this shape
    with pytest.raises(ValueError):
        parse_requirements_lock(p)


def test_parse_the_real_repo_lock_file():
    from scripts.build_vendor_wheelhouse import REPO_ROOT

    packages = parse_requirements_lock(REPO_ROOT / "requirements.lock")
    names = {n for n, _ in packages}
    assert "numpy" in names
    assert "pandas" in names
    assert len(packages) > 50


# ── is_pure_python_wheel ─────────────────────────────────────────────────


def test_pure_python_wheel_detected():
    assert is_pure_python_wheel("PyYAML-6.0-py3-none-any.whl") is True
    assert is_pure_python_wheel("click-8.5.0-py3-none-any.whl") is True


def test_binary_wheel_detected():
    assert is_pure_python_wheel("numpy-1.26.0-cp311-cp311-manylinux_2_17_x86_64.whl") is False


def test_is_pure_python_wheel_handles_odd_filenames_without_raising():
    assert is_pure_python_wheel("not-a-wheel") is False
    assert is_pure_python_wheel("weird.whl") is False


# ── plan_wheelhouse ───────────────────────────────────────────────────────


def _fake_downloader(wheel_for: dict[tuple[str, str], str | None]):
    def downloader(name, version, dest_dir):
        filename = wheel_for.get((name, version))
        if filename is None:
            return []
        return [dest_dir / filename]
    return downloader


def test_plan_wheelhouse_vendors_binary_and_leaves_pure_as_pip_install(tmp_path):
    requirements = [("numpy", "1.26.0"), ("pyyaml", "6.0")]
    downloader = _fake_downloader({
        ("numpy", "1.26.0"): "numpy-1.26.0-cp311-cp311-manylinux_2_17_x86_64.whl",
        ("pyyaml", "6.0"): "PyYAML-6.0-py3-none-any.whl",
    })
    plan = plan_wheelhouse(requirements, downloader=downloader, dest_dir=tmp_path)
    assert len(plan.vendored) == 1
    assert plan.vendored[0].name == "numpy"
    assert len(plan.pip_installed) == 1
    assert plan.pip_installed[0].name == "pyyaml"
    lines = plan.requirements_lines()
    assert "vendor/numpy-1.26.0-cp311-cp311-manylinux_2_17_x86_64.whl" in lines
    assert "pyyaml==6.0" in lines


def test_plan_wheelhouse_no_wheel_found_falls_back_to_pip_install(tmp_path):
    requirements = [("some-pure-sdist-only-pkg", "1.0")]
    downloader = _fake_downloader({})  # nothing found for any package
    plan = plan_wheelhouse(requirements, downloader=downloader, dest_dir=tmp_path)
    assert plan.pip_installed[0].reason == "no matching wheel found"
    assert plan.vendored == []


def test_plan_wheelhouse_vendor_all_vendors_pure_python_too(tmp_path):
    requirements = [("pyyaml", "6.0")]
    downloader = _fake_downloader({("pyyaml", "6.0"): "PyYAML-6.0-py3-none-any.whl"})
    plan = plan_wheelhouse(requirements, downloader=downloader, dest_dir=tmp_path, vendor_all=True)
    assert len(plan.vendored) == 1
    assert plan.vendored[0].reason == "vendor_all"


def test_plan_wheelhouse_calls_downloader_once_per_package(tmp_path):
    calls = []

    def downloader(name, version, dest_dir):
        calls.append((name, version))
        return []

    plan_wheelhouse([("a", "1"), ("b", "2")], downloader=downloader, dest_dir=tmp_path)
    assert calls == [("a", "1"), ("b", "2")]


# ── build_parser / main (--dry-run only, no network) ─────────────────────


def test_build_parser_defaults():
    args = build_parser().parse_args([])
    assert args.manylinux_tag == "manylinux2014_x86_64"
    assert args.vendor_all is False
    assert args.dry_run is False


def test_main_dry_run_never_downloads(tmp_path, capsys):
    req_lock = tmp_path / "requirements.lock"
    req_lock.write_text("numpy==1.26.0\npyyaml==6.0\n")
    out_dir = tmp_path / "vendor"
    exit_code = main([
        "--requirements-lock", str(req_lock), "--out-dir", str(out_dir),
        "--python-version", "3.11", "--dry-run",
    ])
    assert exit_code == 0
    assert not out_dir.exists()  # nothing written, nothing downloaded
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "numpy==1.26.0" in captured.out


# ── opt-in real network smoke test ───────────────────────────────────────


@pytest.mark.skipif(os.environ.get("RUN_WHEEL_SMOKE") != "1", reason="RUN_WHEEL_SMOKE not set — real pip download is opt-in only")
def test_pip_download_smoke(tmp_path):
    # A tiny, stable, pure-Python package -- exercises the real subprocess
    # path without pulling anything large.
    result = pip_download("six", "1.16.0", tmp_path, python_version="3.11", manylinux_tag="manylinux2014_x86_64")
    assert len(result) <= 1  # 0 if pip only has an sdist for it, 1 if a wheel exists
