"""HTTP scan service. No network: the one real scan runs offline against a fixture.

`open_source` is monkeypatched to hand back a local fixture directory, so the
server's clone -> scan -> render plumbing is exercised end to end without cloning
anything or opening a socket to a registry.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from driftscope import server
from driftscope.server import _allowed, _host_of, scan_request
from driftscope.source import Source

FIXTURE = Path(__file__).parent.parent / "fixtures" / "stale-estate"

# Keep the test scans hermetic: `offline` blocks all HTTP, but the Azure-API
# resolver shells out to the `az` CLI (the fixture has a main.bicep), which is not
# a socket and would hang a machine without az logged in. `no_az` closes that door.
HERMETIC = {"offline": True, "no_az": True, "no_github": True}


@pytest.fixture
def local_clone(monkeypatch):
    """Make open_source yield the stale-estate fixture instead of cloning."""

    @contextmanager
    def fake(target, *, ref=None, keep=False, ado_org=None):
        yield Source(display=target, path=FIXTURE)

    monkeypatch.setattr(server, "open_source", fake)


# --------------------------------------------------------------- input validation

def _run(req, **kw):
    return scan_request(req, **kw)


def test_missing_repo_is_a_400():
    code, ctype, body, _ = _run({})
    assert code == 400
    assert "repo" in json.loads(body)["error"]


def test_a_local_path_is_rejected_as_not_remote():
    code, _, body, _ = _run({"repo": "/etc/passwd"})
    assert code == 400
    assert "remote git URL" in json.loads(body)["error"]


def test_an_unknown_format_is_a_400(local_clone):
    code, _, body, _ = _run({"repo": "https://github.com/o/r", "format": "pdf", **HERMETIC})
    assert code == 400
    assert "format" in json.loads(body)["error"]


def test_a_host_outside_the_allowlist_is_a_403():
    code, _, body, _ = _run(
        {"repo": "https://evil.example.com/o/r"}, allow_hosts=("github.com",)
    )
    assert code == 403
    assert "allowlist" in json.loads(body)["error"]


@pytest.mark.parametrize(
    "repo, expected",
    [
        ("https://github.com/o/r", "github.com"),
        ("github.com/o/r", "github.com"),
        ("git@github.com:o/r.git", "github.com"),
        ("https://dev.azure.com/org/proj/_git/r", "dev.azure.com"),
    ],
)
def test_host_extraction(repo, expected):
    assert _host_of(repo) == expected


def test_allowlist_matches_exact_and_subdomain():
    assert _allowed("github.com", ("github.com",))
    assert _allowed("raw.github.com", ("github.com",))
    assert not _allowed("notgithub.com", ("github.com",))
    assert _allowed("anything.at.all", ())  # empty allowlist = allow all


# ------------------------------------------------------------------ a real scan

def test_a_successful_scan_returns_the_json_report(local_clone):
    code, ctype, body, headers = _run(
        {"repo": "https://github.com/o/r", **HERMETIC}
    )
    assert code == 200
    assert ctype.startswith("application/json")
    report = json.loads(body)
    # The fixture has k8s/ruby/action components that resolve offline, so the
    # report is non-empty and identical in shape to the CLI's --json.
    assert report["findings"], "offline fixture scan should still find components"
    assert "summary" in report
    assert "X-Drift-Severity" in headers and "=" in headers["X-Drift-Severity"]


def test_markdown_format_returns_a_markdown_document(local_clone):
    code, ctype, body, _ = _run(
        {"repo": "https://github.com/o/r", "format": "markdown", **HERMETIC}
    )
    assert code == 200
    assert ctype.startswith("text/markdown")
    assert body.decode().startswith("# Version drift report")


def test_html_format_returns_html(local_clone):
    code, ctype, body, _ = _run(
        {"repo": "https://github.com/o/r", "format": "html", **HERMETIC}
    )
    assert code == 200
    assert ctype.startswith("text/html")
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()


def test_a_clone_failure_is_a_400_not_a_500(monkeypatch):
    from driftscope.source import CloneError

    @contextmanager
    def boom(*a, **k):
        raise CloneError("repo not found")
        yield  # pragma: no cover

    monkeypatch.setattr(server, "open_source", boom)
    code, _, body, _ = _run({"repo": "https://github.com/o/nope"})
    assert code == 400
    assert "not found" in json.loads(body)["error"]


# ----------------------------------------------------------- HTTP layer (routing)

@pytest.fixture
def live_server(local_clone):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    httpd.ds_sem = threading.Semaphore(2)
    httpd.ds_allow_hosts = ()
    httpd.ds_cache_dir = None
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read()


def _post(url, obj):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, dict(r.headers), r.read()


def test_healthz_and_usage(live_server):
    code, body = _get(live_server + "/healthz")
    assert code == 200 and json.loads(body)["status"] == "ok"
    # No Accept header -> JSON usage (the API/agent path stays unchanged).
    code, body = _get(live_server + "/")
    assert "POST /scan" in json.loads(body)["endpoints"]


def _get_full(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, dict(r.headers), r.read()


def test_ui_page_is_served(live_server):
    code, hdrs, body = _get_full(live_server + "/ui")
    assert code == 200
    assert hdrs["Content-Type"].startswith("text/html")
    html = body.decode()
    assert "<form" in html and "DriftScope" in html and "/scan" in html


def test_root_serves_ui_to_browsers_json_to_clients(live_server):
    # A browser (Accept: text/html) gets the UI...
    code, hdrs, body = _get_full(live_server + "/", {"Accept": "text/html,*/*"})
    assert code == 200 and hdrs["Content-Type"].startswith("text/html")
    assert "<form" in body.decode()
    # ...an API client (Accept: application/json) gets the JSON usage doc.
    code, hdrs, body = _get_full(live_server + "/", {"Accept": "application/json"})
    assert hdrs["Content-Type"].startswith("application/json")
    assert "POST /scan" in json.loads(body)["endpoints"]


def test_post_scan_over_http(live_server):
    code, headers, body = _post(
        live_server + "/scan", {"repo": "https://github.com/o/r", **HERMETIC}
    )
    assert code == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["findings"]
    assert "X-Drift-Severity" in headers
