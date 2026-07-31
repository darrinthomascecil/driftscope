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
    def fake(target, *, ref=None, keep=False, ado_org=None, identity=None):
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
    httpd.ds_registry = None
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


def _parse_sse(raw: str):
    events = []
    for frame in raw.split("\n\n"):
        if not frame.strip():
            continue
        ev, data = "message", ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        events.append((ev, json.loads(data) if data else None))
    return events


def test_scan_streaming_emits_progress_phases_then_done(live_server):
    req = urllib.request.Request(
        live_server + "/scan?stream=1",
        data=json.dumps({"repo": "https://github.com/o/r", **HERMETIC}).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        events = _parse_sse(r.read().decode())

    phases = [d.get("phase") for ev, d in events if ev == "progress"]
    assert "cloning" in phases, phases
    assert "discovered" in phases, phases
    assert "resolving" in phases, phases  # per-ecosystem resolution progress
    # exactly one terminal done event, carrying the rendered report
    dones = [d for ev, d in events if ev == "done"]
    assert len(dones) == 1
    assert dones[0]["status"] == 200 and dones[0]["severity"]
    assert '"findings"' in dones[0]["body"]  # default format is json


def test_sse_heartbeats_keep_a_quiet_scan_alive(live_server, monkeypatch):
    """A scan that goes quiet (a slow clone) still writes heartbeats, so an idle-based
    proxy won't sever the connection before the done event arrives."""
    import time

    monkeypatch.setattr(server, "SSE_HEARTBEAT_SECS", 0.05)

    def slow_scan(req, **kw):  # no progress events, just a stall then a result
        time.sleep(0.3)
        return (200, "application/json", b'{"findings": []}',
                {"X-Drift-Severity": "current=1"})

    monkeypatch.setattr(server, "scan_request", slow_scan)

    req = urllib.request.Request(
        live_server + "/scan?stream=1",
        data=json.dumps({"repo": "https://github.com/o/r"}).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode()

    assert ": heartbeat" in raw  # at least one keepalive during the quiet stretch
    assert any(ev == "done" for ev, _ in _parse_sse(raw))  # and the result still lands


# ------------------------------------------------------- multi-repo compare

def test_scan_multi_json_compares_repos(monkeypatch):
    from driftscope import server
    from driftscope.models import Report, Finding, Component, Resolution, Lag, ScanStats, SEV_HIGH

    def fake_produce(req, *, cache_dir=None, allow_hosts=(), on_progress=None, registry=None, caller=None):
        # repo-a pins ioredis 4.3, repo-b pins 4.4 -> divergence
        ver = "4.3.0" if req["repo"].endswith("a") else "4.4.0"
        f = Finding(component=Component(ecosystem="npm", name="ioredis", current=ver, source="p"),
                    resolution=Resolution(latest_stable="5.4.1"), lag=Lag(), severity=SEV_HIGH)
        r = Report(root=req["repo"], findings=[f], stats=ScanStats())
        return 200, r, None

    monkeypatch.setattr(server, "produce_report", fake_produce)
    results, fc = server.run_multi(
        {"repos": ["https://github.com/o/repo-a", "https://github.com/o/repo-b"]},
    )
    out = server.multi_json(results, fc)
    assert [r["ok"] for r in out["repos"]] == [True, True]
    assert out["comparison"]["divergent_count"] == 1
    row = out["comparison"]["rows"][0]
    assert row["name"] == "ioredis" and row["diverges"]
    assert row["pins"]["https://github.com/o/repo-a"] == ["4.3.0"]
    assert row["pins"]["https://github.com/o/repo-b"] == ["4.4.0"]


def test_scan_multi_isolates_a_failed_repo(monkeypatch):
    from driftscope import server
    from driftscope.models import Report, Finding, Component, Resolution, Lag, ScanStats, SEV_HIGH

    def fake_produce(req, *, cache_dir=None, allow_hosts=(), on_progress=None, registry=None, caller=None):
        if req["repo"].endswith("bad"):
            return 400, None, "repo not found"
        f = Finding(component=Component(ecosystem="npm", name="x", current="1.0", source="p"),
                    resolution=Resolution(latest_stable="2.0"), lag=Lag(), severity=SEV_HIGH)
        return 200, Report(root=req["repo"], findings=[f], stats=ScanStats()), None

    monkeypatch.setattr(server, "produce_report", fake_produce)
    results, fc = server.run_multi({"repos": ["https://github.com/o/ok", "https://github.com/o/bad"]})
    assert results[0]["ok"] and not results[1]["ok"]
    assert "https://github.com/o/bad" in fc.errors  # comparison still built from the good one
    assert any(r.name == "x" for r in fc.rows)


def _post_expect_error(url, obj):
    import urllib.error
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_scan_multi_rejects_empty_and_oversized(live_server):
    code, body = _post_expect_error(live_server + "/scan-multi", {"repos": []})
    assert code == 400 and "non-empty" in json.loads(body)["error"]
    code, body = _post_expect_error(live_server + "/scan-multi", {"repos": ["https://github.com/o/r"] * 99})
    assert code == 400 and "too many" in json.loads(body)["error"]


# --------------------------------------------------- graceful shutdown (SIGTERM)

def test_serve_exits_gracefully_on_sigterm():
    """SIGTERM must drain and exit 0, not leave the process killed by the signal.

    Before the handler, SIGTERM hit Python's default (terminate) and the process
    exited with -SIGTERM; an orchestrator's every deploy/scale-in would reset live
    scans. A clean 0 proves serve_forever() stopped and server_close() drained.
    """
    import signal
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [sys.executable, "-m", "driftscope", "serve",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        up = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                    if r.status == 200:
                        up = True
                        break
            except OSError:
                time.sleep(0.1)
        assert up, f"server did not come up (exit={proc.poll()})"

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0  # graceful drain + clean exit
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ---------------------------------------------------- SSRF hardening (allowlist)

import pytest as _pytest  # noqa: E402  (already imported above; alias keeps the block self-contained)


@_pytest.mark.parametrize("repo", [
    "http://169.254.169.254/metadata/identity/oauth2/token",  # Azure IMDS -> token theft
    "http://169.254.169.254/latest/meta-data/",               # metadata path
    "https://169.254.169.254/",
    "http://[fd00:ec2::254]/latest/meta-data/",               # IPv6 metadata
    "http://10.1.2.3/o/r",                                    # RFC1918 private
    "http://192.168.0.1/o/r",
    "http://172.16.5.5/o/r",
    "http://127.0.0.1:8080/o/r",                              # loopback
    "http://localhost/o/r",
    "http://100.100.100.200/o/r",                             # alt metadata range
    "git@169.254.169.254:o/r",                                # scp-style to metadata
    "https://github.com@169.254.169.254/o/r",                 # userinfo confusion: real host is the IP
    "http://0xa9fea9fe/o/r",                                  # hex-encoded 169.254.169.254
    "http://2852039166/o/r",                                  # decimal-encoded 169.254.169.254
])
def test_ssrf_targets_are_blocked_by_the_clone_allowlist(repo):
    """A public /scan clones arbitrary URLs, so the allowlist must reject metadata
    endpoints, private ranges, loopback, and host-confusion tricks BEFORE any clone --
    otherwise the box's managed identity is one crafted request away. Rejection happens
    without a clone (no network), so this is hermetic."""
    code, _, body, _ = _run({"repo": repo}, allow_hosts=("github.com", "dev.azure.com"))
    assert code in (400, 403), f"{repo!r} must be rejected, got {code}"


def test_host_of_extracts_the_real_connect_host_not_userinfo():
    """The allowlist keys off the real host, not a spoofed `user@` prefix:
    github.com@evil connects to evil; evil@github.com connects to github."""
    from driftscope.server import _host_of
    assert _host_of("https://github.com@169.254.169.254/o/r") == "169.254.169.254"
    assert _host_of("https://169.254.169.254@github.com/o/r") == "github.com"
    assert _host_of("git@github.com:o/r") == "github.com"


def test_the_allowlist_does_not_overblock_the_real_forges():
    """Sanity: the hardening still lets the legitimate hosts through."""
    from driftscope.server import _allowed, _host_of
    allow = ("github.com", "dev.azure.com")
    assert _allowed(_host_of("https://github.com/o/r"), allow)
    assert _allowed(_host_of("https://raw.github.com/o/r"), allow)          # dotted subdomain
    assert _allowed(_host_of("https://dev.azure.com/org/p/_git/r"), allow)
    assert not _allowed(_host_of("https://notgithub.com/o/r"), allow)
