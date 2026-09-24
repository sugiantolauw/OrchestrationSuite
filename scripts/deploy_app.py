"""Idempotent deploy of the connected App (app/) to a Databricks App.

Everything comes from the environment or CLI args -- CLAUDE.md §3 non-negotiable
16: no hardcoded workspace, catalog, volume, endpoint or organisation name
anywhere in this file.

What it does, in order (safe to re-run):
  1. Builds a deploy bundle in a temp dir: app/, orchestrator/, skills/,
     requirements.txt, requirements.lock, and a generated app.yaml.
  2. Resolves the SQL warehouse (DBX_WAREHOUSE_HTTP_PATH, or the first
     available warehouse) and writes its http_path into app.yaml so
     orchestrator.config never has to derive it at runtime.
  3. Uploads the bundle to /Workspace/Users/<me>/<DBX_APP_NAME>.
  4. Creates the App if it does not exist, attaches the warehouse as the
     `sql-warehouse` resource with CAN_USE (idempotent: re-running updates
     the existing App's resources rather than erroring).
  5. Grants the App's service principal: USE CATALOG on the catalog;
     USE SCHEMA + SELECT on each schema named by DBX_SOURCE_SCHEMAS
     (.env.example); USE SCHEMA + SELECT +
     MODIFY + CREATE TABLE on <catalog>.<DBX_SCHEMA>; READ VOLUME +
     WRITE VOLUME on the exports Volume (creating <catalog>.<DBX_SCHEMA>.files
     first if DBX_VOLUME points at it and it does not exist yet); CAN_MANAGE
     on its MLflow experiment (MLFLOW_EXPERIMENT_PATH, created first if it
     does not exist yet).
  6. Deploys the uploaded source and waits for the deployment to succeed.
  7. Prints the App URL and deployment id.

Usage:
    python scripts/deploy_app.py [--app-name NAME] [--dry-run]

--dry-run builds and prints the bundle plan (app.yaml contents, grants that
would be issued) without talking to a workspace at all -- useful to sanity
check configuration before spending a real deploy.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

# App create/start and deployment can each take minutes (CLAUDE.md §11
# recorded results: app creation ~139s) -- the SDK's wait helpers require a
# real timedelta, not None (passing timeout=None raises AttributeError
# inside wait_get_app_active: it unconditionally calls timeout.total_seconds()).
_WAIT_TIMEOUT = timedelta(minutes=20)

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config import load_settings  # noqa: E402


_ACTIVE_RUN_STATUSES = ("queued", "running", "awaiting_confirmation", "awaiting_signoff", "interrupted")


def _warn_about_active_runs(persistence) -> None:
    """CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    2026-09-24 gap #11: printed from the OLD code, right before this deploy
    replaces it, so whoever is deploying sees exactly which runs it may
    affect and how (each one's own phase decides whether it can simply
    continue at export, or needs a restart -- section 11 of CLAUDE.md). A
    warning only -- there is no `--quiet` to suppress it, and it never
    blocks or changes what this script does next; a persistence problem
    here (no warehouse reachable yet, say) is reported and swallowed rather
    than failing an otherwise-unrelated deploy."""
    try:
        rows = [r for r in persistence.list_runs() if r.get("status") in _ACTIVE_RUN_STATUSES]
    except Exception as exc:
        print(f"Could not check for paused/running runs before deploying: {exc!r}")
        return
    if not rows:
        return
    fingerprints = persistence.get_fingerprints(
        [r["fingerprint_id"] for r in rows if r.get("fingerprint_id")]
    )
    print(f"\n{len(rows)} run(s) are paused or running -- this deploy may affect them:")
    for r in rows:
        fp = fingerprints.get(r.get("fingerprint_id")) or {}
        print(
            f"  {r['run_id']}: status={r['status']} phase={r.get('phase')} "
            f"code_revision={fp.get('code_revision', '<unknown>')}"
        )
    print(
        "A run paused at sign-off may continue its export under the new code; a run paused "
        "before its tests run (or interrupted before execute completed) must be restarted "
        "with the same parameters (CLAUDE.md §11 \"Paused runs across a code deploy\").\n"
    )


def _git_head(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


def _render_app_yaml(env_vars: dict[str, str]) -> str:
    lines = ["command:", '  - "python"', '  - "app/app.py"', "", "env:"]
    for name, value in env_vars.items():
        lines.append(f"  - name: {name}")
        lines.append(f'    value: "{value}"')
    return "\n".join(lines) + "\n"


def _build_bundle(tmp_dir: Path, app_yaml_text: str) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.db")
    for name in ("app", "orchestrator", "skills"):
        shutil.copytree(REPO_ROOT / name, tmp_dir / name, ignore=ignore)
    # P2/P3 gate review item 8: orchestrator.fingerprint.compute_fingerprint
    # hashes requirements.lock (the sibling of requirements.txt), not
    # requirements.txt itself -- the deployed app resolves REPO_ROOT from its
    # OWN copy of orchestrator/ (tmp_dir here), so the lock file must travel
    # in the bundle too, or every run fails loudly with a missing-lock
    # ConfigError the moment it tries to compute its fingerprint. Regenerate
    # it first (scripts/generate_requirements_lock.py) if requirements.txt
    # changed since the committed requirements.lock was last generated --
    # this script does not regenerate it silently, since that would pin
    # THIS deploy machine's installed versions rather than the ones actually
    # tested.
    lock_path = REPO_ROOT / "requirements.lock"
    if not lock_path.is_file():
        raise SystemExit(
            f"{lock_path} is missing -- run scripts/generate_requirements_lock.py first "
            f"(CLAUDE.md P2/P3 gate review item 8)"
        )
    # Non-blocking item 1 (P2/P3 gate review): Databricks Apps installs
    # dependencies from the bundle's OWN requirements.txt, not requirements.lock
    # -- shipping the loose, unpinned requirements.txt there meant the
    # fingerprint's dependency_lock_hash (hashed from requirements.lock)
    # described a set of pinned versions that was never actually what got
    # installed. The bundle's requirements.txt IS requirements.lock's exact
    # content (byte-for-byte the same file, under both names) so what pip
    # installs and what the fingerprint hashes are, by construction, the
    # same bytes -- never hashing a file that describes a different install.
    shutil.copy(lock_path, tmp_dir / "requirements.txt")
    shutil.copy(lock_path, tmp_dir / "requirements.lock")
    (tmp_dir / "app.yaml").write_text(app_yaml_text)


def _resolve_warehouse(w, settings) -> tuple[str, str]:
    """Returns (warehouse_id, http_path). Prefers the configured
    DBX_WAREHOUSE_HTTP_PATH; otherwise resolves the first available
    warehouse and reads its own http_path back from the platform rather
    than guessing the URL shape."""
    if settings.warehouse_http_path:
        warehouse_id = settings.warehouse_http_path.rstrip("/").rsplit("/", 1)[-1]
        return warehouse_id, settings.warehouse_http_path

    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise SystemExit(
            "No SQL warehouse found in this workspace and DBX_WAREHOUSE_HTTP_PATH is not set."
        )
    wh = warehouses[0]
    full = w.warehouses.get(wh.id)
    if full.odbc_params is None or not full.odbc_params.path:
        raise SystemExit(f"Warehouse {wh.id!r} has no resolvable http_path.")
    return wh.id, full.odbc_params.path


def _upload_dir(w, local_dir: Path, workspace_dir: str) -> int:
    from databricks.sdk.service.workspace import ImportFormat

    w.workspace.mkdirs(workspace_dir)
    count = 0
    for entry in sorted(local_dir.iterdir()):
        wpath = f"{workspace_dir}/{entry.name}"
        if entry.is_dir():
            count += _upload_dir(w, entry, wpath)
        else:
            content = entry.read_bytes()
            w.workspace.upload(wpath, content, format=ImportFormat.AUTO, overwrite=True)
            count += 1
    return count


def _ensure_app(w, app_name: str, warehouse_id: str):
    from databricks.sdk.service.apps import (
        App,
        AppResource,
        AppResourceSqlWarehouse,
        AppResourceSqlWarehouseSqlWarehousePermission,
    )

    resource = AppResource(
        name="sql-warehouse",
        sql_warehouse=AppResourceSqlWarehouse(
            id=warehouse_id,
            permission=AppResourceSqlWarehouseSqlWarehousePermission.CAN_USE,
        ),
    )

    existing = None
    for a in w.apps.list():
        if a.name == app_name:
            existing = a
            break

    if existing is not None:
        # w.apps.update() (a plain PATCH with the whole App object) rejects
        # this: "Compute size updates are not supported in this update API."
        # -- some field on the fetched `existing` object (unrelated to what
        # we're actually changing) trips that check when sent back
        # unconditionally. create_update_and_wait's update_mask targets only
        # `resources`, which is both what we want and what this API supports.
        w.apps.create_update_and_wait(
            app_name, update_mask="resources", app=App(name=app_name, resources=[resource]),
            timeout=_WAIT_TIMEOUT,
        )
        print(f"App {app_name!r} already exists — updated its resources.")
    else:
        app = App(name=app_name, resources=[resource])
        wait = w.apps.create(app)
        wait.result(timeout=_WAIT_TIMEOUT) if hasattr(wait, "result") else wait
        print(f"Created App {app_name!r}.")

    # Always re-fetch the canonical App afterward: create's and update's own
    # wait results have different shapes (App vs AppUpdate), and the caller
    # needs service_principal_* off the real App object either way.
    return w.apps.get(app_name)


def _ensure_volume(w, catalog: str, schema: str, volume: str) -> None:
    """Creates <catalog>.<schema>.<volume> if DBX_VOLUME points at it and
    it does not exist yet. Never creates the catalog or schema — those are
    provisioned elsewhere (scripts/setup_workspace.py, the P1B/P2 DDL)."""
    from databricks.sdk.errors import NotFound

    full_name = f"{catalog}.{schema}.{volume}"
    try:
        w.volumes.read(full_name)
        return
    except NotFound:
        pass

    from databricks.sdk.service.catalog import VolumeType

    w.volumes.create(
        catalog_name=catalog, schema_name=schema, name=volume,
        volume_type=VolumeType.MANAGED,
        comment="AI Audit Analyst exports/uploads volume (created by scripts/deploy_app.py)",
    )
    print(f"Created managed volume {full_name!r}.")


def _grant_all(
    w, *, catalog: str, schema: str, principal: str, dbx_volume: str | None, source_schemas: list[str],
) -> None:
    from databricks.sdk.service.catalog import PermissionsChange, Privilege, SecurableType

    def grant(securable_type: SecurableType, full_name: str, privileges: list[Privilege]) -> None:
        w.grants.update(
            securable_type.value, full_name,
            changes=[PermissionsChange(principal=principal, add=privileges)],
        )
        print(f"Granted {[p.value for p in privileges]} on {full_name} to {principal}")

    grant(SecurableType.CATALOG, catalog, [Privilege.USE_CATALOG])
    for source_schema in source_schemas:
        grant(SecurableType.SCHEMA, source_schema, [Privilege.USE_SCHEMA, Privilege.SELECT])
    grant(
        SecurableType.SCHEMA, f"{catalog}.{schema}",
        [Privilege.USE_SCHEMA, Privilege.SELECT, Privilege.MODIFY, Privilege.CREATE_TABLE],
    )

    if dbx_volume:
        # DBX_VOLUME is a Volume path: /Volumes/<catalog>/<schema>/<volume>.
        parts = [p for p in dbx_volume.split("/") if p]
        if len(parts) >= 4 and parts[0] == "Volumes":
            vol_catalog, vol_schema, vol_name = parts[1], parts[2], parts[3]
            if vol_catalog == catalog and vol_schema == schema and vol_name == "files":
                _ensure_volume(w, vol_catalog, vol_schema, vol_name)
            grant(
                SecurableType.VOLUME, f"{vol_catalog}.{vol_schema}.{vol_name}",
                [Privilege.READ_VOLUME, Privilege.WRITE_VOLUME],
            )
        else:
            print(f"DBX_VOLUME={dbx_volume!r} is not a /Volumes/<catalog>/<schema>/<name> path — "
                  "skipping the volume grant; grant it manually.")


def _ensure_mlflow_experiment_permissions(w, experiment_path: str, principal: str) -> None:
    """Grants the App's service principal CAN_MANAGE on its own MLflow
    experiment (CLAUDE.md P2/P3 gate review, MLflow-on-the-platform item) --
    the App logs a run + nested spans per pipeline run (orchestrator/adapters/
    tracing_mlflow.py) and needs write access to the experiment
    MLFLOW_EXPERIMENT_PATH now points it at. Creates the experiment first if
    it does not exist yet (a fresh MLFLOW_EXPERIMENT_PATH the App has never
    logged to), since permissions can only be granted on a real experiment_id.
    Uses update_permissions (a merge/PATCH over the ACL), never
    set_permissions (which REPLACES the whole ACL and could silently drop
    the deploying user's own access) -- the same additive-grant shape
    _grant_all already uses for Unity Catalog via w.grants.update."""
    from databricks.sdk.errors import NotFound, ResourceAlreadyExists
    from databricks.sdk.service.ml import ExperimentAccessControlRequest, ExperimentPermissionLevel

    try:
        experiment = w.experiments.get_by_name(experiment_path)
        experiment_id = experiment.experiment.experiment_id
    except NotFound:
        try:
            experiment_id = w.experiments.create_experiment(name=experiment_path).experiment_id
            print(f"Created MLflow experiment {experiment_path!r}.")
        except ResourceAlreadyExists:
            experiment_id = w.experiments.get_by_name(experiment_path).experiment.experiment_id

    w.experiments.update_permissions(
        experiment_id,
        access_control_list=[
            ExperimentAccessControlRequest(
                service_principal_name=principal, permission_level=ExperimentPermissionLevel.CAN_MANAGE,
            )
        ],
    )
    print(f"Granted CAN_MANAGE on MLflow experiment {experiment_path!r} ({experiment_id}) to {principal}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-name", default=None, help="Overrides DBX_APP_NAME.")
    parser.add_argument("--dry-run", action="store_true", help="Build and print the plan; touch nothing.")
    # Independent review 2026-09-24 item 7: an alternative to uploading a
    # bundle -- point at a workspace path a Databricks Git folder already
    # checked this repo out to (e.g. /Workspace/Repos/<user>/<repo> or a Git
    # folder under /Workspace/Users/<user>/.bundle/...), and this deploys
    # FROM that path instead. Only app.yaml is written there (the rest is
    # assumed to already be the Git folder's checked-out content); no local
    # bundle is built or uploaded.
    parser.add_argument(
        "--source-code-path", default=None,
        help="A workspace path (e.g. a Databricks Git folder) to deploy from instead of uploading a bundle. "
             "Only app.yaml is written there.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Importable entry point (independent review item 7 -- runnable from a
    workspace notebook/Git folder on a cluster with notebook authentication,
    where `WorkspaceClient()` needs no PAT). `argv` defaults to
    `sys.argv[1:]`; returns 0 on success. Fatal configuration problems still
    raise SystemExit (argparse's own convention, and this function's --
    unchanged from before) rather than returning a code, since they are
    usage errors a notebook caller should see as a raised exception, not a
    silently-returned non-zero its caller might not check."""
    args = build_parser().parse_args(argv)

    settings = load_settings()
    app_name = args.app_name or settings.app_name
    if not app_name:
        raise SystemExit("DBX_APP_NAME is not set (env or --app-name).")
    settings.require("catalog", "schema", "host")

    # CLAUDE.md §3 non-negotiable 16: the source schema(s) the App's SP reads
    # from (e.g. the Skill's raw source tables) are a portability concern
    # like any other -- this used to hardcode a specific T&E source schema
    # name as a literal (independent review 2026-09-24). DBX_SOURCE_SCHEMAS
    # is comma-separated fully-qualified
    # `catalog.schema` names (documented in .env.example); no default names
    # any schema, so an unset value grants nothing beyond the run schema
    # itself rather than guessing at a name that may not exist in another
    # workspace.
    source_schemas = [s.strip() for s in (os.environ.get("DBX_SOURCE_SCHEMAS") or "").split(",") if s.strip()]

    code_revision = _git_head(REPO_ROOT) or "unknown"

    env_vars = {
        "PORT": "8000",
        "DBX_CATALOG": settings.catalog,
        "DBX_SCHEMA": settings.schema,
        "CODE_REVISION": code_revision,
        # DBX_APP_NAME and DATABRICKS_HOST are both part of the run
        # fingerprint's runtime_config_hash (orchestrator/config.py) --
        # omitting either made the deployed App's own runtime_config_hash
        # differ from any caller's local one, so every fingerprint
        # verify_fingerprint() does at admission failed with a spurious
        # mismatch. Found live, driving a run against the deployed App's
        # executor from a separate process (CLAUDE.md integration pass item
        # 10) -- DATABRICKS_HOST would otherwise depend on however the
        # platform's own injected value happens to be formatted, rather than
        # matching the exact host string a caller's own DATABRICKS_HOST
        # (e.g. from .env) resolves to. Setting it explicitly here does not
        # change the App's identity -- that is always the platform-injected
        # DATABRICKS_CLIENT_ID/SECRET, never a value from this file.
        "DBX_APP_NAME": app_name,
        "DATABRICKS_HOST": settings.host,
        # MLflow on the platform (CLAUDE.md P2/P3 gate review): the deployed
        # App always uses Databricks-managed tracking, with an experiment
        # path derived from app_name (never a literal workspace name, CLAUDE.md
        # §3 non-negotiable 16) -- Databricks-managed tracking REQUIRES an
        # absolute workspace path, which the adapter's own generic fallback
        # name is not.
        "MLFLOW_TRACKING_URI": "databricks",
        "MLFLOW_EXPERIMENT_PATH": f"/Shared/{app_name}-audit-runs",
    }
    if settings.volume:
        env_vars["DBX_VOLUME"] = settings.volume
    if settings.model_sonnet:
        env_vars["MODEL_SONNET"] = settings.model_sonnet
    if settings.model_gpt_oss:
        env_vars["MODEL_GPT_OSS"] = settings.model_gpt_oss

    print(f"App name:      {app_name}")
    print(f"Code revision: {code_revision}")
    print(f"Catalog:       {settings.catalog}")
    print(f"Schema:        {settings.schema}")

    if args.dry_run:
        w = None
        warehouse_id, http_path = "<resolved-at-deploy-time>", settings.warehouse_http_path or "<resolved-at-deploy-time>"
    else:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        warehouse_id, http_path = _resolve_warehouse(w, settings)

        # CLAUDE.md §11 "Paused runs across a code deploy" / independent
        # review 2026-09-24 gap #11: a warning, not a gate -- see
        # _warn_about_active_runs. Only reachable once http_path is resolved
        # (never in --dry-run, which touches nothing).
        import dataclasses

        from orchestrator.adapters.persistence_delta import DeltaPersistence

        _warn_about_active_runs(DeltaPersistence(dataclasses.replace(settings, warehouse_http_path=http_path)))

    env_vars["DBX_WAREHOUSE_HTTP_PATH"] = http_path
    app_yaml_text = _render_app_yaml(env_vars)

    print("\n--- app.yaml ---")
    print(app_yaml_text)

    if args.dry_run:
        print("\n--dry-run: not touching the workspace. Grants that would be issued:")
        print(f"  USE CATALOG on {settings.catalog}")
        if source_schemas:
            for source_schema in source_schemas:
                print(f"  USE SCHEMA, SELECT on {source_schema}")
        else:
            print("  (no DBX_SOURCE_SCHEMAS configured — no source-schema grants)")
        print(f"  USE SCHEMA, SELECT, MODIFY, CREATE TABLE on {settings.catalog}.{settings.schema}")
        if settings.volume:
            print(f"  READ VOLUME, WRITE VOLUME on the volume named by {settings.volume}")
        print(f"  CAN_MANAGE on MLflow experiment {env_vars['MLFLOW_EXPERIMENT_PATH']!r} "
              f"(created first if it does not exist)")
        if args.source_code_path:
            print(f"  Would write app.yaml to {args.source_code_path}/app.yaml (no bundle upload).")
        return 0

    if args.source_code_path:
        # Independent review 2026-09-24 item 7: deploy from a workspace Git
        # folder that has already checked this repo out -- only app.yaml is
        # written there; app/, orchestrator/, skills/, requirements.* are
        # assumed to already be that checkout's own content.
        workspace_dir = args.source_code_path.rstrip("/")
        from databricks.sdk.service.workspace import ImportFormat

        w.workspace.upload(
            f"{workspace_dir}/app.yaml", app_yaml_text.encode("utf-8"),
            format=ImportFormat.AUTO, overwrite=True,
        )
        print(f"Wrote app.yaml to {workspace_dir}/app.yaml.")
        _finish_deploy(
            w, app_name=app_name, warehouse_id=warehouse_id, workspace_dir=workspace_dir,
            settings=settings, source_schemas=source_schemas, env_vars=env_vars,
        )
        return 0

    with tempfile.TemporaryDirectory(prefix="ai-audit-analyst-deploy-") as tmp:
        tmp_dir = Path(tmp)
        _build_bundle(tmp_dir, app_yaml_text)
        print(f"Bundle built at {tmp_dir} (app/, orchestrator/, skills/, requirements.txt, requirements.lock, app.yaml).")

        me = w.current_user.me().user_name
        workspace_dir = f"/Workspace/Users/{me}/{app_name}"
        n = _upload_dir(w, tmp_dir, workspace_dir)
        print(f"Uploaded {n} files to {workspace_dir}.")

        _finish_deploy(
            w, app_name=app_name, warehouse_id=warehouse_id, workspace_dir=workspace_dir,
            settings=settings, source_schemas=source_schemas, env_vars=env_vars,
        )
    return 0


def _finish_deploy(
    w, *, app_name: str, warehouse_id: str, workspace_dir: str, settings, source_schemas: list[str],
    env_vars: dict[str, str],
) -> None:
    """The part of a deploy common to both a bundle upload and a
    --source-code-path deploy: create/update the App, grant its service
    principal, and deploy from `workspace_dir`."""
    app = _ensure_app(w, app_name, warehouse_id)
    # service_principal_client_id (a UUID) first: Unity Catalog GRANT ...
    # TO `<principal>` needs the application/client id, not
    # service_principal_name -- that field is a human-readable DISPLAY
    # name ("app-2kxaxf ai-audit-analyst", with a space in it) and is
    # never a valid grant principal (CLAUDE.md §11 recorded results:
    # "GRANT ... TO `<sp client id>`").
    principal = app.service_principal_client_id or app.service_principal_id or app.service_principal_name
    if not principal:
        raise SystemExit(f"App {app_name!r} has no service principal yet — try again once it is provisioned.")

    _grant_all(
        w, catalog=settings.catalog, schema=settings.schema, principal=principal,
        dbx_volume=settings.volume, source_schemas=source_schemas,
    )
    _ensure_mlflow_experiment_permissions(w, env_vars["MLFLOW_EXPERIMENT_PATH"], principal)

    from databricks.sdk.service.apps import AppDeployment

    deployment = AppDeployment(source_code_path=workspace_dir)
    wait = w.apps.deploy(app_name, deployment)
    result = wait.result(timeout=_WAIT_TIMEOUT) if hasattr(wait, "result") else wait

    app = w.apps.get(app_name)
    print("\n--- Deployed ---")
    print(f"URL:           {app.url}")
    print(f"Deployment id: {getattr(result, 'deployment_id', None)}")


if __name__ == "__main__":
    sys.exit(main())
