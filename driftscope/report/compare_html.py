"""Full-page HTML for the fleet comparison.

Styled with the single-repo report's CSS so it drops into the same iframe as the
per-repo tabs. It renders one row per component and one column per repo, with the
pinned version in each cell; rows where the repos disagree float to the top and are
highlighted (the whole point of the view). Zero dependencies.
"""

from __future__ import annotations

from ..compare import FleetComparison, newest_pin
from .html import _CSS, e
from .render import ECO_LABEL

_CMP_CSS = """
.cmp{border-collapse:collapse;width:100%;font-size:13px}
.cmp th,.cmp td{border-bottom:1px solid var(--rule,#8882);padding:6px 10px;text-align:left;white-space:nowrap}
.cmp thead th{position:sticky;top:0;background:var(--surface-1,#0000);font-weight:600}
.cmp tr.dv{background:color-mix(in srgb, var(--st-warning) 12%, transparent)}
.cmp td.behind{color:var(--st-critical)}
.cmp td.miss{color:var(--text-muted);opacity:.5}
.cmp .eco{color:var(--text-muted);font-size:11px}
.cmp .num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;margin-bottom:10px}
.sev-critical{color:var(--st-critical)}.sev-high{color:var(--st-serious,var(--st-critical))}
.sev-medium{color:var(--st-warning)}.sev-low,.sev-current{color:var(--text-secondary)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chip{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--rule,#8884);color:var(--text-secondary)}
.chip.warn{border-color:var(--st-warning);color:var(--st-warning)}
"""


_SEV_ORDER = ("critical", "high", "medium", "low", "current", "unknown")


def _cell(pin, newest):
    if pin is None:
        return "<td class='miss'>—</td>"
    vers = ", ".join(e(v) for v in pin.versions)
    cls = "sev-" + pin.severity
    # "behind" only when the row is orderable and this repo lacks the newest pin.
    behind = newest is not None and newest not in pin.versions
    td_cls = "behind" if behind else ""
    return f"<td class='{td_cls}'><span class='{cls}'>{vers}</span></td>"


def _posture(fc) -> str:
    """Per-repo severity roll-up -- the fleet's overall posture, which the
    component-by-component matrix does not show. The repo with the most
    critical/high findings is flagged as the worst."""
    if not fc.posture:
        return ""
    scored = lambda c: tuple(c.get(s, 0) for s in _SEV_ORDER)  # noqa: E731
    worst = max(fc.repos, key=lambda r: scored(fc.posture.get(r, {})))
    out = ["<div class='sub' style='margin:14px 0 4px;font-weight:600'>Fleet posture</div>",
           "<div class='scroll'><table class='cmp'><thead><tr><th>Repo</th>"]
    for s in _SEV_ORDER:
        out.append(f"<th class='sev-{s}'>{e(s)}</th>")
    out.append("</tr></thead><tbody>")
    for repo in fc.repos:
        c = fc.posture.get(repo, {})
        flag = repo == worst and (c.get("critical") or c.get("high"))
        name = e(repo.rsplit("/", 1)[-1] or repo)
        out.append(f"<tr><td><b>{name}</b>"
                   + (" <span class='sev-critical'>&larr; worst</span>" if flag else "") + "</td>")
        for s in _SEV_ORDER:
            n = c.get(s, 0)
            out.append(f"<td class='num {'sev-' + s if n else ''}'>{n or ''}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def to_comparison_html(fc: FleetComparison) -> str:
    a: list[str] = []
    ap = a.append
    ap("<h1 style='margin:0 0 2px'>Fleet comparison</h1>")
    ap(f"<p class='note'>{len(fc.repos)} repos &middot; {len(fc.rows)} components "
       f"&middot; {len(fc.divergent)} diverging across repos</p>")

    ap("<div class='chips'>")
    ap(f"<span class='chip warn'>{len(fc.divergent)} diverging</span>")
    ap(f"<span class='chip'>{len(fc.shared)} shared (2+ repos)</span>")
    ap("</div>")
    ap(_posture(fc))

    if fc.errors:
        ap("<div class='note' style='color:var(--st-critical)'>Could not scan: "
           + "; ".join(f"{e(k.rsplit('/', 1)[-1])} ({e(v)})" for k, v in fc.errors.items())
           + "</div>")

    ap("<label class='note' style='display:block;margin:6px 0'>"
       "<input type='checkbox' id='dvonly'> show only components that diverge</label>")

    ap("<div class='scroll'><table class='cmp'><thead><tr>")
    ap("<th>Component</th>")
    for r in fc.repos:
        ap(f"<th>{e(r.rsplit('/', 1)[-1] or r)}</th>")
    ap("<th class='num'>Uses</th><th>Align &rarr;</th><th>Latest</th></tr></thead><tbody>")

    for row in fc.rows:
        newest = newest_pin({v for p in row.pins.values() for v in p.versions}, row.ecosystem)
        cls = "dv" if row.diverges else ""
        ap(f"<tr class='{cls}' data-dv='{1 if row.diverges else 0}'>")
        eco = ECO_LABEL.get(row.ecosystem, row.ecosystem)
        ap(f"<td><b>{e(row.name)}</b> <span class='eco'>{e(eco)}</span></td>")
        for repo in fc.repos:
            ap(_cell(row.pins.get(repo), newest))
        ap(f"<td class='num note'>{row.repo_count}/{len(fc.repos)}</td>")
        ap("<td>" + (f"&rarr; <b>{e(row.align_to)}</b>" if row.align_to
                     else "<span class='note'>&mdash;</span>") + "</td>")
        ap(f"<td class='note'>{e(row.latest or '?')}</td></tr>")
    ap("</tbody></table></div>")
    ap("<p class='note'>Cells show each repo's declared pin; red means it lacks the "
       "newest pin present across the repos. <b>Uses</b> is how many repos pin the "
       "component (shared risk). <b>Align &rarr;</b> is the newest pin already in the "
       "fleet &mdash; a proven target to bring the laggards to, before aiming for "
       "<b>Latest</b>. \"Diverging\" means the repos pin more than one version between "
       "them, not that one is provably older when the scheme can't be ordered.</p>")

    js = ("<script>document.getElementById('dvonly').addEventListener('change',function(e){"
          "document.querySelectorAll('.cmp tbody tr').forEach(function(r){"
          "r.style.display=(e.target.checked && r.dataset.dv==='0')?'none':'';});});</script>")

    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Fleet comparison</title><style>{_CSS}{_CMP_CSS}</style></head>"
            f"<body class='ds'><div class='wrap'>{''.join(a)}</div>{js}</body></html>")
