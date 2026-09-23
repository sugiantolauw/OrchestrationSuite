"""Browser E2E fixtures for the reference_app (prototype) Dash UI.

Collection is gated on RUN_E2E=1 (see pytest_ignore_collect below) so the
default `python -m pytest -q` unit run is unaffected: these tests do not
even get collected unless RUN_E2E=1 is set, which keeps
`python -m pytest -q --co | tail -1`'s count unchanged.

Point APP_URL at an already-running app (prototype or, later, the rebuilt
app) to skip launching reference_app/app.py locally. Without it, this
module launches `python app.py` from reference_app/ on a free port and
waits for it to serve HTTP 200. That fallback path has no data Volume
configured, so it exercises the prototype's deterministic demo-data path
(see reference_app/app.py:load_runtime_data) -- expected, not a bug.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCE_APP_DIR = REPO_ROOT / "reference_app"


def pytest_ignore_collect(collection_path, config):
    """Keep tests/e2e out of the default unit run entirely (collection-time,
    not just skipped) unless explicitly requested with RUN_E2E=1."""
    if os.environ.get("RUN_E2E") != "1":
        return True
    return False


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: browser end-to-end tests against the Dash app (tests/e2e)")


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.e2e)


# ─── App under test ──────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http_200(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
        time.sleep(0.5)
    raise RuntimeError(f"App did not respond 200 at {url} within {timeout}s (last error: {last_err})")


@pytest.fixture
def app_base_url():
    """Base URL of the app under test: a fresh reference_app/app.py process
    per test.

    APP_URL, if set, points this suite at an already-running app (used for
    an authenticated runner against a deployed app later -- see
    CLAUDE.md §9B "Practical pattern for Free Edition") and is reused
    across tests, since it isn't this suite's process to restart.

    Without APP_URL, a fresh process per test costs a few seconds of
    startup, but reference_app/app.py runs Flask's dev server single-
    threaded (app.py:2468, no threaded=True): reusing one process across
    the whole file was observed to work reliably for roughly a dozen
    consecutive full-page navigations and then stall indefinitely on
    every request after, regardless of which test came 13th -- consistent
    with the dev server (not this suite, not the browser) exhausting some
    resource (most plausibly its request queue/socket backlog) rather
    than any single test's actions. A fresh process per test sidesteps
    that entirely rather than chasing a workaround for a server this
    suite cannot modify.
    """
    external = os.environ.get("APP_URL")
    if external:
        base = external.rstrip("/")
        _wait_for_http_200(base + "/", timeout=30.0)
        yield base
        return

    port = _free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=str(REFERENCE_APP_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_200(base_url + "/", timeout=90.0)
    except Exception:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=10)
        raise RuntimeError(f"reference_app failed to start on {base_url}:\n{out}")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# ─── Playwright browser/page ─────────────────────────────────────────────────

def _chromium_executable() -> str | None:
    """Resolve the pre-installed Chromium under PLAYWRIGHT_BROWSERS_PATH
    explicitly, rather than relying on the installed playwright package's
    own (version-sensitive) browser resolution -- see the session report
    for why: the pip-latest playwright package expects a newer browser
    build than what's pre-installed in this environment."""
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    base = Path(browsers_path)
    if not base.is_dir():
        return None
    candidates = sorted(base.glob("chromium-*/chrome-linux*/chrome"))
    if not candidates:
        return None
    return str(candidates[-1])


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
        exe = _chromium_executable()
        if exe:
            launch_kwargs["executable_path"] = exe
        b = p.chromium.launch(**launch_kwargs)
        yield b
        b.close()


_cdn_cache: dict[str, tuple[bytes, str]] = {}


def _serve_cdn_locally(route):
    """Fulfil CDN requests (e.g. app.py's dbc.themes.BOOTSTRAP stylesheet)
    from a same-process fetch instead of letting headless Chromium fetch
    them itself.

    Chromium's bundled NSS store in this sandbox does not carry the
    TLS-terminating egress proxy's CA (see /root/.ccr/README.md), so a
    direct browser fetch of an external HTTPS asset fails with
    ERR_CERT_AUTHORITY_INVALID. Without the stylesheet, dash-bootstrap-
    components' tab/modal show-hide CSS never applies, panes stack instead
    of hiding, and clicks land on the wrong (overlapping) element --
    corrupting otherwise-unrelated tests. This is not a TLS-verification
    bypass: urllib here performs a normal, fully-verified HTTPS request
    (proven working via `curl` against this same host in the session
    report) and simply hands Chromium the resulting bytes directly."""
    url = route.request.url
    if url not in _cdn_cache:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read()
            content_type = resp.headers.get_content_type() or "text/css"
        _cdn_cache[url] = (body, content_type)
    body, content_type = _cdn_cache[url]
    route.fulfill(status=200, content_type=content_type, body=body)


#: reference_app/app.py runs Flask's development server without
#: threaded=True (app.py:2468), so it serves one request at a time. This
#: suite therefore keeps requests sequential (no parallel pages/workers)
#: and uses generous timeouts rather than tuning the server, which would
#: mean modifying reference_app/.
_DEFAULT_TIMEOUT_MS = 45_000


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    context.route("https://cdn.jsdelivr.net/**", _serve_cdn_locally)
    context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    context.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)
    pg = context.new_page()
    yield pg
    # reference_app/app.py runs Flask's dev server single-threaded
    # (app.py:2468, no threaded=True). Tearing a context down while a
    # request it started is still in flight can leave that connection
    # half-open server-side, and a single-threaded server can then stall
    # on it, starving every later test's requests. Give any in-flight
    # request from the test's last interaction a chance to finish first.
    try:
        pg.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    context.close()


# ─── Console / network error capture ─────────────────────────────────────────

#: Console noise that is a property of this sandboxed environment's TLS-
#: terminating egress proxy, not of the application under test. The app
#: itself is always served locally over plain HTTP (no TLS involved), so
#: any ERR_CERT_* console error can only come from a cross-origin HTTPS
#: asset (e.g. app.py:1056's dbc.themes.BOOTSTRAP CDN stylesheet) failing
#: to load because the headless Chromium profile does not carry the
#: proxy's re-terminating CA (see /root/.ccr/README.md). This does not
#: disable TLS verification -- Chromium still fully verifies the chain and
#: still fails the request; it only keeps that known, environment-specific
#: failure from masking a genuine application console error.
_ENVIRONMENT_PROXY_NOISE = "net::ERR_CERT_AUTHORITY_INVALID"


class ConsoleWatcher:
    """Collects browser console errors and failed (>=400) responses to
    Dash's own update endpoint while attached to a page."""

    def __init__(self, page):
        self.console_errors: list[str] = []
        self.failed_dash_responses: list[tuple[int, str]] = []
        page.on("console", self._on_console)
        page.on("response", self._on_response)

    def _on_console(self, msg):
        if msg.type == "error" and _ENVIRONMENT_PROXY_NOISE not in msg.text:
            self.console_errors.append(msg.text)

    def _on_response(self, response):
        if "_dash-update-component" in response.url and response.status >= 400:
            self.failed_dash_responses.append((response.status, response.url))

    def assert_clean(self):
        assert not self.console_errors, f"Browser console errors: {self.console_errors}"
        assert not self.failed_dash_responses, (
            f"Failed _dash-update-component responses: {self.failed_dash_responses}"
        )


@pytest.fixture
def watched_page(page):
    """A page with console-error and failed-callback-response capture wired
    up from the very first navigation."""
    watcher = ConsoleWatcher(page)
    return page, watcher


def goto(page, base_url: str, path: str):
    page.goto(base_url + path, wait_until="networkidle")
    return page


def dismiss_stray_action_modal(page):
    """Work around a known prototype bug (see test_known_bugs.py::
    test_tne_route_loads_without_spurious_action_modal): the Management
    Action modal (#action-modal) can be open on initial load of
    /workspace/tne with no user interaction, its backdrop covering the
    whole page. Route around it here so the rest of the suite can still
    exercise real page behaviour; the bug itself is captured, not hidden,
    by its own dedicated xfail test."""
    modal = page.locator("#action-modal")
    if modal.count() and modal.evaluate("el => getComputedStyle(el).display") != "none":
        page.locator("#action-cancel").click()
        page.wait_for_load_state("networkidle")


def goto_workspace(page, base_url: str):
    goto(page, base_url, "/workspace/tne")
    dismiss_stray_action_modal(page)
    return page


def has_error_overlay(page) -> bool:
    """Best-effort detection of a Dash dev-tools error overlay. The
    prototype runs with debug=False (reference_app/app.py:2468), which
    disables the fancy overlay UI, so the primary signal for a broken
    callback is watched_page's console/network capture -- this is a
    belt-and-suspenders structural check."""
    return page.locator(".dash-error-card, #_dash-global-error-container, ._dash-error-menu").count() > 0
