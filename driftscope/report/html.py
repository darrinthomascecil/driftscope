"""Self-contained HTML report.

The form is chosen per the data's job: the headline severity counts are *stat
tiles*, not a chart (six numbers do not need axes), plus one composition bar for
the at-a-glance mix. Everything else is a table, because per-finding detail is
lookup work.

Severity uses the reserved status palette (good/warning/serious/critical) and
never carries meaning by colour alone -- every badge ships its label, which is
also what makes the report readable in print, forced-colors and greyscale.

No external requests: one file, inline CSS and JS, safe to email or attach.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from urllib.parse import urlencode

from ..models import (
    Report,
    SEV_CRITICAL,
    SEV_CURRENT,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    SEV_UNKNOWN,
    VERDICT_HOLD,
    VERDICT_LOW,
    VERDICT_PLAN,
    VERDICT_UPGRADE,
    severity_rank,
)
from ..source import parse_ado_url
from .render import ECO_LABEL, _lag_phrase, _plural
from .summarize import (
    group_findings,
    spark_ratios,
    summarize_trend,
    summarize_velocity,
    summarize_vulns,
)

# Severity -> status role. Six levels map onto four reserved status colours;
# LOW and UNKNOWN stay in ink because they are informational, not stateful.
SEV_STYLE = {
    SEV_CRITICAL: ("critical", "●", "Deprecated/removed upstream, or 2+ majors behind"),
    SEV_HIGH: ("serious", "●", "One major behind, or 2+ years stale"),
    SEV_MEDIUM: ("warning", "●", "Several minors behind, over a year old, or unpinned"),
    SEV_LOW: ("low", "○", "Minor or patch behind"),
    SEV_CURRENT: ("good", "✓", "At the latest stable version"),
    SEV_UNKNOWN: ("unknown", "?", "Could not be assessed"),
}
SEV_ORDER = (SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW, SEV_CURRENT, SEV_UNKNOWN)


def e(s: object) -> str:
    return html.escape(str(s), quote=True) if s is not None else ""


_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  background:var(--surface-0);color:var(--text-primary);
  -webkit-font-smoothing:antialiased}
.ds{--surface-0:#f4f4f2;--surface-1:#fcfcfb;--surface-2:#ececea;
  --text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#7a7873;
  --rule:#dedcd7;
  --st-good:#0ca30c;--st-warning:#fab219;--st-serious:#ec835a;--st-critical:#d03b3b;
  --st-low:#7a7873;--st-unknown:#a8a6a0;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .ds{
  --surface-0:#111110;--surface-1:#1a1a19;--surface-2:#242422;
  --text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--rule:#33332f;
  --st-low:#8f8e86;--st-unknown:#6d6c66}}
:root[data-theme=dark] .ds{--surface-0:#111110;--surface-1:#1a1a19;--surface-2:#242422;
  --text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--rule:#33332f;
  --st-low:#8f8e86;--st-unknown:#6d6c66}
.wrap{max-width:1180px;margin:0 auto;padding:40px 24px 72px}
header h1{margin:0 0 6px;font-size:26px;letter-spacing:-.02em}
.sub{color:var(--text-secondary);font-size:13.5px}
.sub code{font-family:var(--mono);font-size:12.5px}
.card{background:var(--surface-1);border:1px solid var(--rule);border-radius:12px;
  padding:18px 20px;margin:22px 0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--text-muted);margin:0 0 14px;font-weight:600}

/* stat tiles: the headline numbers are not a chart */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px}
.tile{background:var(--surface-2);border:1px solid var(--rule);border-radius:10px;
  padding:13px 14px}
.tile .n{font-size:27px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.tile .k{display:flex;align-items:center;gap:6px;font-size:11.5px;font-weight:600;
  text-transform:uppercase;letter-spacing:.06em;color:var(--text-secondary);margin-top:3px}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.dot.critical{background:var(--st-critical)}.dot.serious{background:var(--st-serious)}
.dot.warning{background:var(--st-warning)}.dot.low{background:var(--st-low)}
.dot.good{background:var(--st-good)}.dot.unknown{background:var(--st-unknown)}

/* composition: one stacked bar, 2px surface gaps, 4px rounded ends */
.mix{display:flex;gap:2px;height:14px;margin:2px 0 12px}
.mix span{height:100%;position:relative}
.mix span:first-child{border-radius:4px 0 0 4px}
.mix span:last-child{border-radius:0 4px 4px 0}
.mix span:only-child{border-radius:4px}
.mix .critical{background:var(--st-critical)}.mix .serious{background:var(--st-serious)}
.mix .warning{background:var(--st-warning)}.mix .low{background:var(--st-low)}
.mix .good{background:var(--st-good)}.mix .unknown{background:var(--st-unknown)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12.5px;color:var(--text-secondary)}
.legend i{display:inline-flex;align-items:center;gap:6px;font-style:normal}

.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px}
.controls label{font-size:12px;color:var(--text-muted);margin-right:2px}
button.chip,select{font:inherit;font-size:12.5px;padding:5px 11px;border-radius:999px;
  border:1px solid var(--rule);background:var(--surface-2);color:var(--text-primary);
  cursor:pointer}
select{border-radius:8px}
button.chip[aria-pressed=true]{background:var(--text-primary);color:var(--surface-1);
  border-color:var(--text-primary)}

.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--text-muted);font-weight:600;padding:0 10px 8px;white-space:nowrap}
td{padding:9px 10px;border-top:1px solid var(--rule);vertical-align:top}
tbody tr:hover{background:var(--surface-2)}
code,.v{font-family:var(--mono);font-size:12.5px}
.name{font-weight:600}
.eco{color:var(--text-muted);font-size:11.5px}
.where{color:var(--text-secondary);font-size:12px;font-family:var(--mono)}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:700;
  letter-spacing:.04em;padding:2px 8px;border-radius:999px;white-space:nowrap;
  border:1px solid var(--rule);background:var(--surface-2)}
.badge .g{font-size:9px;line-height:1}
.badge.critical{color:var(--st-critical)}.badge.serious{color:var(--st-serious)}
.badge.warning{color:#8a5c00}.badge.low{color:var(--st-low)}
.badge.good{color:var(--st-good)}.badge.unknown{color:var(--st-unknown)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .badge.warning{color:var(--st-warning)}}
:root[data-theme=dark] .badge.warning{color:var(--st-warning)}

details{border-top:1px solid var(--rule);padding:10px 0}
details summary{cursor:pointer;font-size:13.5px;display:flex;gap:10px;align-items:center;
  flex-wrap:wrap}
details summary::-webkit-details-marker{display:none}
details summary:before{content:"\\25b8";color:var(--text-muted);font-size:11px}
details[open] summary:before{content:"\\25be"}
.detail{padding:12px 0 6px 18px;font-size:13.5px;color:var(--text-secondary)}
.detail b{color:var(--text-primary);font-weight:600}
.detail ol,.detail ul{margin:6px 0;padding-left:20px}
.detail li{margin:3px 0}
.note{color:var(--text-muted);font-size:12.5px;font-style:italic}
a{color:inherit;text-decoration-color:var(--text-muted);text-underline-offset:2px}
.foot{color:var(--text-muted);font-size:12.5px;margin-top:26px;border-top:1px solid var(--rule);
  padding-top:16px}
.empty{color:var(--text-muted);padding:14px 0;font-size:13.5px}

/* sections: one table per domain, so a reader can stop at the part that is theirs */
.sect{margin:0 0 26px}
.sect:last-child{margin-bottom:0}
.secth{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:0 0 4px;
  font-size:15px;font-weight:650;letter-spacing:-.01em}
.secmeta{font-weight:400;font-size:12.5px;color:var(--text-muted)}
.secchips{display:flex;flex-wrap:wrap;gap:14px;margin:0 0 8px;font-size:12px;
  color:var(--text-secondary)}
.seccount{display:inline-flex;align-items:center;gap:5px}

/* findings rows expand in place: one location per component */
tr.frow{cursor:pointer}
th.tog,td.tog{width:26px;padding-left:4px;padding-right:0}
button.caret{background:none;border:0;padding:2px 4px;cursor:pointer;
  color:var(--text-muted);font-size:11px;line-height:1;border-radius:4px}
button.caret:hover{color:var(--text-primary);background:var(--surface-2)}
tr.fdetail>td{border-top:0;padding-top:0;background:var(--surface-1)}
.cvss{display:inline-block;min-width:30px;padding:1px 6px;margin-right:7px;
  border-radius:5px;font-size:11.5px;font-weight:600;text-align:center;
  font-variant-numeric:tabular-nums;border:1px solid var(--rule);
  color:var(--text-primary);background:var(--surface-2)}
.cvss.critical{border-color:var(--critical);color:var(--critical)}
.cvss.warning{border-color:var(--warning)}
.cvss.low{border-color:var(--good)}
.cvss.none{color:var(--text-muted);font-weight:400}
tr.fdetail .detail{padding:2px 0 14px}

/* upstream velocity: sits beneath the finding it qualifies */
.vel{margin:12px 0 4px;padding:12px 14px;background:var(--surface-2);
  border:1px solid var(--rule);border-radius:10px}
.velhead{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:9px;
  font-size:13px;color:var(--text-primary)}
.veltable{width:auto;font-size:12.5px}
.vuln{border-left:3px solid var(--st-critical)}
.veltable th.vh{white-space:normal;max-width:230px}
.veltable th{text-align:left;padding:4px 14px 4px 0;text-transform:none;
  letter-spacing:0;font-size:12.5px;color:var(--text-secondary);font-weight:500;
  white-space:nowrap;border:0;cursor:help}
.veltable td{border:0;padding:4px 12px 4px 0;vertical-align:middle}
/* one series per row: 2px surface gaps, 4px rounded data-ends on the baseline */
.spark{display:inline-flex;align-items:flex-end;gap:2px;height:20px}
.spark i{display:block;width:9px;border-radius:2px 2px 0 0;opacity:.45;
  background:currentColor}
.spark i.last{opacity:1}
.spark i.neg{border-radius:0 0 2px 2px;align-self:flex-start}
.spark.good{color:var(--st-good)}.spark.critical{color:var(--st-critical)}
.spark.low{color:var(--st-low)}.spark.unknown{color:var(--st-unknown)}
.good-ink{color:var(--st-good)}.critical-ink{color:var(--st-critical)}
.low-ink{color:var(--text-secondary)}.unknown-ink{color:var(--text-muted)}
.verdict{display:inline-block;padding:3px 10px;border-radius:6px;font-size:12px;margin:2px 0 8px;border:1px solid currentColor}
.verdict b{font-weight:700}
.v-up{color:var(--st-good)}.v-plan{color:var(--st-warning)}.v-hold{color:var(--st-critical)}.v-low{color:var(--text-secondary)}
.weigh{display:flex;flex-wrap:wrap;gap:22px;margin:0 0 10px}
.weigh .wl{font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:var(--text-muted)}
.weigh ul{margin:3px 0 0;padding-left:16px}
.weigh li{font-size:12.5px;color:var(--text-secondary)}
"""

_JS = """
const rows=[...document.querySelectorAll('table.findings tbody tr.frow')];
const sections=[...document.querySelectorAll('section.sect')];
let sev='actionable', eco='all';
function detailOf(r){ return r.dataset.for ? document.getElementById(r.dataset.for) : null; }
function want(el){
  const s=el.dataset.sev, k=el.dataset.eco;
  const sevOk = sev==='all' || (sev==='actionable' ? +el.dataset.rank<=2 : s===sev);
  return sevOk && (eco==='all' || k===eco);
}
function apply(){
  let n=0;
  for(const r of rows){
    const ok=want(r); r.hidden=!ok; if(ok)n++;
    const d=detailOf(r);
    // A detail row is only visible when its own row passes the filter AND the
    // user expanded it; filtering must not silently reveal collapsed guidance.
    if(d) d.hidden = !ok || r.dataset.open!=='1';
  }
  // A section whose every row was filtered out becomes a heading over an empty
  // table, which reads as "nothing wrong here" rather than "nothing matched".
  for(const sec of sections){
    const visible=[...sec.querySelectorAll('tbody tr.frow')].some(r=>!r.hidden);
    sec.hidden=!visible;
  }
  document.getElementById('count').textContent=n+' shown';
  document.getElementById('none').hidden=n>0;
}
function toggle(r){
  const d=detailOf(r); if(!d) return;
  const open = r.dataset.open==='1';
  r.dataset.open = open ? '0' : '1';
  d.hidden = open;
  const b=r.querySelector('button.caret');
  if(b){ b.setAttribute('aria-expanded', String(!open)); b.innerHTML = open ? '\u25b8' : '\u25be'; }
}
for(const r of rows){
  r.addEventListener('click', ev=>{
    // Let links and the button itself behave normally.
    if(ev.target.closest('a')) return;
    toggle(r);
  });
  const b=r.querySelector('button.caret');
  if(b) b.addEventListener('click', ev=>{ ev.stopPropagation(); toggle(r); });
}
document.querySelectorAll('button.chip').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('button.chip').forEach(x=>x.setAttribute('aria-pressed',x===b));
  sev=b.dataset.v; apply();
});
document.getElementById('eco').onchange=ev=>{eco=ev.target.value; apply();};
const expandBtn=document.getElementById('expandall');
function syncExpandLabel(){
  // The button flips both ways, so its label has to follow the rows rather than
  // stay fixed -- it read "expand all" while everything was already expanded.
  const anyClosed = rows.some(r=>!r.hidden && detailOf(r) && r.dataset.open!=='1');
  expandBtn.textContent = anyClosed ? 'expand all' : 'collapse all';
}
expandBtn.onclick=()=>{
  const anyClosed = rows.some(r=>!r.hidden && detailOf(r) && r.dataset.open!=='1');
  for(const r of rows){ if(r.hidden||!detailOf(r)) continue;
    if((r.dataset.open==='1') === anyClosed) toggle(r); }
  syncExpandLabel();
};
apply();
syncExpandLabel();
"""


TREND_STYLE = {
    "improving": ("good", "▲", "getting better"),
    "steady": ("low", "▬", "roughly unchanged"),
    "deteriorating": ("critical", "▼", "getting worse"),
    "unknown": ("unknown", "?", "not measurable"),
}


def _spark(values, *, role: str, labels) -> str:
    """A tiny per-window bar strip. One series, so no legend -- the row label names
    it; the last bar is emphasised because it is the window under judgement.

    The 2px floor keeps a near-zero window visible as a bar rather than as nothing,
    which is why this maps ratios to pixels itself instead of reusing the
    eight-glyph bucketing the Markdown sparkline uses.
    """
    ratios = spark_ratios(values)
    if not ratios:
        return ""
    bars = []
    last_i = len(ratios) - 1
    for i, (r, v, lab) in enumerate(zip(ratios, values, labels)):
        h = max(2, round(r * 18))
        last = " last" if i == last_i else ""
        sign = " neg" if v < 0 else ""
        bars.append(
            f"<i class='b{last}{sign}' style='height:{h}px' title='{e(lab)}: {v:g}'></i>"
        )
    return f"<span class='spark {role}'>{''.join(bars)}</span>"


def _pct(x: float | None, *, unit: str = "%") -> str:
    if x is None:
        return "—"
    # Avoid "-0": a rounded-to-zero change is no change.
    if abs(x) < 0.5:
        return f"0{unit}"
    return f"{x:+.0f}{unit}"


VSEV_STYLE = {
    "CRITICAL": "critical", "HIGH": "critical", "MODERATE": "warning",
    "MEDIUM": "warning", "LOW": "low", "": "unknown",
}


def _vuln_rows(sec) -> str:
    out = [f"<tr><th class='vh'>{e(sec.title)}</th><td class='v'>"
           f"<b class='{sec.role}-ink'>{sec.count}</b></td><td>"]
    if sec.shown:
        items = []
        for v in sec.shown:
            sev = VSEV_STYLE.get(v.severity, "unknown")
            # The 0-100 chip is the CVSS v3 base score x10. Advisories with no
            # usable v3 vector render no chip at all rather than a placeholder --
            # a number beside a CVE that no source stands behind is worse than a
            # blank, and "unscored" is a different statement from "harmless".
            chip = (f"<span class='cvss {sev}' title='CVSS v3 base score "
                    f"{v.score / 10:.1f} of 10'>{v.score}</span>"
                    if v.score is not None else
                    "<span class='cvss none' title='advisory carries no CVSS v3 "
                    "vector'>&mdash;</span>")
            items.append(
                f"<div>{chip}<a href='{e(v.url)}' target='_blank' rel='noopener'>"
                f"{e(v.cve)}</a> <span class='badge {sev}'>{e(v.severity or 'unrated')}"
                f"</span> {e(v.summary[:110])}</div>"
            )
        out.append("".join(items))
        if sec.hidden:
            out.append(f"<div class='note'>+{sec.hidden} more not described</div>")
    out.append("</td></tr>")
    return "".join(out)


def _registry_notes_block(f) -> str:
    """What changed between the image running and the one available.

    A private registry has no release notes endpoint, so this is assembled from
    OCI labels on each image in the gap. Three claims are kept apart because they
    are answers to different questions: what changed, what breaks, and what is
    already wrong with the version in production right now.
    """
    ex = (f.resolution.extra or {}) if f.resolution else {}
    if not ex.get("registry"):
        return ""
    changelog = ex.get("changelog") or []
    breaking = ex.get("breaking") or []
    ahead = ex.get("known_issues") or []
    current_issue = ex.get("current_known_issues")
    span = ex.get("span") or []
    if not (changelog or breaking or ahead or current_issue or span):
        return ""

    out = []
    a = out.append
    a(f"<p><b>Registry notes</b> {e(ex['registry'])}"
      f"{f' &middot; {len(span)} version(s) ahead' if span else ''}</p>")

    # Deployed-version issues first: this is live in production, not prospective.
    if current_issue:
        a(f"<div class='note'><b>Known issue in the deployed version "
          f"{e(str(f.component.current))}:</b> {e(current_issue)}</div>")

    if breaking:
        a("<div class='note'><b>Breaking changes in this upgrade</b></div><ul>")
        for b in breaking[:8]:
            a(f"<li><code>{e(b['version'])}</code> {e(b['text'])}</li>")
        a("</ul>")

    if changelog:
        a("<div class='note'><b>Changelog</b></div><ul>")
        for entry in changelog[:12]:
            a(f"<li><code>{e(entry['version'])}</code> {e(entry['text'])}</li>")
        a("</ul>")

    if ahead:
        a("<div class='note'><b>Known issues introduced along the way</b> "
          "&mdash; present in intermediate versions, check whether the target "
          "still carries them</div><ul>")
        for k in ahead[:8]:
            a(f"<li><code>{e(k['version'])}</code> {e(k['text'])}</li>")
        a("</ul>")

    # Absence of notes is not absence of change, and must not read as one.
    documented = ex.get("documented_versions", 0)
    if span and documented < len(span):
        a(f"<div class='note'>{len(span) - documented} of {len(span)} version(s) "
          "in this gap carry no changelog label &mdash; what changed in them is "
          "unrecorded, not empty.</div>")
    if ex.get("span_truncated"):
        a("<div class='note'>Older versions in the gap were not opened; the "
          "changelog above is the most recent portion only.</div>")
    return "".join(out)


def _vuln_block(r) -> str:
    """Known advisories, and specifically which ones the upgrade would clear."""
    s = summarize_vulns(r)
    if s.unmatched:
        return f"<div class='note'>Vulnerability lookup: {e(s.note)}</div>"
    worst = VSEV_STYLE.get(s.worst_severity, "unknown")
    out = [
        "<div class='vel vuln'><div class='velhead'><b>Known vulnerabilities</b> ",
        (f"<span class='badge unknown'>{s.headline_count} on record</span> "
         if s.headline_version is None else
         f"<span class='badge {worst}'>{s.headline_count} affecting "
         f"{e(s.headline_version)}</span> "),
        f"<span class='note'>{e(s.package)} &middot; via OSV"
        f"{' &middot; approximate' if s.approximate else ''}</span></div>",
        "<table class='veltable'><tbody>",
    ]
    out += [_vuln_rows(sec) for sec in s.sections]
    out.append("</tbody></table>")
    if s.upgrade_clears_nothing:
        out.append(
            "<div class='note'>The upgrade clears none of these: they affect the "
            "target version too, so this needs a different remedy.</div>"
        )
    if s.note:
        out.append(f"<div class='note'>{e(s.note)}</div>")
    out.append("</div>")
    return "".join(out)


DIR_STYLE = {
    "improving": ("good", "▲"),
    "falling behind": ("critical", "▼"),
    "flat": ("low", "▬"),
}


def _trend_block(d) -> str:
    """Movement since the previous stored run: are we catching up?"""
    s = summarize_trend(d)
    role, glyph = DIR_STYLE.get(s.direction, DIR_STYLE["flat"])
    out = ["<div class='card'><h2>Since the last run</h2>"]
    out.append(
        f"<div class='velhead'><span class='badge {role}'><span class='g'>{glyph}</span>"
        f"{e(s.direction)}</span> <b>{s.net:+d}</b> actionable findings "
        f"<span class='note'>{e(s.from_run)} &rarr; "
        f"{e(s.to_run)}</span></div>"
    )
    out.append("<div class='tiles'>")
    for tile in s.tiles:
        out.append(
            f"<div class='tile'><div class='n'>{tile.count}</div>"
            f"<div class='k'><span class='dot {tile.role}'></span>"
            f"{e(tile.label)}</div></div>"
        )
    out.append("</div>")

    if s.has_rows:
        out.append("<div class='scroll'><table><thead><tr><th>Change</th>"
                   "<th>Component</th><th>Severity</th><th>Version</th>"
                   "</tr></thead><tbody>")
        for g in s.groups:
            for c in g.shown:
                sev = (
                    f"{e(c.from_severity or '—')} &rarr; {e(c.to_severity or 'gone')}"
                )
                ver = (
                    f"<span class='v'>{e(c.from_version or '—')} &rarr; "
                    f"{e(c.to_version or '—')}</span>"
                    if c.moved_version
                    else f"<span class='v'>{e(c.to_version or c.from_version or '')}</span>"
                )
                out.append(
                    f"<tr><td><span class='badge {g.role}'>{e(g.label)}</span></td>"
                    f"<td><span class='name'>{e(c.name)}</span><br>"
                    f"<span class='eco'>{e(ECO_LABEL.get(c.ecosystem, c.ecosystem))}</span></td>"
                    f"<td>{sev}</td><td>{ver}</td></tr>"
                )
            if g.overflow:
                out.append(
                    f"<tr><td></td><td class='note'>+{g.overflow} more "
                    f"{e(g.label)}</td><td></td><td></td></tr>"
                )
        out.append("</tbody></table></div>")
    elif s.is_empty:
        out.append("<div class='empty'>No component changed severity or version.</div>")

    if s.note:
        out.append(f"<div class='note'>{e(s.note)}</div>")
    out.append(
        "<div class='note'>Findings are matched by ecosystem and name, so a bumped "
        "version reads as the same component moving rather than one disappearing "
        "and another appearing. \"Actionable\" counts MEDIUM and worse.</div></div>"
    )
    return "".join(out)


_REMOTE_RE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def _blob_url(report, source: str | None, line: int | None) -> str | None:
    """Permalink a declaration site to whichever forge the scan came from.

    Anchored to the scanned commit rather than a branch, so the link keeps
    pointing at the line that was actually scanned even after the branch moves.
    Returns None for local scans and for live Azure findings, which have no file.
    """
    src = getattr(report, "source", None)
    if not src or not source or source.startswith("azure://"):
        return None
    remote = src.get("remote") or ""
    ref = src.get("commit") or src.get("ref") or "HEAD"

    ado = parse_ado_url(remote)
    if ado:
        # ADO addresses a file by query string, not path segments: `GC` marks the
        # version as a Git Commit, and `path` must carry a leading slash. The
        # line range is half-open, hence lineEnd = line + 1.
        q = {"path": f"/{source}", "version": f"GC{ref}"}
        if line:
            q |= {"line": str(line), "lineEnd": str(line + 1),
                  "lineStartColumn": "1", "lineEndColumn": "1"}
        return f"{ado.web_base}?{urlencode(q)}"

    m = _REMOTE_RE.search(remote)
    if not m:
        return None
    url = f"https://github.com/{m.group(1)}/{m.group(2)}/blob/{ref}/{source}"
    return f"{url}#L{line}" if line else url


def _where(report, source: str | None, line: int | None) -> str:
    """The declaration site, hyperlinked when the target was a GitHub remote."""
    label = e(source) + (f":{line}" if line else "")
    url = _blob_url(report, source, line)
    return f"<a href='{e(url)}' target='_blank' rel='noopener'>{label}</a>" if url else label


def _velocity_block(v) -> str:
    """Upstream health, shown beneath the finding it qualifies."""
    s = summarize_velocity(v)
    role, glyph, meaning = TREND_STYLE.get(s.verdict, TREND_STYLE["unknown"])
    out = [f"<div class='vel'><div class='velhead'>"
           f"<b>Upstream velocity</b> "
           f"<span class='badge {role}'><span class='g'>{glyph}</span>"
           f"{e(s.verdict)}</span> "
           f"<span class='note'>{e(s.repo)} &middot; {s.window_count}"
           f" &times; {s.window_days}-day windows</span></div>"]

    if s.error:
        out.append(f"<div class='note'>{e(s.error)}</div></div>")
        return "".join(out)

    out.append("<table class='veltable'><tbody>")
    for m in s.metrics:
        trole, tglyph, _ = TREND_STYLE.get(m.trend, TREND_STYLE["unknown"])
        # Deliberately the series value, not m.latest: the bar strip and the number
        # beside it must agree, and an unmeasurable window plots as a zero bar.
        latest = m.series[-1] if m.series else 0
        shown = f"{latest:.0f}%" if m.kind == "share" else f"{latest:.0f}"
        out.append(
            f"<tr><th title='{e(m.why)}'>{e(m.label)}</th>"
            f"<td>{_spark(m.series, role=trole, labels=m.series_labels)}</td>"
            f"<td class='v'>{shown}</td>"
            f"<td class='v {trole}-ink'>"
            f"{_pct(m.change, unit=m.unit)}</td>"
            f"<td><span class='badge {trole}'><span class='g'>{tglyph}</span>"
            f"{e(m.trend)}</span></td></tr>"
        )
    out.append("</tbody></table>")
    latest = s.latest_window
    if latest:
        out.append(
            f"<div class='note'>Latest window {e(latest.label)}: "
            f"{latest.opened} opened, {latest.closed} closed, "
            f"{latest.total_reactions} reactions across {latest.sampled_issues} "
            f"sampled issues, {latest.releases} release(s).</div>"
        )
    for n in s.notes:
        out.append(f"<div class='note'>{e(n)}</div>")
    out.append(
        "<div class='note'>Percentages compare the latest window with the mean of "
        "the preceding ones, so they read as acceleration rather than level. "
        "Reaction mix is a proxy for sentiment, not a measurement of it.</div></div>"
    )
    return "".join(out)


_VERDICT_LABEL = {
    VERDICT_UPGRADE: ("Upgrade now", "v-up"),
    VERDICT_PLAN: ("Plan a staged upgrade", "v-plan"),
    VERDICT_HOLD: ("Hold", "v-hold"),
    VERDICT_LOW: ("Low priority", "v-low"),
}


def _verdict_block(v) -> str:
    """The 'should you upgrade?' badge plus the two-sided why-move / why-wait lists."""
    if v is None:
        return ""
    label, cls = _VERDICT_LABEL.get(v.verdict, (v.verdict, "v-low"))
    out = [f"<div class='verdict {cls}'>Should you upgrade? &nbsp;<b>{e(label)}</b></div>"]
    cols = []
    if v.move_reasons:
        cols.append("<div><div class='wl'>Why upgrade</div><ul>"
                    + "".join(f"<li>{e(r)}</li>" for r in v.move_reasons) + "</ul></div>")
    if v.wait_reasons:
        cols.append("<div><div class='wl'>Why wait</div><ul>"
                    + "".join(f"<li>{e(r)}</li>" for r in v.wait_reasons) + "</ul></div>")
    if cols:
        out.append("<div class='weigh'>" + "".join(cols) + "</div>")
    return "".join(out)


def _finding_detail(report: Report, f) -> str:
    """The guidance body for one finding, rendered for its expandable row."""
    c, res, adv = f.component, f.resolution, f.advice
    out: list[str] = []
    a = out.append

    a(_verdict_block(f.verdict))

    if f.lag.current_age_days:
        yrs = f.lag.current_age_days / 365.25
        age = f"{yrs:.1f} years" if yrs >= 1 else _plural(f.lag.current_age_days, "day")
        bound = "at least " if f.lag.age_lower_bound else ""
        a(f"<div><b>Age of your version:</b> {bound}{e(age)}</div>")
    also = c.meta.get("also_at") or []
    if also:
        links = []
        for loc in also[:8]:
            path, _, ln = loc.rpartition(":")
            if path and ln.isdigit():
                links.append(_where(report, path, int(ln)))
            else:
                links.append(_where(report, loc, None))
        more = f" (+{len(also) - 8} more)" if len(also) > 8 else ""
        a(f"<div><b>Also at:</b> <span class='where'>{', '.join(links)}</span>{more}</div>")
    if res.repo:
        from ..forge import forge_web_url

        a(f"<div><b>Source:</b> <a href='{e(forge_web_url(res.forge, res.repo))}' "
          f"target='_blank' rel='noopener'>{e(res.repo)}</a></div>")
    if res.homepage:
        a(f"<div><b>Docs:</b> <a href='{e(res.homepage)}' target='_blank' "
          f"rel='noopener'>{e(res.homepage)}</a></div>")
    if adv:
        if adv.strategy:
            a(f"<p><b>Strategy.</b> {e(adv.strategy)}</p>")
        if adv.intermediate_versions:
            path = " &rarr; ".join(
                f"<code>{e(v)}</code>"
                for v in [c.current, *adv.intermediate_versions, res.latest_stable]
            )
            a(f"<div><b>Staged path:</b> {path}</div>")
        if adv.steps:
            a("<b>Steps</b><ol>")
            for st in adv.steps:
                a(f"<li>{e(st)}</li>")
            a("</ol>")
        if adv.cautions:
            a("<b>Cautions</b><ul>")
            for ct in adv.cautions:
                a(f"<li>{e(ct)}</li>")
            a("</ul>")
    a(_registry_notes_block(f))
    d = f.delta
    if d and (d.breaking_notes or d.bug_issues or d.total_bug_count):
        a(f"<p><b>Upstream delta</b> {e(d.from_ref)} &rarr; {e(d.to_ref)}"
          f"{f' &middot; {len(d.releases_in_window)} releases' if d.releases_in_window else ''}</p>")
        if d.breaking_notes:
            a("<ul>")
            for n in d.breaking_notes[:8]:
                a(f"<li>{e(n)}</li>")
            a("</ul>")
        # Show the count whenever the window had any bug-labelled issues, not only
        # when there are still-open ones to list -- a window with 9 filed and 0 open
        # is a real signal that used to render as nothing.
        if d.total_bug_count or d.bug_issues:
            a(f"<div class='note'>Bug activity in the version window "
              f"(a rough signal &mdash; not scoped to a specific version): "
              f"{d.open_bug_count} open of {d.total_bug_count} bug-labelled issue(s) filed</div>")
            if d.bug_issues:
                a("<ul>")
                for i in d.bug_issues[:6]:
                    a(f"<li><a href='{e(i.url)}' target='_blank' rel='noopener'>"
                      f"#{i.number}</a> {e(i.title)}</li>")
                a("</ul>")
        if d.note:
            a(f"<div class='note'>{e(d.note)}</div>")
    if f.vulns is not None and f.vulns.has_data:
        a(_vuln_block(f.vulns))
    if f.velocity:
        a(_velocity_block(f.velocity))
    if res.note:
        a(f"<div class='note'>{e(res.note)}</div>")
    body = "".join(out)
    return f"<div class='detail'>{body}</div>" if body else ""


def to_html(report: Report, *, title: str = "Version drift report") -> str:
    counts = report.counts()
    findings = report.sorted_findings()
    total = len(findings) or 1
    s = report.stats

    parts: list[str] = []
    a = parts.append

    a("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    a("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    a(f"<title>{e(title)}</title><style>{_CSS}</style></head>")
    a("<body class='ds'><div class='wrap'>")

    # ---- header
    a("<header>")
    a(f"<h1>{e(title)}</h1>")
    a(f"<div class='sub'>Scanned <code>{e(report.root)}</code>")
    if report.source:
        src = report.source
        a(f" &middot; commit <code>{e((src.get('commit') or '')[:12])}</code>"
          f" on <code>{e(src.get('ref'))}</code>")
    a(f"<br>{s.components_found} versioned components across {s.files_scanned} files"
      f" &middot; {s.resolved} resolved, {s.resolve_errors} unresolved"
      f" &middot; {s.duration_s}s, {s.github_queries} GitHub queries"
      f"<br>Generated {e(datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z'))}</div>")
    a("</header>")

    # ---- stat tiles + composition bar
    a("<div class='card'><h2>Summary</h2><div class='tiles'>")
    for sev in SEV_ORDER:
        n = counts.get(sev, 0)
        if not n:
            continue
        role, glyph, _ = SEV_STYLE[sev]
        a(f"<div class='tile'><div class='n'>{n}</div>"
          f"<div class='k'><span class='dot {role}'></span>{e(sev)}</div></div>")
    a("</div>")

    a("<div class='mix' role='img' aria-label='severity composition'>")
    for sev in SEV_ORDER:
        n = counts.get(sev, 0)
        if not n:
            continue
        role, _, _ = SEV_STYLE[sev]
        pct = n / total * 100
        a(f"<span class='{role}' style='width:{pct:.2f}%' "
          f"title='{e(sev)}: {n} ({pct:.0f}%)'></span>")
    a("</div><div class='legend'>")
    for sev in SEV_ORDER:
        n = counts.get(sev, 0)
        if not n:
            continue
        role, glyph, meaning = SEV_STYLE[sev]
        a(f"<i title='{e(meaning)}'><span class='dot {role}'></span>{e(sev)} {n}</i>")
    a("</div></div>")

    # ---- trend since the previous run
    if report.trend is not None:
        a(_trend_block(report.trend))

    # ---- controls + table
    ecos = sorted({f.component.ecosystem for f in findings})
    a("<div class='card'><h2>Findings</h2><div class='controls'>")
    a("<label>severity</label>")
    for label, val in (("actionable", "actionable"), ("all", "all"),
                       ("critical", SEV_CRITICAL), ("high", SEV_HIGH),
                       ("medium", SEV_MEDIUM)):
        pressed = "true" if val == "actionable" else "false"
        a(f"<button class='chip' data-v='{e(val)}' aria-pressed='{pressed}'>{e(label)}</button>")
    a("<label style='margin-left:8px'>ecosystem</label><select id='eco'>")
    a("<option value='all'>all</option>")
    for k in ecos:
        a(f"<option value='{e(k)}'>{e(ECO_LABEL.get(k, k))}</option>")
    a("</select>")
    a("<button class='chip' id='expandall' style='margin-left:8px'>collapse all</button>")
    a("<span style='margin-left:auto;color:var(--text-muted);font-size:12px' id='count'></span>")
    a("</div>")

    # One table per section rather than a single long list. The sections map onto
    # who fixes them -- a platform team owns the IaC and the images, an
    # application team owns its dependencies -- so a reader can stop at the part
    # that is theirs. Worst section first.
    sections = group_findings(findings, severity_rank=severity_rank)
    i = 0
    for sec in sections:
        chips = " ".join(
            f"<span class='seccount'><span class='dot {SEV_STYLE.get(sv, SEV_STYLE[SEV_UNKNOWN])[0]}'></span>"
            f"{n} {e(sv)}</span>"
            for sv, n in sorted(sec.counts.items(), key=lambda kv: severity_rank(kv[0]))
        )
        a(f"<section class='sect' data-section='{e(sec.key)}'>")
        a(f"<h3 class='secth'>{e(sec.label)}"
          f"<span class='secmeta'>{sec.actionable} of {len(sec.findings)} behind</span></h3>")
        a(f"<div class='secchips'>{chips}</div>")
        a("<div class='scroll'><table class='findings'><thead><tr>"
          "<th class='tog'></th>"
          "<th>Severity</th><th>Component</th><th>Current</th><th>Latest stable</th>"
          "<th>Behind by</th><th>Declared at</th></tr></thead><tbody>")
        for f in sec.findings:
            i += 1
            c, res = f.component, f.resolution
            role, glyph, _ = SEV_STYLE.get(f.severity, SEV_STYLE[SEV_UNKNOWN])
            where = _where(report, c.source, c.line)
            rid = f"d{i}"
            attrs = (f"data-sev='{e(f.severity)}' data-rank='{severity_rank(f.severity)}' "
                     f"data-eco='{e(c.ecosystem)}'")
            detail = _finding_detail(report, f)

            # Guidance is expanded on open. The report is read once, usually by
            # someone who did not run the scan, so the upgrade advice is the point --
            # making them click 20 carets to reach it buries the answer behind the
            # question. The caret still collapses a row that is not relevant.
            a(f"<tr class='frow' {attrs} data-for='{rid}' data-open='1'>")
            if detail:
                a(f"<td class='tog'><button class='caret' aria-expanded='true' "
                  f"aria-controls='{rid}' title='hide guidance'>&#9662;</button></td>")
            else:
                a("<td class='tog'></td>")
            a(f"<td><span class='badge {role}'><span class='g'>{glyph}</span>{e(f.severity)}</span></td>")
            a(f"<td><span class='name'>{e(c.name)}</span><br>"
              f"<span class='eco'>{e(ECO_LABEL.get(c.ecosystem, c.ecosystem))}</span></td>")
            a(f"<td class='v'>{e(c.current or '—')}</td>")
            a(f"<td class='v'>{e(res.latest_stable or '?')}</td>")
            a(f"<td>{e(_lag_phrase(f))}</td>")
            a(f"<td class='where'>{where}</td></tr>")
            # Guidance for this component, in the row beneath it -- so there is one
            # place per component rather than a table and a separate section to
            # cross-reference.
            if detail:
                a(f"<tr class='fdetail' id='{rid}' {attrs}>"
                  f"<td class='tog'></td><td colspan='6'>{detail}</td></tr>")
        a("</tbody></table></div>")
        a("</section>")
    a("<div class='empty' id='none' hidden>Nothing matches this filter.</div>")
    a("</div>")

    if report.errors:
        a("<div class='card'><h2>Scan errors</h2><ul>")
        for err in report.errors:
            a(f"<li>{e(err)}</li>")
        a("</ul></div>")

    a("<div class='foot'>Severity is rule-based and inspectable: deprecated or 2+ "
      "majors behind is <b>critical</b>; one major behind or 2+ years stale is "
      "<b>high</b>. Runtimes and AKS node images are scored on support expiry and "
      "age instead of version distance. Issue counts in an upstream delta are "
      "<i>issues filed during the version window</i> &mdash; a signal, not proof of "
      "a regression; the breaking-change notes quoted from release bodies are the "
      "higher-confidence input.</div>")
    a(f"<script>{_JS}</script></div></body></html>")
    return "\n".join(parts)
