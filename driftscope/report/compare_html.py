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
.cmp th,.cmp td{border-bottom:1px solid var(--border-subtle,#8882);padding:6px 10px;text-align:left;white-space:nowrap}
.cmp thead th{position:sticky;top:0;background:var(--surface-1,#0000);font-weight:600}
.cmp tr.dv{background:color-mix(in srgb, var(--st-warning) 12%, transparent)}
.cmp td.behind{color:var(--st-critical)}
.cmp td.miss{color:var(--text-muted);opacity:.5}
.cmp .eco{color:var(--text-muted);font-size:11px}
.scroll{overflow-x:auto}
.sev-critical{color:var(--st-critical)}.sev-high{color:var(--st-serious,var(--st-critical))}
.sev-medium{color:var(--st-warning)}.sev-low,.sev-current{color:var(--text-secondary)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chip{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--border-subtle,#8884);color:var(--text-secondary)}
.chip.warn{border-color:var(--st-warning);color:var(--st-warning)}
"""


def _cell(pin, newest):
    if pin is None:
        return "<td class='miss'>—</td>"
    vers = ", ".join(e(v) for v in pin.versions)
    cls = "sev-" + pin.severity
    # "behind" only when the row is orderable and this repo lacks the newest pin.
    behind = newest is not None and newest not in pin.versions
    td_cls = "behind" if behind else ""
    return f"<td class='{td_cls}'><span class='{cls}'>{vers}</span></td>"


def to_comparison_html(fc: FleetComparison) -> str:
    a: list[str] = []
    ap = a.append
    ap("<div class='card'>")
    ap(f"<h1 style='margin:0 0 2px'>Fleet comparison</h1>")
    ap(f"<p class='note'>{len(fc.repos)} repos &middot; {len(fc.rows)} components "
       f"&middot; {len(fc.divergent)} diverging across repos</p>")

    ap("<div class='chips'>")
    ap(f"<span class='chip warn'>{len(fc.divergent)} diverging</span>")
    ap(f"<span class='chip'>{len(fc.shared)} shared (2+ repos)</span>")
    for r in fc.repos:
        ap(f"<span class='chip'>{e(r.rsplit('/', 1)[-1] or r)}</span>")
    ap("</div>")

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
    ap("<th>Latest</th></tr></thead><tbody>")

    for row in fc.rows:
        newest = newest_pin({v for p in row.pins.values() for v in p.versions}, row.ecosystem)
        cls = "dv" if row.diverges else ""
        ap(f"<tr class='{cls}' data-dv='{1 if row.diverges else 0}'>")
        eco = ECO_LABEL.get(row.ecosystem, row.ecosystem)
        ap(f"<td><b>{e(row.name)}</b> <span class='eco'>{e(eco)}</span></td>")
        for repo in fc.repos:
            ap(_cell(row.pins.get(repo), newest))
        ap(f"<td class='note'>{e(row.latest or '?')}</td></tr>")
    ap("</tbody></table></div>")
    ap("<p class='note'>Cells show each repo's declared pin; red means it lacks the "
       "newest pin present across the repos. \"Diverging\" means the repos pin more "
       "than one version between them &mdash; not that one is provably older when the "
       "version scheme can't be ordered.</p>")
    ap("</div>")

    js = ("<script>document.getElementById('dvonly').addEventListener('change',function(e){"
          "document.querySelectorAll('.cmp tbody tr').forEach(function(r){"
          "r.style.display=(e.target.checked && r.dataset.dv==='0')?'none':'';});});</script>")

    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Fleet comparison</title><style>{_CSS}{_CMP_CSS}</style></head>"
            f"<body><main class='report'>{''.join(a)}</main>{js}</body></html>")
