"""docs/specs/P7_mapping_authoring_design.md §2.2 "Scaffold": writes a new
Skill directory that loads and validates -- every required file, empty but
schema-valid, so `orchestrator.authoring.checks.validate_skill_dir` can run
against it immediately and a Skill author's very first step is a passing
(if trivial) validation, not a blank directory. Never writes `custom.py` or
`workspace.py` (CLAUDE.md §4.5 "Explorer does not generate code" -- the same
rule applies to a human scaffolding a Skill by hand)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import yaml

from orchestrator.explorer.profile import infer_column_type
from orchestrator.fingerprint import skill_content_hash

_CONTRACT_COLUMN_TYPES = ("string", "integer", "number", "date", "datetime", "boolean")


@dataclass
class SampleSpec:
    """One source's sample data file, for scaffold_skill's optional column
    inference (§2.2). `format`/`sheet` are inferred from the path's suffix
    when omitted; `sheet` is only used for an xlsx sample."""

    path: str | Path
    format: str | None = None
    sheet: str | int | None = None


def _yaml_scalar(value) -> str:
    """A single YAML flow scalar for `value` -- always double-quoted for a
    string (so a header containing a colon or a leading/trailing space is
    never ambiguous), and json-compatible for everything else, which is
    valid YAML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    return json.dumps(value)


def _read_sample(spec: SampleSpec) -> tuple[pd.DataFrame, str, str]:
    """Returns (dataframe, resolved_format, sha256_hex) for one sample file."""
    path = Path(spec.path)
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    fmt = spec.format or ("xlsx" if path.suffix.lower() in (".xlsx", ".xls") else "csv")
    if fmt == "xlsx":
        df = pd.read_excel(path, sheet_name=spec.sheet if spec.sheet is not None else 0)
    else:
        df = pd.read_csv(path)
    return df, fmt, sha256


def _render_contract_yaml(*, timezone: str, sources: dict[str, dict]) -> str:
    """Hand-emitted (not yaml.dump) because an inferred column needs a
    trailing `# REVIEW: inferred from sample <sha256[:12]>` comment
    (§2.2), which yaml.dump/PyYAML cannot attach to a specific key without
    a comment-preserving round-trip library -- and CLAUDE.md §10 asks
    before adding a dependency. `sources[name]` is
    {format, file, header_trim, inferred: bool, sample_sha256: str | None,
    columns: {col_name: {type, nullable, pii}}}."""
    lines = [f"timezone: {_yaml_scalar(timezone)}", "sources:"]
    if not sources:
        lines.append("  {}")
    for name, cfg in sources.items():
        lines.append(f"  {_yaml_scalar(name)}:")
        lines.append(f"    format: {_yaml_scalar(cfg['format'])}")
        lines.append(f"    file: {_yaml_scalar(cfg['file'])}")
        lines.append("    header_trim: true")
        lines.append("    columns:")
        if not cfg["columns"]:
            lines.append("      {}")
        for col_name, col_cfg in cfg["columns"].items():
            suffix = ""
            if cfg.get("inferred"):
                suffix = f"  # REVIEW: inferred from sample {cfg['sample_sha256'][:12]}"
            lines.append(f"      {_yaml_scalar(col_name)}:{suffix}")
            lines.append(f"        type: {_yaml_scalar(col_cfg['type'])}")
            lines.append(f"        nullable: {_yaml_scalar(col_cfg['nullable'])}")
            # NN14/§2.2: never inferred false -- every scaffolded column is
            # pii: true until the author reviews and changes it.
            lines.append(f"        pii: {_yaml_scalar(col_cfg.get('pii', True))}")
    return "\n".join(lines) + "\n"


def scaffold_skill(
    dest: str | Path,
    *,
    skill_id: str,
    name: str,
    domain: str,
    owner: str,
    timezone: str,
    sources: dict[str, SampleSpec] | None = None,
) -> Path:
    """Writes a new Skill at `dest` (created if absent) that
    `orchestrator.skills.load_skill(dest)` loads and
    `orchestrator.authoring.checks.validate_skill_dir(dest)` validates with
    zero errors. `timezone` has no default -- CLAUDE.md §0.5/NN14: an audit
    period is a business-calendar concept in a STATED timezone, never
    guessed, so a Skill author states one explicitly from the first
    keystroke rather than inheriting a placeholder that might survive into
    a real run. Raises ValueError for a timezone zoneinfo cannot resolve.

    `sources`, when given, seeds contract.yaml/plan.yaml from real sample
    files (authoring-time inference, §2.2 -- reviewed by a human like
    CLAUDE.md §4.6; RUNTIME binding stays exact, never fuzzy, per NN14):
    each source gets one `raw_<name>` population in plan.yaml, and every
    inferred column is written `pii: true` with a `# REVIEW` comment naming
    the sample's sha256 prefix. With `sources=None`, contract.yaml/plan.yaml
    are empty-but-schema-valid -- a Skill author fills sources in by hand."""
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"timezone {timezone!r} is not a known IANA timezone") from exc

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "reference").mkdir(exist_ok=True)

    manifest = {
        "id": skill_id,
        "name": name,
        "domain": domain,
        "version": "0.1.0-draft",
        "owner": owner,
        "status": "draft",
    }
    (dest / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    contract_sources: dict[str, dict] = {}
    plan_populations: dict[str, dict] = {}
    for src_name, spec in (sources or {}).items():
        df, fmt, sha256 = _read_sample(spec)
        columns = {
            str(col): {"type": infer_column_type(df[col]), "nullable": bool(df[col].isna().any()), "pii": True}
            for col in df.columns
        }
        contract_sources[src_name] = {
            "format": fmt,
            "file": f"{src_name}.{fmt}",
            "columns": columns,
            "inferred": True,
            "sample_sha256": sha256,
        }
        plan_populations[f"raw_{src_name}"] = {"source": src_name}

    (dest / "contract.yaml").write_text(_render_contract_yaml(timezone=timezone, sources=contract_sources))

    plan = {"populations": plan_populations, "tests": []}
    (dest / "plan.yaml").write_text(yaml.safe_dump(plan, sort_keys=False))

    (dest / "findings.yaml").write_text(yaml.safe_dump({"findings": []}, sort_keys=False))
    (dest / "thresholds.yaml").write_text(yaml.safe_dump({}, sort_keys=False))
    (dest / "risk_control.yaml").write_text(yaml.safe_dump({"controls": [], "risks": []}, sort_keys=False))
    (dest / "catalogue.yaml").write_text(yaml.safe_dump({"tests": []}, sort_keys=False))

    plants_skeleton = {
        "seed": 20260101,
        "natural_id_column": "Fixture Line Id",
        "background": {},
        "tests": {},
    }
    (dest / "plants.yaml").write_text(yaml.safe_dump(plants_skeleton, sort_keys=False))

    content_hash = skill_content_hash(dest)
    lock = {"version": manifest["version"], "content_hash": content_hash}
    (dest / "content.lock").write_text(yaml.safe_dump(lock, sort_keys=False))

    return dest
