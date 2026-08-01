"""Thin HTTP surface over FleetService (stdlib http.server).

  POST /fleet/reports?tenant=T      body = report.json         -> 200 {verdict}
  GET  /fleet/verdict?tenant=T&repo=R                          -> 200 {verdict} | 404
  GET  /fleet/inventory?tenant=T&component=eco:name            -> 200 {uses:[[repo,version]]}

Tenant is a query param here (MVP). In production it comes from the authenticated
identity, not the caller's word -- same per-tenant token mechanism as private-repo clone
(docs/design-org-aggregation.md, open item #3). The verdict is computed on ingest and
stored, so the gate's GET always has an answer.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .evaluate import FleetService
from .ui import render_dashboard


def make_handler(service: FleetService, default_tenant: str = "default"):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, code: int, markup: str) -> None:
            body = markup.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _q(self):
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        def log_message(self, *_):  # quiet
            pass

        def do_POST(self):
            path = urlparse(self.path).path
            q = self._q()
            if path != "/fleet/reports":
                return self._send(404, {"error": "not found"})
            tenant = q.get("tenant", default_tenant)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                report = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            try:
                verdict = service.ingest(tenant, report, repo=q.get("repo"))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, verdict.to_dict())

        def do_GET(self):
            path = urlparse(self.path).path
            q = self._q()
            # Public, tenant-free endpoints first (health probe + dashboard).
            if path == "/healthz":
                return self._send(200, {"status": "ok"})
            if path == "/":
                return self._send_html(200, render_dashboard(service, q.get("tenant", default_tenant)))
            # Tenant-scoped JSON below.
            tenant = q.get("tenant", default_tenant)
            if path == "/fleet/verdict":
                v = service.verdict(tenant, q.get("repo", ""))
                return self._send(200, v) if v else self._send(404, {"error": "no verdict"})
            if path == "/fleet/inventory":
                uses = service.uses(tenant, q.get("component", ""))
                return self._send(200, {"component": q.get("component"), "uses": uses})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(service: FleetService, host: str = "127.0.0.1", port: int = 8081,
          default_tenant: str = "default"):
    httpd = ThreadingHTTPServer((host, port), make_handler(service, default_tenant))
    httpd.serve_forever()
