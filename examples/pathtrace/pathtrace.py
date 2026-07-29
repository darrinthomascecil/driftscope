#!/usr/bin/env python3
"""pathtrace -- a vendor-neutral edge->app network path tracer.

Each hop is a ROLE (edge/CDN, forwarding chain, ingress, auth, service identity, app),
not a specific product. For every role we try an ordered list of vendor detectors
(Cloudflare, CloudFront, Front Door, Fastly, Akamai, Istio/Envoy, Linkerd, oauth2-proxy,
Cloudflare Access, Google IAP, ALB OIDC, Azure Easy Auth, ...), then fall back to
vendor-neutral signals (Via, Forwarded, X-Forwarded-For depth, W3C traceparent). We
report WHICH signal matched, so the page self-identifies the stack instead of assuming
one. A hop that leaves no fingerprint is reported as "not seen" (transparent or absent)
-- deliberately NOT "failed", because absence of evidence isn't evidence of a broken hop.

Stdlib only. /healthz is the pod probe; ?format=json dumps the raw evidence.
"""
from __future__ import annotations
import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POD = os.environ.get("HOSTNAME", "unknown")
NS = os.environ.get("POD_NAMESPACE", "driftscope")


def _xfcc(v: str) -> dict:
    out = {}
    for part in v.split(";"):
        if "=" in part:
            k, _, val = part.partition("=")
            out[k.strip().lower()] = val.strip().strip('"')
    return out


def _shape(present: dict) -> dict:
    """Expand identity-bearing headers into something readable."""
    out = {}
    for k, v in present.items():
        if k == "x-forwarded-client-cert":
            p = _xfcc(v)
            out["client-cert By"] = p.get("by", "(unparsed)")
            out["client-cert URI (caller)"] = p.get("uri", "(none)")
        else:
            out[k] = v
    return out


# Each hop's detectors are tried in order; first vendor with ANY of its signals present
# wins. If none match, the vendor-neutral `generic` signals are tried. This table is the
# whole point: adding an environment means adding a row, not changing logic.
def hops_spec() -> list[dict]:
    return [
        {
            "n": 0, "role": "Client + DNS", "kind": "fixed",
            "plain": "Before anything can connect, the client turns the hostname into an address "
                     "-- that is DNS, like looking a company up in a phone book to get its number "
                     "before you call. If the record is proxied, that number rings an edge, not the "
                     "app's own line.",
            "note": "The Host that survived resolution + SNI. Generalizes to any stack -- every request arrives with one.",
            "failure": "You never even reach this page: 'server not found', and no edge/proxy signals appear at all "
                       "because nothing resolved. Classic false alarm -- an authoritative resolver (dig @1.1.1.1) "
                       "answers but the client does not: a stale negative cache, not a real outage.",
            "debug": "dig +short <host> @1.1.1.1   # vs the local resolver; mismatch = stale cache, not DNS",
        },
        {
            "n": 1, "role": "Edge / CDN", "kind": "detect",
            "detectors": [
                ("Cloudflare", ["cf-ray", "cf-connecting-ip", "cf-visitor"]),
                ("AWS CloudFront", ["x-amz-cf-id", "x-amz-cf-pop"]),
                ("Azure Front Door", ["x-azure-ref", "x-fd-healthprobe"]),
                ("Fastly", ["x-served-by", "fastly-io-info"]),
                ("Akamai", ["akamai-grn", "x-akamai-request-id"]),
            ],
            "generic": ["via", "x-cache"],
            "plain": "The building's front reception: TLS is terminated, the request is screened (WAF) and "
                     "shielded from floods (DDoS) before a byte reaches your origin. Different vendors sign "
                     "their work with different headers -- the evidence below names whichever one answered.",
            "note": "A branded header (cf-ray, x-amz-cf-id, x-azure-ref, ...) names the CDN; a bare Via means a proxy we can't identify.",
            "failure": "A vendor-branded error page instead of the app (e.g. Cloudflare 525 TLS-handshake, a WAF 403). "
                       "The tell: it still carries the edge's signature header, so the edge is up and the fault is "
                       "config or downstream, not your network.",
            "debug": "curl -sv https://<host>/ 2>&1 | grep -Ei 'cf-ray|x-amz-cf-id|x-azure-ref|via|server:'",
        },
        {
            "n": 2, "role": "Forwarding / proxy chain", "kind": "chain",
            "generic": ["forwarded", "x-forwarded-for", "x-forwarded-proto"],
            "plain": "How many proxies stand between you and the app. The standard way to count them is "
                     "vendor-neutral: RFC 7239 Forwarded, or the length of the X-Forwarded-For chain. In "
                     "this deployment one of those hops is a private outbound tunnel with no public door.",
            "note": "Depth = number of proxies the request admits to crossing. Works everywhere; needs no branded header.",
            "failure": "The chain is wrong or broken: a 502/1033 with the edge signature present but no healthy next "
                       "hop, or an XFF depth that doesn't match your topology (a proxy you didn't expect, or one "
                       "silently missing). Spoofable XFF is also how naive IP allowlists get bypassed.",
            "debug": "echo $X_FORWARDED_FOR | tr ',' '\\n' | nl   # count/inspect the proxy chain",
        },
        {
            "n": 3, "role": "Ingress / gateway", "kind": "detect",
            "detectors": [
                ("Istio / Envoy", ["x-envoy-external-address", "x-envoy-attempt-count", "x-envoy-decorator-operation"]),
                ("Contour / Envoy", ["x-envoy-upstream-service-time"]),
            ],
            "generic": ["x-request-id", "traceparent", "x-b3-traceid", "server"],
            "plain": "Inside the building, the mailroom: it receives the request, stamps it with a tracking id "
                     "so its path can be followed, and routes it to the right department. Envoy stamps x-envoy-*; "
                     "anything with a request-id or a W3C traceparent is at least a tracing-aware proxy.",
            "note": "x-envoy-* names Envoy specifically; a lone x-request-id / traceparent proves 'some ingress' without naming it.",
            "failure": "503 'no healthy upstream' or a 523: the gateway Service has no endpoints (pods down) or the "
                       "wrong port is named. An in-cluster curl to the gateway returns 000 (connection refused) "
                       "instead of a routed response.",
            "debug": "kubectl -n <gw-ns> get endpoints <gateway-svc>   # empty = no backends; then curl the svc from a debug pod",
        },
        {
            "n": 4, "role": "Authentication (IAP / SSO)", "kind": "detect",
            "detectors": [
                ("oauth2-proxy", ["x-auth-request-email", "x-auth-request-user"]),
                ("Cloudflare Access", ["cf-access-authenticated-user-email", "cf-access-jwt-assertion"]),
                ("Google IAP", ["x-goog-authenticated-user-email", "x-goog-iap-jwt-assertion"]),
                ("AWS ALB OIDC", ["x-amzn-oidc-identity", "x-amzn-oidc-data"]),
                ("Azure Easy Auth", ["x-ms-client-principal-name", "x-ms-client-principal"]),
            ],
            "generic": ["authorization"],
            "plain": "The badge checkpoint. Before you go further, your identity is verified against a directory "
                     "and stamped onto the request. Every identity-aware proxy injects a different header -- "
                     "oauth2-proxy, Cloudflare Access, Google IAP, ALB, Azure Easy Auth -- but the ROLE is the same.",
            "note": "You are only reading this because auth passed; the header below is the identity the proxy vouched for. A bare Authorization means a token but no proxy-verified identity.",
            "failure": "A healthy redirect to the IdP is the gate working; the FAILURE is a 403 'access denied' or an "
                       "endless login loop when the proxy can't be reached and fails closed -- or a callback 500 "
                       "(e.g. Entra AADSTS7000215) from a bad client secret. Either way, no identity header appears.",
            "debug": "curl -sI https://<host>/ | grep -i location   # unauth should redirect to the IdP, not 200",
        },
        {
            "n": 5, "role": "Service identity / mTLS", "kind": "detect",
            "detectors": [
                ("Istio mTLS (SPIFFE)", ["x-forwarded-client-cert"]),
                ("Linkerd mTLS", ["l5d-client-id"]),
            ],
            "generic": [],
            "plain": "A secure room where both sides present cryptographic ID -- mutual TLS, which can't be faked. "
                     "The caller's identity is the SPIFFE URI (Istio) or l5d-client-id (Linkerd). Good policy admits "
                     "only ONE caller here (the gateway); any other in-mesh workload is refused even with a valid cert.",
            "note": "The URI/id is the caller's cryptographic identity. Absent = not a mesh, or mTLS not surfaced to the app.",
            "failure": "403 'RBAC: access denied' at the app: if the allow-policy principal is wrong, even the gateway "
                       "is refused and every request 403s right after login. A broken handshake instead shows as a "
                       "connection reset / 503 UC; the identity header would be absent or a non-gateway principal.",
            "debug": "kubectl -n <app-ns> exec <app-pod> -c istio-proxy -- pilot-agent request GET stats/prometheus | grep connection_security_policy",
        },
        {
            "n": 6, "role": "App", "kind": "fixed",
            "plain": "The desk that finally does the work. By the time the request lands here it's been handed off "
                     "inside the same host (over localhost), so if the answer is wrong you know it's this worker -- "
                     "not the couriers, checkpoints, or mailroom that got it here.",
            "note": "Reached over localhost, so app bugs reproduce with the network out of the picture.",
            "failure": "The path is fine but the app itself fails: a 500 traceback in its logs, a crash-loop (503 "
                       "'no healthy upstream' from the sidecar), or OOM under load. Sometimes a 200 with a wrong "
                       "answer -- an app bug, not plumbing.",
            "debug": "kubectl -n <app-ns> logs <app-pod> -c <app-container> -f   # the app's own logs",
        },
    ]


def evaluate(headers) -> list[dict]:
    h = {k.lower(): v for k, v in headers.items()}
    results = []
    for spec in hops_spec():
        r = dict(spec)
        if spec["kind"] == "fixed" and spec["n"] == 0:
            r.update(status="ok", via="request received",
                     evidence={"Host": h.get("host", "(none)")})
        elif spec["kind"] == "fixed":  # app
            r.update(status="ok", via=f"pod {POD}",
                     evidence={"pod": POD, "namespace": NS, "served_by": "pathtrace :8080"})
        elif spec["kind"] == "chain":
            xff = h.get("x-forwarded-for", "")
            depth = len([x for x in xff.split(",") if x.strip()]) if xff else 0
            ev = {k: h[k] for k in spec["generic"] if h.get(k)}
            if depth:
                r.update(status="ok", via=f"{depth} proxy hop(s)", evidence=ev or {"x-forwarded-for": xff})
            elif ev:
                r.update(status="generic", via="proxy chain (no XFF)", evidence=ev)
            else:
                r.update(status="unknown", via=None, evidence={"forwarded / x-forwarded-for": "(absent)"})
        else:  # detect
            matched = None
            for vendor, names in spec.get("detectors", []):
                present = {k: h[k] for k in names if h.get(k)}
                if present:
                    matched = (vendor, present); break
            if matched:
                r.update(status="ok", via=matched[0], evidence=_shape(matched[1]))
            else:
                gen = {k: h[k] for k in spec.get("generic", []) if h.get(k)}
                if gen:
                    r.update(status="generic", via="unidentified (generic signal)", evidence=_shape(gen))
                else:
                    looked = [n for _, ns in spec.get("detectors", []) for n in ns][:3] + spec.get("generic", [])[:2]
                    r.update(status="unknown", via=None,
                             evidence={", ".join(looked[:4]) or "(no signals)": "(none present)"})
        results.append(r)
    return results


def summarize(results) -> str:
    seg = []
    for r in results:
        if r["status"] == "unknown":
            seg.append("· (not seen)")
        elif r["n"] == 0:
            seg.append("client")
        elif r["n"] == 6:
            seg.append("app")
        else:
            seg.append(r["via"])
    return "  →  ".join(seg)


STATUS = {"ok": ("detected", "ok"), "generic": ("generic", "generic"), "unknown": ("not seen", "unknown")}


def render_html(results) -> bytes:
    known = sum(1 for r in results if r["status"] == "ok")
    generic = sum(1 for r in results if r["status"] == "generic")
    unseen = sum(1 for r in results if r["status"] == "unknown")
    rows = []
    for r in results:
        label, cls = STATUS[r["status"]]
        via = html.escape(r["via"]) if r["via"] else "no fingerprint"
        ev = "".join(
            f'<div class="kv"><span class="k">{html.escape(k)}</span>'
            f'<span class="v">{html.escape(str(val))}</span></div>'
            for k, val in r["evidence"].items()
        )
        rows.append(f"""
        <section class="hop {cls}">
          <span class="num">{r['n']}</span>
          <div class="body">
            <header>
              <h2>{html.escape(r['role'])}</h2>
              <span class="via {cls}">{via}</span>
              <span class="badge {cls}">{label}</span>
            </header>
            <div class="detail"><span class="dlabel">In plain terms</span>{html.escape(r['plain'])}</div>
            <div class="evidence">{ev}</div>
            <p class="note">{html.escape(r['note'])}</p>
            <div class="failure"><span class="flabel">If this hop fails</span>{html.escape(r['failure'])}</div>
            <details open><summary>How to debug this hop</summary>
              <pre><code>{html.escape(r['debug'])}</code></pre></details>
          </div>
        </section>""")
    body = "\n".join(rows)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pathtrace — edge to app</title>
<style>
 :root {{ color-scheme: light dark; --bg:#0b0e14; --card:#141a24; --fg:#e6edf3; --muted:#8b97a7;
          --ok:#3fb950; --gen:#539bf5; --unk:#7d8590; --warn:#d29922; --line:#222b38; --rail:#2b3543;
          --code:#090c11; --accent:#539bf5; --shadow:0 1px 2px rgba(0,0,0,.4); }}
 @media (prefers-color-scheme: light) {{ :root {{ --bg:#f4f6f9; --card:#fff; --fg:#1f2328;
          --muted:#5b6572; --line:#e2e7ee; --rail:#d7dee7; --code:#f6f8fa; --accent:#0969da;
          --unk:#8b949e; --shadow:0 1px 2px rgba(140,149,159,.2); }} }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; font:15px/1.55 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,sans-serif;
         background:var(--bg); color:var(--fg); -webkit-font-smoothing:antialiased; }}
 .wrap {{ max-width:790px; margin:0 auto; padding:40px 20px 72px; }}
 h1 {{ font-size:26px; letter-spacing:-.01em; margin:0 0 6px; }}
 .sub {{ color:var(--muted); margin:0; max-width:62ch; }}
 .sub code {{ background:var(--code); padding:1px 6px; border-radius:5px; font-size:12.5px; }}
 .path {{ margin:22px 0 4px; padding:12px 14px; background:var(--card); border:1px solid var(--line);
          border-radius:10px; box-shadow:var(--shadow); font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
          font-size:12.5px; overflow-x:auto; white-space:nowrap; }}
 .path .lbl {{ color:var(--muted); }}
 .tally {{ margin:12px 0 4px; font-size:13px; color:var(--muted); display:flex; gap:16px; flex-wrap:wrap; }}
 .tally b {{ color:var(--fg); }}
 .pill {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; vertical-align:middle; }}
 .pill.ok {{ background:var(--ok); }} .pill.generic {{ background:var(--gen); }} .pill.unknown {{ background:var(--unk); }}
 .hops {{ position:relative; margin-top:22px; }}
 .hops::before {{ content:''; position:absolute; left:19px; top:14px; bottom:14px; width:2px; background:var(--rail); }}
 .hop {{ position:relative; display:flex; gap:18px; }}
 .num {{ position:relative; z-index:1; flex:none; width:40px; height:40px; border-radius:50%; display:grid;
         place-items:center; font-weight:700; font-size:15px; background:var(--card); border:2px solid var(--rail);
         color:var(--muted); box-shadow:var(--shadow); }}
 .hop.ok .num {{ border-color:var(--ok); color:var(--ok); }}
 .hop.generic .num {{ border-color:var(--gen); color:var(--gen); }}
 .hop.unknown .num {{ border-color:var(--unk); color:var(--unk); border-style:dashed; }}
 .body {{ flex:1; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px;
          margin-bottom:16px; box-shadow:var(--shadow); min-width:0; }}
 header {{ display:flex; align-items:center; gap:9px; margin-bottom:8px; flex-wrap:wrap; }}
 h2 {{ font-size:16px; margin:0; flex:1; letter-spacing:-.005em; }}
 .via {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11.5px; color:var(--muted);
         background:var(--code); border:1px solid var(--line); border-radius:6px; padding:2px 7px; }}
 .badge {{ font-size:11px; font-weight:700; letter-spacing:.05em; text-transform:uppercase; padding:3px 9px; border-radius:999px; flex:none; }}
 .badge.ok {{ background:rgba(63,185,80,.15); color:var(--ok); }}
 .badge.generic {{ background:rgba(83,155,245,.15); color:var(--gen); }}
 .badge.unknown {{ background:rgba(125,133,144,.15); color:var(--unk); }}
 .detail {{ background:rgba(83,155,245,.06); border-left:3px solid var(--accent); border-radius:0 8px 8px 0;
            padding:11px 14px; margin:0 0 14px; font-size:13.5px; }}
 .dlabel {{ display:block; font-size:10.5px; font-weight:700; letter-spacing:.07em; text-transform:uppercase; color:var(--accent); margin-bottom:5px; }}
 .evidence {{ background:var(--code); border:1px solid var(--line); border-radius:9px; padding:4px 12px; }}
 .kv {{ display:flex; gap:14px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; padding:6px 0; border-bottom:1px solid var(--line); }}
 .kv:last-child {{ border-bottom:0; }}
 .k {{ color:var(--muted); min-width:210px; flex:none; }}
 .v {{ color:var(--fg); word-break:break-all; }}
 .note {{ color:var(--muted); font-size:13px; margin:12px 0 10px; }}
 .failure {{ background:rgba(210,153,34,.07); border-left:3px solid var(--warn); border-radius:0 8px 8px 0;
             padding:11px 14px; margin:12px 0 4px; font-size:13.5px; }}
 .flabel {{ display:block; font-size:10.5px; font-weight:700; letter-spacing:.07em; text-transform:uppercase; color:var(--warn); margin-bottom:5px; }}
 details {{ border-top:1px dashed var(--line); padding-top:10px; margin-top:12px; }}
 summary {{ cursor:pointer; color:var(--muted); font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; list-style:none; }}
 summary::-webkit-details-marker {{ display:none; }}
 summary::before {{ content:'▾ '; }}
 pre {{ background:var(--code); border:1px solid var(--line); border-radius:9px; padding:12px 14px; overflow-x:auto; font-size:12px; line-height:1.6; margin:10px 0 0; }}
 pre code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--fg); }}
 footer {{ color:var(--muted); font-size:12.5px; margin-top:32px; padding-top:16px; border-top:1px solid var(--line); }}
 a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
</style></head><body><div class="wrap">
 <h1>pathtrace</h1>
 <p class="sub">Each hop is a <em>role</em> in the network path, detected from whatever fingerprint your
   stack leaves — vendor headers first, then neutral signals (Via, Forwarded, traceparent). Read live
   from the request you sent pod <code>{html.escape(POD)}</code>.</p>
 <div class="path"><span class="lbl">detected path:</span> {html.escape(summarize(results))}</div>
 <div class="tally">
   <span><span class="pill ok"></span><b>{known}</b> vendor-identified</span>
   <span><span class="pill generic"></span><b>{generic}</b> generic</span>
   <span><span class="pill unknown"></span><b>{unseen}</b> not seen</span>
 </div>
 <div class="hops">
 {body}
 </div>
 <footer>Roles + detector registry generalize this beyond one stack. Raw evidence:
   <a href="?format=json">?format=json</a> · companion to <code>docs/runbook-edge-to-app.md</code>.</footer>
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
        results = evaluate(self.headers)
        if "format=json" in self.path:
            slim = [{k: r[k] for k in ("n", "role", "status", "via", "evidence")} for r in results]
            return self._send(200, json.dumps({"pod": POD, "path": summarize(results), "hops": slim}, indent=2).encode(),
                              "application/json")
        return self._send(200, render_html(results))

    def log_message(self, fmt, *args):
        print("pathtrace %s - %s" % (self.address_string(), fmt % args))


def main():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"pathtrace listening on :{port} (pod {POD})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
