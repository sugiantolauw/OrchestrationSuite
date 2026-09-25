"""docs/specs/P7_mapping_authoring_design.md §2.2 "Validator": the shape a
collect-all validator run over a Skill directory reports in -- every check
that ran, whether it passed, and the accumulated errors/warnings across all
of them, rather than raising on the first violation the way `load_skill`
does."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)  # [{name, passed, detail}]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_check(self, name: str, violations: list[str], *, warning: bool = False) -> None:
        """Records one named check's outcome. `violations` land in
        `self.errors` unless `warning=True`, in which case a non-empty
        result is still a FAILED check line (so the table shows it) but
        never makes `self.ok` False -- the shape §2.2 wants for "a draft
        Skill with no lock is a warning, not an error" and for
        custom.py/workspace.py presence."""
        passed = not violations
        detail = "; ".join(violations) if violations else "ok"
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        if violations:
            (self.warnings if warning else self.errors).extend(violations)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "checks": [dict(c) for c in self.checks],
        }

    def render_table(self) -> str:
        if not self.checks:
            return "(no checks ran)"
        width = max(len(c["name"]) for c in self.checks)
        lines = []
        for c in self.checks:
            status = "PASS" if c["passed"] else "FAIL"
            lines.append(f"{c['name']:<{width}}  {status}  {c['detail']}")
        return "\n".join(lines)
