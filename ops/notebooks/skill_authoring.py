# Databricks notebook source
# MAGIC %md
# MAGIC # Skill authoring kit
# MAGIC
# MAGIC Runs `orchestrator.authoring` (docs/specs/P7_mapping_authoring_design.md §2,
# MAGIC build-here item 5) from a cluster with **notebook authentication**
# MAGIC (CLAUDE.md §11 "Operations must run from a workspace cluster/notebook"):
# MAGIC scaffold a new Skill, validate it, generate planted-exception fixtures
# MAGIC from a hand-authored `plants.yaml` sidecar, then score them. Writes
# MAGIC nothing to Delta except the one optional final cell (recording a
# MAGIC passing Surface 2 result on the draft's `skill_versions` row).

# COMMAND ----------

dbutils.widgets.text("repo_root", "", "Repo checkout path (blank = auto-detect from this notebook)")
dbutils.widgets.text("skill_dir", "", "Skill directory (existing, or the scaffold destination)")
dbutils.widgets.dropdown("do_scaffold", "no", ["no", "yes"], "Scaffold a new Skill at skill_dir first?")
dbutils.widgets.text("skill_id", "", "New Skill id (scaffold only)")
dbutils.widgets.text("skill_name", "", "New Skill name (scaffold only)")
dbutils.widgets.text("skill_domain", "", "New Skill domain (scaffold only)")
dbutils.widgets.text("skill_owner", "", "New Skill owner (scaffold only)")
dbutils.widgets.text("skill_timezone", "", "New Skill IANA timezone (scaffold only, e.g. Australia/Sydney)")
dbutils.widgets.text("plants_path", "", "Path to plants.yaml (blank = <skill_dir>/plants.yaml)")
dbutils.widgets.text("fixtures_dir", "", "Directory for generated fixture data (blank = .local/authoring_fixtures)")

# COMMAND ----------

import os
import sys

repo_root = dbutils.widgets.get("repo_root").strip()
if not repo_root:
    this_dir = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()
    repo_root = os.path.dirname(os.path.dirname(this_dir))
print(f"Using repo_root = {repo_root!r}")

if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from orchestrator.authoring.checks import validate_skill_dir  # noqa: E402
from orchestrator.authoring.fixtures import generate_fixtures  # noqa: E402
from orchestrator.authoring.scaffold import SampleSpec, scaffold_skill  # noqa: E402
from orchestrator.authoring.score import score_fixtures  # noqa: E402

# COMMAND ----------

skill_dir = dbutils.widgets.get("skill_dir").strip()
if not skill_dir:
    raise ValueError("the skill_dir widget is required")

if dbutils.widgets.get("do_scaffold") == "yes":
    scaffold_skill(
        skill_dir,
        skill_id=dbutils.widgets.get("skill_id").strip(),
        name=dbutils.widgets.get("skill_name").strip(),
        domain=dbutils.widgets.get("skill_domain").strip(),
        owner=dbutils.widgets.get("skill_owner").strip(),
        timezone=dbutils.widgets.get("skill_timezone").strip(),
    )
    print(f"Scaffolded a new Skill at {skill_dir}")

# COMMAND ----------

plants_path = dbutils.widgets.get("plants_path").strip() or os.path.join(skill_dir, "plants.yaml")
fixtures_dir = dbutils.widgets.get("fixtures_dir").strip() or os.path.join(repo_root, ".local", "authoring_fixtures")

report = validate_skill_dir(skill_dir)
print(report.render_table())
print()
print("PASSED" if report.ok else f"FAILED ({len(report.errors)} error(s), {len(report.warnings)} warning(s))")

# COMMAND ----------

written = generate_fixtures(skill_dir, plants_path, fixtures_dir)
print(f"Generated fixture data under {fixtures_dir}:")
for source, rows in sorted(written.items()):
    print(f"  {source}: {len(rows)} row(s)")

# COMMAND ----------

scores = score_fixtures(skill_dir, plants_path, fixtures_dir)
for test_id, s in sorted(scores.items()):
    print(
        f"{test_id:16s} plants={s.n_plants:3d} negs={s.n_negatives:3d} "
        f"TP={s.true_positives:3d} FP={s.false_positives:2d} FN={s.false_negatives:2d} "
        f"precision={s.precision} recall={s.recall}"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Optional: record a passing Surface 2 result
# MAGIC
# MAGIC Only run this cell once every scored test above meets precision >= 0.98
# MAGIC and recall >= 0.95. This is the one Delta write this notebook makes --
# MAGIC recording the result on the draft's `skill_versions` row that
# MAGIC `publish_skill`'s promotion guard reads (CLAUDE.md §4.5 "Promotion
# MAGIC draft -> published"). It never publishes by itself.

dbutils.widgets.text("record_actor", "", "Your identity, to record against skill_versions (leave blank to skip)")

# COMMAND ----------

actor = dbutils.widgets.get("record_actor").strip()
if actor:
    from orchestrator.service import build_app_context, record_skill_surface2  # noqa: E402
    from orchestrator.skills import load_skill  # noqa: E402

    skill = load_skill(skill_dir)
    all_pass = all(
        (s.precision is None or s.precision >= 0.98) and (s.recall is None or s.recall >= 0.95)
        for s in scores.values()
    )
    if not all_pass:
        raise RuntimeError("not every scored test meets precision >= 0.98 / recall >= 0.95 -- fix the Skill or the fixture, then re-run")
    ctx = build_app_context()
    result = record_skill_surface2(ctx, skill.skill_id, skill.version, scores, actor=actor)
    print(f"Recorded Surface 2 results for {result['skill_id']} v{result['version']}")
else:
    print("record_actor is blank -- skipping the Delta write")
