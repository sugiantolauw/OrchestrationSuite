# Databricks notebook source
# MAGIC %md
# MAGIC # Bootstrap the workspace (catalog / schema / volume / tables)
# MAGIC
# MAGIC Runs `scripts/setup_workspace.py` from a cluster with **notebook
# MAGIC authentication** (independent review 2026-09-24 item 7) — no personal
# MAGIC access token needed. Idempotent; safe to run repeatedly.
# MAGIC
# MAGIC Attach this notebook to a Databricks Git folder checkout of this repo
# MAGIC and run all cells; `repo_root` is auto-detected from this notebook's
# MAGIC own path unless overridden below.

# COMMAND ----------

dbutils.widgets.text("repo_root", "", "Repo checkout path (blank = auto-detect from this notebook)")
dbutils.widgets.text("catalog", "", "Catalog (blank = DBX_CATALOG from the environment)")
dbutils.widgets.text("schema", "", "Schema (blank = DBX_SCHEMA from the environment)")
dbutils.widgets.text("volume", "", "Volume path (blank = DBX_VOLUME from the environment)")
dbutils.widgets.text("warehouse_id", "", "Warehouse id (blank = auto-discover the first available one)")

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

import scripts.setup_workspace as setup_workspace  # noqa: E402

# COMMAND ----------

argv = []
for widget_name, flag in (
    ("catalog", "--catalog"), ("schema", "--schema"),
    ("volume", "--volume"), ("warehouse_id", "--warehouse-id"),
):
    value = dbutils.widgets.get(widget_name).strip()
    if value:
        argv += [flag, value]

print(f"scripts/setup_workspace.py {' '.join(argv)}")
exit_code = setup_workspace.main(argv)
if exit_code != 0:
    raise RuntimeError(f"setup_workspace.main() returned {exit_code}")
