"""HTTP surface: POST a repo URL, get a drift report back.

Zero-dependency by construction, like the rest of the tool -- this is stdlib
`http.server`, not a framework. It exists so driftscope can run as a service (or
behind an agent) instead of only as a one-shot CLI. It re-implements none of the
analysis: `scan_request` walks the same `open_source -> build_context -> run_scan
-> render` path `cmd_scan` does, so the body it returns is byte-identical to
`driftscope scan <url> --json`.

The core is split from the HTTP shell on purpose. `scan_request` is a plain
function -- validate, clone, scan, render, return `(status, content_type, body,
headers)` -- so it is testable without opening a socket. `_Handler` and
`run_server` add only transport: body reading, a concurrency gate, and routing.

Security note this module is honest about: `POST /scan` clones whatever URL it is
given, which is server-side request execution against an arbitrary host. Behind a
public endpoint that is an SSRF surface. `allow_hosts` (``--allow-host`` /
``DRIFTSCOPE_ALLOW_HOSTS``) restricts which hosts may be cloned; it defaults to
open because a locked-down default is unusable out of the box, but any real
deployment should set it.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .report import render
from .source import CloneError, looks_remote, open_source

# A scan request is a small JSON object; anything larger is a mistake or an abuse,
# so the body is capped hard rather than streamed.
MAX_BODY = 64 * 1024

_RENDERERS = {
    "json": ("application/json; charset=utf-8", render.to_json),
    "markdown": ("text/markdown; charset=utf-8", render.to_markdown),
    "text": ("text/plain; charset=utf-8", render.to_text),
    # "html" is handled separately -- its renderer lives in report.html, which is
    # imported lazily so the common JSON path never pays for it.
}


def _host_of(repo: str) -> str:
    """The host a git URL would be cloned from, for allowlist checks.

    `scp`-style `git@github.com:org/repo` has no scheme, so a bare reference is
    given one before parsing; `urlparse` otherwise reads the whole thing as a path
    and returns an empty host, which would silently pass every allowlist.
    """
    ref = repo.strip()
    if "://" not in ref:
        ref = "ssh://" + ref.replace(":", "/", 1) if "@" in ref else "https://" + ref
    return (urlparse(ref).hostname or "").lower()


def _allowed(host: str, allow_hosts) -> bool:
    """Empty allowlist = allow all. Otherwise exact host or a dotted subdomain."""
    if not allow_hosts:
        return True
    return any(host == h or host.endswith("." + h) for h in allow_hosts)


def _severity_counts(report) -> dict:
    counts: dict[str, int] = {}
    for f in report.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def produce_report(req, *, cache_dir=None, allow_hosts=(), on_progress=None):
    """Validate, clone, and scan one repo. Returns (status, Report|None, error|None).

    The report-producing core, split out of scan_request so the multi-repo endpoint
    reuses the exact same clone -> run_scan path and gets Report objects to compare.
    """
    _p = on_progress or (lambda _e: None)
    repo = req.get("repo") or req.get("url")
    if not isinstance(repo, str) or not repo.strip():
        return 400, None, "body must be a JSON object with a 'repo' git URL"
    if not looks_remote(repo):
        return 400, None, f"'repo' must be a remote git URL, got {repo!r}"
    if not _allowed(_host_of(repo), allow_hosts):
        return 403, None, f"host {_host_of(repo)!r} is not in the clone allowlist"
    ecosystems = req.get("ecosystems")
    if ecosystems is not None and not isinstance(ecosystems, list):
        return 400, None, "'ecosystems' must be a list of ecosystem names"

    from .engine import build_context, run_scan

    _p({"phase": "cloning", "repo": repo})
    try:
        with open_source(repo, ref=req.get("ref"), ado_org=req.get("ado_org")) as src:
            _p({"phase": "cloned", "commit": (src.commit or "")[:12] if src.cloned else None})
            ctx = build_context(
                offline=bool(req.get("offline")),
                no_github=bool(req.get("no_github")),
                no_az=bool(req.get("no_az")),
                cache_dir=Path(cache_dir) if cache_dir else None,
            )
            report = run_scan(
                src.path, ctx=ctx,
                ecosystems=set(ecosystems) if ecosystems else None,
                include_vulns=bool(req.get("include_vulns", True)),
                include_velocity=bool(req.get("velocity", False)),
                progress=on_progress,
            )
            report.root = src.display
            if src.cloned:
                report.source = {"remote": src.remote, "ref": src.ref, "commit": src.commit}
    except CloneError as e:
        return 400, None, str(e)
    except Exception as e:  # a scan bug must not take the server down
        return 500, None, f"scan failed: {type(e).__name__}: {e}"
    return 200, report, None


def render_report(report, fmt):
    """Render a Report to (content_type, body_bytes, headers) for `fmt`."""
    headers = {"X-Drift-Severity": ",".join(f"{k}={v}" for k, v in _severity_counts(report).items())}
    commit = report.source.get("commit") if isinstance(report.source, dict) else None
    if commit:
        headers["X-Drift-Commit"] = str(commit)[:12]
    if fmt == "html":
        from .report import html as html_report
        return "text/html; charset=utf-8", html_report.to_html(report).encode(), headers
    content_type, fn = _RENDERERS[fmt]
    return content_type, fn(report).encode(), headers


MAX_REPOS = 12  # a fleet compare clones every repo; cap so one call can't run away


def run_multi(req, *, cache_dir=None, allow_hosts=(), on_progress=None):
    """Scan several repos and compare them. Returns (repo_results, FleetComparison).

    Sequential on purpose: the disk cache makes later repos cheap (a shared dep is
    resolved once and reused), and it keeps the streamed progress legible -- one repo
    at a time. A repo that fails to clone is isolated: its error is recorded and the
    comparison is built from the repos that succeeded.
    """
    from .compare import compare_reports

    _p = on_progress or (lambda _e: None)
    repos = req.get("repos") or []
    named, errors, results = [], {}, []
    for i, url in enumerate(repos):
        _p({"phase": "repo", "repo_index": i + 1, "repo_total": len(repos), "repo": url})
        sub = {k: v for k, v in req.items() if k != "repos"}
        sub["repo"] = url
        # repo_index/repo_total are added under distinct keys so they don't clobber a
        # resolution event's own `total` (its component count).
        _status, report, error = produce_report(
            sub, cache_dir=cache_dir, allow_hosts=allow_hosts,
            on_progress=lambda ev: _p({**ev, "repo_index": i + 1, "repo_total": len(repos)}),
        )
        if report is not None:
            commit = (report.source or {}).get("commit") if isinstance(report.source, dict) else None
            named.append((url, report))
            results.append({"repo": url, "ok": True, "commit": (commit or "")[:12] or None,
                            "severity": _severity_counts(report), "report": report})
        else:
            errors[url] = error
            results.append({"repo": url, "ok": False, "error": error})
    return results, compare_reports(named, errors=errors)


def multi_json(results, fc):
    """Serialise a multi-scan for the non-streaming JSON response (no report bodies)."""
    return {
        "repos": [{k: v for k, v in r.items() if k != "report"} for r in results],
        "comparison": {
            "repos": fc.repos,
            "divergent_count": len(fc.divergent),
            "shared_count": len(fc.shared),
            "errors": fc.errors,
            "rows": [{
                "ecosystem": row.ecosystem, "name": row.name, "latest": row.latest,
                "diverges": row.diverges, "worst": row.worst,
                "pins": {repo: pin.versions for repo, pin in row.pins.items()},
            } for row in fc.rows],
        },
    }


def scan_request(
    req: dict,
    *,
    cache_dir: str | None = None,
    allow_hosts: tuple[str, ...] = (),
    on_progress=None,
) -> tuple[int, str, bytes, dict]:
    """Clone, scan, and render one request. Returns (status, content_type, body, headers).

    Errors come back as JSON with an `error` field and a 4xx/5xx status; a scan
    that ran comes back in the requested `format`. Nothing here raises for a bad
    request -- the HTTP shell must be able to answer every call.

    `on_progress`, if given, is called with `{"phase": ...}` dicts as the scan moves
    through cloning, per-ecosystem resolution, deltas, vulns, and rendering. The
    streaming handler forwards these to the client as SSE; the plain path ignores it.
    """
    _p = on_progress or (lambda _e: None)

    def err(code: int, message: str):
        return code, "application/json; charset=utf-8", json.dumps({"error": message}).encode(), {}

    fmt = str(req.get("format") or "json").lower()
    if fmt != "html" and fmt not in _RENDERERS:
        return err(400, f"unknown format {fmt!r}; use json, markdown, html, or text")

    status, report, error = produce_report(
        req, cache_dir=cache_dir, allow_hosts=allow_hosts, on_progress=on_progress
    )
    if report is None:
        return err(status, error)

    _p({"phase": "rendering", "format": fmt})
    content_type, body, headers = render_report(report, fmt)
    return 200, content_type, body, headers


_USAGE = {
    "service": "driftscope",
    "endpoints": {
        "GET /healthz": "liveness probe",
        "GET /": "this message (or the browser UI if the client accepts text/html)",
        "GET /ui": "browser UI for running scans",
        "POST /scan": "clone a repo and return a drift report",
        "POST /scan-multi": "scan several repos {\"repos\":[...]} and compare their drift",
    },
    "POST /scan body": {
        "repo": "https://github.com/org/repo   (required, remote git URL)",
        "ref": "branch or tag (optional)",
        "format": "json | markdown | html | text   (default json)",
        "ecosystems": "optional list, e.g. ['python','docker']",
        "offline": "bool", "no_github": "bool", "no_az": "bool",
        "include_vulns": "bool (default true)", "velocity": "bool (default false)",
        "ado_org": "Azure DevOps org for owner/repo shorthand (optional)",
    },
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "driftscope"
    protocol_version = "HTTP/1.1"

    # Config is stashed on the server instance by run_server.
    @property
    def _cfg(self):
        return self.server  # type: ignore[attr-defined]

    def _send(self, code: int, content_type: str, body: bytes, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict, headers: dict | None = None):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode(), headers)

    def _send_ui(self):
        from .ui import INDEX_HTML

        self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/ui":
            self._send_ui()
        elif path in ("", "/index"):
            # Same URL, two audiences: a browser (Accept: text/html) gets the visual
            # UI; curl and agents get the JSON usage doc unchanged.
            if "text/html" in (self.headers.get("Accept") or ""):
                self._send_ui()
            else:
                self._json(200, _USAGE)
        elif path == "/healthz":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": f"no route {self.path!r}"})

    def do_POST(self):
        parsed = self.path.split("?", 1)
        path = parsed[0].rstrip("/")
        query = parsed[1] if len(parsed) > 1 else ""
        if path not in ("/scan", "/scan-multi"):
            self._json(404, {"error": f"no route {self.path!r}"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._json(413, {"error": f"body over {MAX_BODY} bytes"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw or b"{}")
            if not isinstance(req, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as e:
            self._json(400, {"error": f"invalid JSON body: {e}"})
            return

        # One scan can spin up eight workers and clone a repo; without a gate a
        # burst of requests would fork-bomb the box. Reject rather than queue, so
        # a caller learns immediately instead of hanging on an invisible backlog.
        sem: threading.Semaphore = self.server.ds_sem  # type: ignore[attr-defined]
        if not sem.acquire(blocking=False):
            self._json(503, {"error": "server busy: too many concurrent scans"})
            return
        try:
            stream = "stream=1" in query
            if path == "/scan-multi":
                self._scan_multi(req, stream)
            elif stream:
                self._scan_streaming(req)
            else:
                code, content_type, body, headers = scan_request(
                    req,
                    cache_dir=self.server.ds_cache_dir,  # type: ignore[attr-defined]
                    allow_hosts=self.server.ds_allow_hosts,  # type: ignore[attr-defined]
                )
                self._send(code, content_type, body, headers)
        finally:
            sem.release()

    def _scan_multi(self, req, stream):
        repos = req.get("repos")
        if not isinstance(repos, list) or not repos:
            self._json(400, {"error": "body must include a non-empty 'repos' list of git URLs"})
            return
        if len(repos) > MAX_REPOS:
            self._json(400, {"error": f"too many repos: {len(repos)} > {MAX_REPOS}"})
            return
        cache_dir = self.server.ds_cache_dir       # type: ignore[attr-defined]
        allow = self.server.ds_allow_hosts         # type: ignore[attr-defined]
        if not stream:
            results, fc = run_multi(req, cache_dir=cache_dir, allow_hosts=allow)
            self._json(200, multi_json(results, fc))
            return

        import queue
        from .report.compare_html import to_comparison_html

        q: queue.Queue = queue.Queue()

        def worker():
            try:
                results, fc = run_multi(req, cache_dir=cache_dir, allow_hosts=allow,
                                        on_progress=lambda ev: q.put(("progress", ev)))
                repos_out = []
                for r in results:
                    if r["ok"]:
                        _, body, hdrs = render_report(r["report"], "html")
                        repos_out.append({"repo": r["repo"], "ok": True,
                                          "html": body.decode("utf-8", "replace"),
                                          "severity": hdrs.get("X-Drift-Severity"),
                                          "commit": r.get("commit")})
                    else:
                        repos_out.append({"repo": r["repo"], "ok": False, "error": r.get("error")})
                payload = {"comparison_html": to_comparison_html(fc), "repos": repos_out}
            except Exception as e:  # a bug must not wedge the stream
                payload = {"error": f"{type(e).__name__}: {e}"}
            q.put(("done", payload))
            q.put(None)

        threading.Thread(target=worker, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def frame(event, data):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        try:
            while True:
                item = q.get()
                if item is None:
                    break
                frame(item[0], item[1])
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _scan_streaming(self, req):
        """Run the scan in a worker thread, streaming phase events as SSE.

        Progress events (`cloning`, `resolving` per ecosystem, `deltas`, `vulns`,
        `rendering`) drive the client's live status; the final `done` event carries
        the rendered report so the page can display it in place.
        """
        import queue

        q: queue.Queue = queue.Queue()

        def worker():
            try:
                result = scan_request(
                    req,
                    cache_dir=self.server.ds_cache_dir,  # type: ignore[attr-defined]
                    allow_hosts=self.server.ds_allow_hosts,  # type: ignore[attr-defined]
                    on_progress=lambda ev: q.put(("progress", ev)),
                )
            except Exception as e:  # a scan bug must not wedge the stream
                result = (500, "application/json",
                          json.dumps({"error": str(e)}).encode(), {})
            q.put(("done", result))
            q.put(None)

        threading.Thread(target=worker, daemon=True).start()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def frame(event, data):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        try:
            while True:
                item = q.get()
                if item is None:
                    break
                kind, payload = item
                if kind == "progress":
                    frame("progress", payload)
                else:
                    status, content_type, body, headers = payload
                    frame("done", {
                        "status": status,
                        "content_type": content_type,
                        "severity": headers.get("X-Drift-Severity"),
                        "commit": headers.get("X-Drift-Commit"),
                        "body": body.decode("utf-8", "replace"),
                    })
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected; let the worker finish and be GC'd

    def log_message(self, fmt, *args):  # concise one-liner to stderr
        import sys
        sys.stderr.write(f"driftscope-server: {self.address_string()} {fmt % args}\n")


def run_server(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    max_concurrent: int = 4,
    allow_hosts: tuple[str, ...] = (),
    cache_dir: str | None = None,
) -> None:
    """Serve until interrupted. Threaded so a slow scan does not block liveness."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.ds_sem = threading.Semaphore(max(1, max_concurrent))  # type: ignore[attr-defined]
    httpd.ds_allow_hosts = tuple(allow_hosts)  # type: ignore[attr-defined]
    httpd.ds_cache_dir = cache_dir or os.environ.get("DRIFTSCOPE_CACHE")  # type: ignore[attr-defined]
    import sys
    print(
        f"driftscope serving on http://{host}:{port} "
        f"(max_concurrent={max_concurrent}, "
        f"allow_hosts={list(allow_hosts) or 'ALL'})",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
