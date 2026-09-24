# Databricks notebook source
# MAGIC %md
# MAGIC # Post-deploy idle-cost check
# MAGIC
# MAGIC Runs `scripts/check_idle_cost.py` from a cluster with **notebook
# MAGIC authentication** (independent review 2026-09-24 item 7) — the
# MAGIC regression check for the 2026-09-23 cost incident (CLAUDE.md §11):
# MAGIC after a deploy, with the App idle, the warehouse should end the window
# MAGIC STOPPED and should not have been woken more than a handful of times.
# MAGIC
# MAGIC Requires `DBX_WAREHOUSE_ID` (and, optionally,
# MAGIC `DBX_APP_SERVICE_PRINCIPAL_CLIENT_ID`) to already be set in this
# MAGIC cluster's environment — the same variables `scripts/deploy_app.py`
# MAGIC prints after a deploy.

# COMMAND ----------

dbutils.widgets.text("repo_root", "", "Repo checkout path (blank = auto-detect from this notebook)")
dbutils.widgets.text("minutes", "30", "Lookback window, minutes")
dbutils.widgets.text("max_start_events", "2", "Max warehouse STARTING events tolerated while idle")

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

import scripts.check_idle_cost as check_idle_cost  # noqa: E402

# COMMAND ----------

argv = [
    "--minutes", dbutils.widgets.get("minutes"),
    "--max-start-events", dbutils.widgets.get("max_start_events"),
]
print(f"scripts/check_idle_cost.py {' '.join(argv)}")
exit_code = check_idle_cost.main(argv)
if exit_code != 0:
    raise RuntimeError(f"check_idle_cost.main() returned {exit_code} (idle-cost check FAILED)")
