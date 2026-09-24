# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy the AI Audit Analyst App
# MAGIC
# MAGIC Runs `scripts/deploy_app.py` from a cluster with **notebook
# MAGIC authentication** — `WorkspaceClient()` picks that up automatically, no
# MAGIC personal access token needed (independent review 2026-09-24 item 7).
# MAGIC
# MAGIC Works two ways:
# MAGIC 1. **From a Git folder** — attach this notebook to a Databricks Git
# MAGIC    folder checkout of this repo and just run all cells; `repo_root`
# MAGIC    below is auto-detected from this notebook's own path.
# MAGIC 2. **From anywhere else** — set `repo_root` to wherever the repo's
# MAGIC    checkout lives in the workspace (e.g. `/Workspace/Repos/<user>/<repo>`).
# MAGIC
# MAGIC Set `source_code_path` to deploy directly from that checkout (no bundle
# MAGIC upload — see `scripts/deploy_app.py --source-code-path`); leave it blank
# MAGIC to build and upload a bundle instead, the same as running the script
# MAGIC from a local checkout.
# MAGIC
# MAGIC `.env` is **not** required here — every value below is read from this
# MAGIC cluster's own environment/secrets (or the widgets), never from a file
# MAGIC that would need to be committed (CLAUDE.md §3 non-negotiable 16).

# COMMAND ----------

dbutils.widgets.text("repo_root", "", "Repo checkout path (blank = auto-detect from this notebook)")
dbutils.widgets.text("app_name", "", "App name (blank = DBX_APP_NAME from the environment)")
dbutils.widgets.text("source_code_path", "", "Deploy FROM this workspace path (blank = build+upload a bundle)")
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "Dry run")

# COMMAND ----------

import os
import sys

repo_root = dbutils.widgets.get("repo_root").strip()
if not repo_root:
    # This notebook lives at <repo_root>/ops/notebooks/deploy_app.py --
    # two levels up from its own directory.
    this_dir = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()
    repo_root = os.path.dirname(os.path.dirname(this_dir))
print(f"Using repo_root = {repo_root!r}")

if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import scripts.deploy_app as deploy_app  # noqa: E402

# COMMAND ----------

argv = []
app_name = dbutils.widgets.get("app_name").strip()
source_code_path = dbutils.widgets.get("source_code_path").strip()
dry_run = dbutils.widgets.get("dry_run") == "true"

if app_name:
    argv += ["--app-name", app_name]
if source_code_path:
    argv += ["--source-code-path", source_code_path]
if dry_run:
    argv += ["--dry-run"]

print(f"scripts/deploy_app.py {' '.join(argv)}")
exit_code = deploy_app.main(argv)
if exit_code != 0:
    raise RuntimeError(f"deploy_app.main() returned {exit_code}")
