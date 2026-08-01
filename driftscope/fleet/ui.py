"""A minimal built-in dashboard (GET /) so the deployed service is inspectable in a
browser behind the SSO edge. The full UI pod (docs/design-org-aggregation.md) is a later
slice; this is enough to see verdicts, violations and the learned baseline at a glance."""

from __future__ import annotations

import html

from .policy import fleet_baseline


def render_dashboard(service, tenant: str) -> str:
    e = lambda s: html.escape(str(s if s is not None else ""))
    recs = service.store.records(tenant)
    base = fleet_baseline(recs, service.policy.baseline)
    rows, blocked = [], 0
    for r in sorted(recs, key=lambda r: r.repo):
        v = service.verdict(tenant, r.repo) or {"status": "pass", "violations": []}
        if v["status"] == "block":
            blocked += 1
        colour = "#c0262e" if v["status"] == "block" else "#157f3c"
        vios = "".join(
            f"<div class=v><b>{e(x['kind'])}</b> {e(x['component'])} <code>{e(x.get('current'))}</code>"
            + (f" &rarr; {e(x.get('source'))} <code>{e(x.get('sanctioned'))}</code>"
               if x.get("sanctioned") else "")
            + f"<span class=d>{e(x.get('detail'))}</span></div>"
            for x in v["violations"]) or "<span class=d>&mdash; conforms &mdash;</span>"
        rows.append(
            f"<tr><td class=repo>{e(r.repo)}<span class=rel>{e(r.release)}</span></td>"
            f"<td><span class=st style='background:{colour}'>{e(v['status']).upper()}</span></td>"
            f"<td>{vios}</td></tr>")
    baseline = "".join(f"<li><code>{e(k)}</code> &rarr; <code>{e(val)}</code></li>"
                       for k, val in sorted(base.items())) or "<li class=d>none yet</li>"
    return f"""<!doctype html><meta charset=utf-8><title>driftscope fleet</title>
<style>
 body{{font:15px/1.55 -apple-system,system-ui,sans-serif;background:#0f1419;color:#e6edf3;margin:0;padding:36px 22px 80px}}
 .wrap{{max-width:1040px;margin:0 auto}} h1{{font-size:22px;margin:0 0 4px}} .d{{color:#8b98a5}}
 .sub{{color:#8b98a5;font-size:13px;margin-bottom:20px}}
 .tiles{{display:flex;gap:12px;margin:18px 0}} .tile{{background:#161b22;border:1px solid #263039;border-radius:10px;padding:12px 16px}}
 .tile .n{{font-size:26px;font-weight:650}} .tile .k{{color:#8b98a5;font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
 table{{width:100%;border-collapse:collapse;font-size:13.5px;background:#161b22;border:1px solid #263039;border-radius:10px;overflow:hidden}}
 th,td{{text-align:left;padding:11px 14px;border-bottom:1px solid #263039;vertical-align:top}}
 th{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#8b98a5}}
 .repo{{font-weight:600;white-space:nowrap}} .rel{{color:#8b98a5;font-weight:400;margin-left:8px;font-size:12px}}
 .st{{color:#fff;font:700 11px/1 ui-monospace,monospace;padding:4px 8px;border-radius:5px}}
 code{{font:12px ui-monospace,monospace;background:#0f1419;border:1px solid #263039;padding:1px 5px;border-radius:4px}}
 .v{{margin:3px 0}} .v .d{{margin-left:8px}} h2{{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#8b98a5;margin:26px 0 8px}}
 ul{{margin:6px 0;padding-left:20px}}
</style>
<div class=wrap>
 <h1>driftscope &mdash; fleet governance</h1>
 <div class=sub>tenant <code>{e(tenant)}</code> &middot; {len(recs)} repos</div>
 <div class=tiles>
  <div class=tile><div class=n>{len(recs)}</div><div class=k>repos</div></div>
  <div class=tile><div class=n style='color:#e2695f'>{blocked}</div><div class=k>blocked</div></div>
  <div class=tile><div class=n style='color:#54b072'>{len(recs) - blocked}</div><div class=k>passing</div></div>
 </div>
 <table><tr><th>repo</th><th>verdict</th><th>violations</th></tr>{''.join(rows) or '<tr><td colspan=3 class=d>no reports ingested yet</td></tr>'}</table>
 <h2>fleet baseline (learned)</h2><ul>{baseline}</ul>
</div>"""
