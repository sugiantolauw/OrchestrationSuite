# Databricks notebook source
# MAGIC %md
# MAGIC # Surface 1 -- auditor-confirmed exception list
# MAGIC
# MAGIC Runs `scripts/run_surface1.py` (CLAUDE.md §5 Tier B, §11 "Surface 1
# MAGIC harness") from a cluster with **notebook authentication** against a
# MAGIC completed run and the auditor-confirmed oracle file dropped into this
# MAGIC workspace (never committed to the repo, CLAUDE.md §9 -- upload it to a
# MAGIC Volume or workspace path first and point `oracle_path` at it).

# COMMAND ----------

dbutils.widgets.text("repo_root", "", "Repo checkout path (blank = auto-detect from this notebook)")
dbutils.widgets.text("run_id", "", "run_id to score (must be completed or awaiting_signoff)")
dbutils.widgets.text("oracle_path", "", "Path to the auditor-confirmed oracle file (.csv or .xlsx)")
dbutils.widgets.text("out_dir", "", "Output directory for the JSON/XLSX reports (blank = .local/surface1)")

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

import scripts.run_surface1 as run_surface1_script  # noqa: E402

# COMMAND ----------

run_id = dbutils.widgets.get("run_id").strip()
oracle_path = dbutils.widgets.get("oracle_path").strip()
out_dir = dbutils.widgets.get("out_dir").strip()

if not run_id:
    raise ValueError("the run_id widget is required")
if not oracle_path:
    raise ValueError("the oracle_path widget is required")

argv = ["--run-id", run_id, "--oracle", oracle_path]
if out_dir:
    argv += ["--out", out_dir]
print(f"scripts/run_surface1.py {' '.join(argv)}")
exit_code = run_surface1_script.main(argv)
if exit_code != 0:
    raise RuntimeError(f"run_surface1.main() returned {exit_code} (Surface 1 FAILED)")
