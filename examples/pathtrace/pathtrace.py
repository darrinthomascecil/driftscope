#!/usr/bin/env python3
"""pathtrace -- a self-diagnostic page that proves the edge->app network path.

Each hop from the internet to this pod leaves fingerprints in the request headers
(Cloudflare's cf-ray, Envoy's x-request-id, the mTLS x-forwarded-client-cert, the
SSO x-auth-request-email, ...). This app reads those fingerprints off the request it
just received and, for every hop, shows: what the hop does, the live evidence that it
happened, a verdict, and the command you'd run to debug it if it hadn't.

Stdlib only, like driftscope. Serves any path (it lives behind a /trace route); /healthz
is the pod-local probe; ?format=json dumps the raw evidence.
"""
from __future__ import annotations
import html
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POD = os.environ.get("HOSTNAME", "unknown")
NS = os.environ.get("POD_NAMESPACE", "driftscope")


def _xfcc(v: str) -> dict:
    """Parse an x-forwarded-client-cert header into its key/values (By, URI, ...)."""
    out = {}
    for part in v.split(";"):
        if "=" in part:
            k, _, val = part.partition("=")
            out[k.strip().lower()] = val.strip().strip('"')
    return out


def evaluate(headers) -> list[dict]:
    """Build the per-hop evidence from the request headers. h() is case-insensitive."""
    def h(name, default=""):
        return headers.get(name, default)

    cf_ray = h("cf-ray")
    cf_ip = h("cf-connecting-ip")
    cf_visitor = h("cf-visitor")
    xff = h("x-forwarded-for")
    xfproto = h("x-forwarded-proto")
    req_id = h("x-request-id")
    envoy_ext = h("x-envoy-external-address")
    xfcc = h("x-forwarded-client-cert")
    xfcc_p = _xfcc(xfcc)
    auth_email = h("x-auth-request-email") or h("x-auth-request-user")
    have_authz = bool(h("authorization"))

    gw_principal = xfcc_p.get("uri", "")
    is_gateway = "aks-istio-ingress" in gw_principal

    return [
        {
            "n": 0, "title": "Client + DNS",
            "what": "The browser resolves this hostname to a Cloudflare edge IP (proxied record).",
            "ok": bool(h("host")),
            "evidence": {"Host": h("host") or "(none)"},
            "note": "The Host that survived resolution + SNI. A non-Cloudflare answer would mean the record was un-proxied.",
            "debug": "dig +short driftscope.justmakeitgo.net @1.1.1.1   # vs the local resolver; mismatch = stale cache, not DNS",
        },
        {
            "n": 1, "title": "Cloudflare edge (TLS · WAF · DDoS)",
            "what": "Terminates TLS and screens the request, then hands it to the tunnel.",
            "ok": bool(cf_ray),
            "evidence": {"cf-ray": cf_ray or "(absent)", "cf-connecting-ip": cf_ip or "(absent)",
                         "cf-visitor": cf_visitor or "(absent)"},
            "note": "cf-ray present = the request genuinely transited Cloudflare. cf-visitor shows the original scheme (https).",
            "debug": "curl -sv https://driftscope.justmakeitgo.net/ 2>&1 | grep -Ei 'cf-ray|server:'   # 5xx WITH cf-ray = edge ok, origin sick",
        },
        {
            "n": 2, "title": "Cloudflare Tunnel (cloudflared)",
            "what": "cloudflared dials OUT to Cloudflare; the edge forwards this request down that tunnel. No public origin.",
            "ok": bool(xff),
            "evidence": {"x-forwarded-for": xff or "(absent)", "x-forwarded-proto": xfproto or "(absent)"},
            "note": "The forwarded-for chain is the client IP as cloudflared passed it inward.",
            "debug": "kubectl -n driftscope logs deploy/cloudflared | grep -Ei 'registered|error'   # ~4 connectors = healthy",
        },
        {
            "n": 3, "title": "Istio internal ingress gateway",
            "what": "cloudflared forwards to the gateway Service (internal LB, no public IP); Envoy takes over.",
            "ok": bool(req_id),
            "evidence": {"x-request-id": req_id or "(absent)", "x-envoy-external-address": envoy_ext or "(absent)"},
            "note": "x-request-id / x-envoy-* are stamped by the gateway's Envoy -- proof the request passed through it.",
            "debug": "kubectl -n default run t --image=curlimages/curl --restart=Never -- sleep 3600; "
                     "kubectl -n default exec t -- curl -s -o /dev/null -w '%{http_code}' "
                     "-H 'Host: driftscope.justmakeitgo.net' http://aks-istio-ingressgateway-internal.aks-istio-ingress.svc:80/trace   # 302",
        },
        {
            "n": 4, "title": "Gateway policy -> ext_authz (SSO)",
            "what": "The CUSTOM AuthorizationPolicy calls oauth2-proxy for every non-/oauth2 path; unauthenticated => 302 to Entra.",
            "ok": bool(auth_email),
            "evidence": {"x-auth-request-email": auth_email or "(absent)", "authorization": "Bearer <token>" if have_authz else "(absent)"},
            "note": "You are only reading this because SSO passed. oauth2-proxy injected your identity as upstream headers.",
            "debug": "kubectl -n aks-istio-ingress exec <gw-pod> -c istio-proxy -- pilot-agent request GET stats | grep ext_authz   # error>0 = oauth2-proxy unreachable",
        },
        {
            "n": 5, "title": "Gateway -> app: mTLS + workload authz",
            "what": "The gateway originates mTLS; the app admits ONLY the gateway's SPIFFE identity (driftscope-allow-gateway-only).",
            "ok": is_gateway,
            "evidence": {"x-forwarded-client-cert (By)": xfcc_p.get("by", "(absent)"),
                         "x-forwarded-client-cert (URI = caller)": gw_principal or "(absent)"},
            "note": "URI is the caller's cryptographic identity. It MUST be the ingress gateway SA -- any other mesh pod gets 403.",
            "debug": "kubectl -n driftscope exec <app-pod> -c istio-proxy -- pilot-agent request GET stats/prometheus "
                     "| grep connection_security_policy   # mutual_tls, never 'none'",
        },
        {
            "n": 6, "title": "App container",
            "what": "This pod (pathtrace) finally handles the request over localhost from its sidecar.",
            "ok": True,
            "evidence": {"pod": POD, "namespace": NS, "served_by": "pathtrace :8080"},
            "note": "Reached over localhost, so app bugs reproduce with the mesh out of the picture (kubectl exec ... curl localhost:8080).",
            "debug": f"kubectl -n {NS} logs {POD} -c pathtrace -f   # the app's own logs (‑c pathtrace, not istio-proxy)",
        },
    ]


def render_html(hops: list[dict]) -> bytes:
    rows = []
    for hop in hops:
        badge = "ok" if hop["ok"] else "warn"
        label = "DETECTED" if hop["ok"] else "NOT DETECTED"
        ev = "".join(
            f'<div class="kv"><span class="k">{html.escape(k)}</span>'
            f'<span class="v">{html.escape(str(val))}</span></div>'
            for k, val in hop["evidence"].items()
        )
        rows.append(f"""
        <section class="hop {badge}">
          <header><span class="num">{hop['n']}</span>
            <h2>{html.escape(hop['title'])}</h2>
            <span class="badge {badge}">{label}</span></header>
          <p class="what">{html.escape(hop['what'])}</p>
          <div class="evidence">{ev}</div>
          <p class="note">{html.escape(hop['note'])}</p>
          <details><summary>How to debug this hop</summary>
            <pre>{html.escape(hop['debug'])}</pre></details>
        </section>""")
    body = "\n".join(rows)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pathtrace — edge to app</title>
<style>
 :root {{ color-scheme: light dark; --bg:#0b0e14; --card:#141a24; --fg:#e6edf3; --muted:#8b97a7;
          --ok:#2ea043; --warn:#d29922; --line:#232c39; --code:#0a0d12; }}
 @media (prefers-color-scheme: light) {{ :root {{ --bg:#f6f8fa; --card:#fff; --fg:#1f2328;
          --muted:#57606a; --line:#d0d7de; --code:#f6f8fa; }} }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }}
 .wrap {{ max-width:820px; margin:0 auto; padding:32px 20px 64px; }}
 h1 {{ font-size:24px; margin:0 0 4px; }}
 .sub {{ color:var(--muted); margin:0 0 28px; }}
 .hop {{ background:var(--card); border:1px solid var(--line); border-left:4px solid var(--line);
         border-radius:10px; padding:16px 18px; margin:14px 0; }}
 .hop.ok {{ border-left-color:var(--ok); }}
 .hop.warn {{ border-left-color:var(--warn); }}
 .hop header {{ display:flex; align-items:center; gap:10px; }}
 .num {{ width:26px; height:26px; flex:none; border-radius:50%; background:var(--line); color:var(--fg);
         display:grid; place-items:center; font-weight:700; font-size:13px; }}
 h2 {{ font-size:16px; margin:0; flex:1; }}
 .badge {{ font-size:11px; font-weight:700; letter-spacing:.04em; padding:3px 8px; border-radius:20px; }}
 .badge.ok {{ background:rgba(46,160,67,.16); color:var(--ok); }}
 .badge.warn {{ background:rgba(210,153,34,.16); color:var(--warn); }}
 .what {{ margin:10px 0 12px; }}
 .evidence {{ background:var(--code); border:1px solid var(--line); border-radius:8px; padding:8px 10px; }}
 .kv {{ display:flex; gap:10px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; padding:2px 0; }}
 .k {{ color:var(--muted); min-width:230px; flex:none; }}
 .v {{ color:var(--fg); word-break:break-all; }}
 .note {{ color:var(--muted); font-size:13.5px; margin:12px 0 8px; }}
 details {{ margin-top:6px; }}
 summary {{ cursor:pointer; color:var(--muted); font-size:13px; }}
 pre {{ background:var(--code); border:1px solid var(--line); border-radius:8px; padding:10px;
        overflow-x:auto; font-size:12px; margin:8px 0 0; }}
 footer {{ color:var(--muted); font-size:12.5px; margin-top:28px; }}
 a {{ color:#539bf5; }}
</style></head><body><div class="wrap">
 <h1>pathtrace</h1>
 <p class="sub">You are reading this from inside pod <code>{html.escape(POD)}</code>. Every hop below was
   verified from the fingerprints your request carried in. Green = the hop's evidence is present.</p>
 {body}
 <footer>Raw evidence as JSON: <a href="?format=json">?format=json</a>. Each hop maps to
   <code>docs/runbook-edge-to-app.md</code>.</footer>
</div></body></html>"""
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/healthz") or self.path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        hops = evaluate(self.headers)
        if "format=json" in self.path:
            payload = json.dumps({"pod": POD, "hops": hops}, indent=2).encode()
            return self._send(200, payload, "application/json")
        return self._send(200, render_html(hops))

    def log_message(self, fmt, *args):  # quiet, single-line
        print("pathtrace %s - %s" % (self.address_string(), fmt % args))


def main():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"pathtrace listening on :{port} (pod {POD})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
