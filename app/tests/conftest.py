from __future__ import annotations

import importlib.util
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_APP_DIR)

# Order matters: app/ must come before the repo root so `import src...`
# (used throughout app/) resolves to app/src, not some other `src`.
for _path in (_REPO_ROOT, _APP_DIR, _TESTS_DIR):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

import pytest  # noqa: E402

import fake_service  # noqa: E402
from src.platform import adapters  # noqa: E402


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    """Points app/src/platform/adapters at the fake backend instead of
    orchestrator.service, and gives every test a fresh context so runs
    created in one test don't leak into another."""
    monkeypatch.setattr(adapters, "service", fake_service)
    adapters._ctx = None
    yield
    adapters._ctx = None


def load_app_entry():
    """Loads app/app.py under an unambiguous module name. `app/app.py`
    defines a module-level variable named `app` (the Dash instance) and
    lives in a directory also named `app/` — importing it as `import app`
    or `from app import app` is genuinely ambiguous (it can resolve to
    either the file or the directory depending on sys.path order), so
    tests load it directly by file path instead of relying on that name."""
    spec = importlib.util.spec_from_file_location("tne_app_entry", os.path.join(_APP_DIR, "app.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
