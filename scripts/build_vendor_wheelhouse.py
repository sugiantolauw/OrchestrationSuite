"""Builds a `vendor/` wheelhouse for the deploy bundle (independent review
2026-09-24 item 6; CLAUDE.md §11 corporate-workspace assessment: "Binary
Python packages need vendored manylinux wheels. Pure-Python packages
install from PyPI" -- and whether the corporate Apps runtime can reach
PyPI at all is unconfirmed, so an option to vendor EVERYTHING exists for a
no-PyPI environment).

For every package pinned in requirements.lock, downloads a
manylinux2014_x86_64 (or the given `--manylinux-tag`) wheel built for the
Apps runtime's Python version (orchestrator.config.Settings.
apps_python_version, default "3.11" -- Databricks Apps cannot compile C
extensions at deploy time, CLAUDE.md §7) into `vendor/`, using `pip
download --no-deps --only-binary=:all:`. A package with no such wheel (pure
Python, `*-none-any.whl`) is left to install from PyPI/an index by its
ordinary `name==version` pin UNLESS `--vendor-all` is given, in which case
every package -- binary or pure -- is vendored.

Writes `vendor/requirements.txt`: the bundle's requirements.txt with a
binary package's line replaced by `vendor/<wheel filename>` and every pure
package left as `name==version`.

Dry-run only -- this script never deploys or touches a live App; that
remains scripts/deploy_app.py's job (which can be told to use this
vendored requirements.txt as its bundle's requirements.txt once built).

Usage:
    python scripts/build_vendor_wheelhouse.py [--requirements-lock PATH]
        [--out-dir PATH] [--python-version 3.11] [--manylinux-tag manylinux2014_x86_64]
        [--vendor-all] [--dry-run]

--dry-run prints the plan (which packages would be vendored, which left as
pip installs) without downloading anything -- the resolution logic itself
(tests/test_build_vendor_wheelhouse.py) is exercised entirely against a
mocked downloader, never a real `pip download`, so it runs with no network
and no multi-GB transfer.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANYLINUX_TAG = "manylinux2014_x86_64"

_REQ_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.!+_-]+)\s*$")


def parse_requirements_lock(path: Path) -> list[tuple[str, str]]:
    """`[(name, version), ...]`, in file order, skipping blank lines and
    `#`-comments. Raises ValueError on a line that isn't a plain `name==version`
    pin -- requirements.lock (scripts/generate_requirements_lock.py) never
    emits anything else, so a line that doesn't parse means the lock file's
    shape changed and this script needs a look, not a silent skip."""
    out: list[tuple[str, str]] = []
    for raw in Path(path).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _REQ_LINE_RE.match(line)
        if not m:
            raise ValueError(f"requirements.lock line does not parse as name==version: {raw!r}")
        out.append((m.group(1), m.group(2)))
    return out


def is_pure_python_wheel(filename: str) -> bool:
    """A wheel filename's tag ends `-none-any.whl` (or `-none-any` before
    an extension pip sometimes normalizes) for a package with no compiled
    extension -- true "pure Python", installable on any platform/Python
    ABI without vendoring."""
    stem = filename[:-4] if filename.endswith(".whl") else filename
    parts = stem.split("-")
    # wheel filename: {name}-{version}[-{build}]-{python tag}-{abi tag}-{platform tag}
    if len(parts) < 3:
        return False
    abi_tag, platform_tag = parts[-2], parts[-1]
    return abi_tag == "none" and platform_tag == "any"


@dataclass(frozen=True)
class PackagePlan:
    name: str
    version: str
    vendor: bool
    wheel_filename: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class WheelhousePlan:
    packages: tuple[PackagePlan, ...]

    @property
    def vendored(self) -> list[PackagePlan]:
        return [p for p in self.packages if p.vendor]

    @property
    def pip_installed(self) -> list[PackagePlan]:
        return [p for p in self.packages if not p.vendor]

    def requirements_lines(self) -> list[str]:
        lines = []
        for p in self.packages:
            if p.vendor:
                lines.append(f"vendor/{p.wheel_filename}")
            else:
                lines.append(f"{p.name}=={p.version}")
        return lines


DownloaderFn = Callable[[str, str, Path], list[Path]]


def plan_wheelhouse(
    requirements: list[tuple[str, str]],
    *,
    downloader: DownloaderFn,
    dest_dir: Path,
    vendor_all: bool = False,
) -> WheelhousePlan:
    """The resolution logic, offline-testable: `downloader(name, version,
    dest_dir) -> [wheel paths]` is the only thing that ever touches the
    network (or a mock, in tests) -- an empty list means "no matching wheel
    was found for this platform/Python version", which is expected and
    normal for a pure-Python package (pip only ships `-none-any` wheels for
    those, or sometimes only an sdist) and is treated as "install from PyPI
    instead", never as an error."""
    plans: list[PackagePlan] = []
    for name, version in requirements:
        downloaded = downloader(name, version, dest_dir)
        if not downloaded:
            plans.append(PackagePlan(name, version, vendor=False, reason="no matching wheel found"))
            continue
        wheel_path = downloaded[0]
        pure = is_pure_python_wheel(wheel_path.name)
        if pure and not vendor_all:
            plans.append(PackagePlan(name, version, vendor=False, reason="pure Python wheel"))
        else:
            plans.append(PackagePlan(
                name, version, vendor=True, wheel_filename=wheel_path.name,
                reason="vendor_all" if (pure and vendor_all) else "binary wheel",
            ))
    return WheelhousePlan(packages=tuple(plans))


def pip_download(
    name: str, version: str, dest_dir: Path, *, python_version: str, manylinux_tag: str,
) -> list[Path]:
    """The real downloader -- `pip download --no-deps --only-binary=:all:`
    for the given platform/Python version. Returns the wheel file(s) it
    produced, or [] if pip found nothing installable for that
    platform/version (pip's own exit code is non-zero in that case; treated
    as "nothing to vendor", not raised, since that is the expected shape
    for most pure-Python packages)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    before = set(dest_dir.glob("*.whl"))
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
            "--platform", manylinux_tag, "--python-version", python_version,
            "--implementation", "cp", "--abi", f"cp{python_version.replace('.', '')}",
            f"{name}=={version}", "-d", str(dest_dir),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    after = set(dest_dir.glob("*.whl"))
    return sorted(after - before)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements-lock", default=str(REPO_ROOT / "requirements.lock"))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "vendor"))
    parser.add_argument("--python-version", default=None, help="defaults to Settings.apps_python_version (3.11)")
    parser.add_argument("--manylinux-tag", default=DEFAULT_MANYLINUX_TAG)
    parser.add_argument("--vendor-all", action="store_true", help="vendor every package, not only binary ones")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; download nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    python_version = args.python_version
    if python_version is None:
        from orchestrator.config import load_settings

        python_version = load_settings().apps_python_version

    requirements = parse_requirements_lock(Path(args.requirements_lock))
    out_dir = Path(args.out_dir)

    if args.dry_run:
        print(
            f"DRY RUN: would resolve {len(requirements)} package(s) for "
            f"Python {python_version} / {args.manylinux_tag} into {out_dir} "
            f"(vendor_all={args.vendor_all})"
        )
        for name, version in requirements:
            print(f"  {name}=={version}")
        return 0

    downloader = lambda name, version, dest: pip_download(
        name, version, dest, python_version=python_version, manylinux_tag=args.manylinux_tag,
    )
    plan = plan_wheelhouse(requirements, downloader=downloader, dest_dir=out_dir, vendor_all=args.vendor_all)

    req_path = out_dir / "requirements.txt"
    out_dir.mkdir(parents=True, exist_ok=True)
    req_path.write_text("\n".join(plan.requirements_lines()) + "\n")

    print(f"Vendored {len(plan.vendored)} package(s), {len(plan.pip_installed)} left as pip installs.")
    print(f"Wrote {req_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
