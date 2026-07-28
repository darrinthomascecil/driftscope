"""Known vulnerabilities, and which ones an upgrade would actually clear.

Version distance says "you are behind". This says "you are exposed", which is the
sentence that gets an upgrade scheduled. Both questions are answered from the same
source of truth: OSV (osv.dev), which aggregates GitHub Advisories, PyPA, RustSec,
and the Alpine/Debian/Ubuntu distro trackers.

The design point that matters: every component is queried at BOTH its current and
its target version, in a single batched call. The difference between those two sets
is the answer to "what would upgrading fix" -- computed from advisory data rather
than inferred from version numbers.

  affects current, not target  -> fixed by the upgrade
  affects both                 -> the upgrade does not help; needs a different fix
  affects target, not current  -> introduced by the upgrade (rare, but real)

Container images are best-effort and labelled as such. Without pulling and
inspecting an image there is no package inventory, so the image's primary software
is queried against the distro ecosystem implied by its tag variant (`-alpine` ->
Alpine, `-slim`/`-bookworm` -> Debian). That finds advisories for the headline
component, not for everything in the image; a real image scan is a different tool.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .http import Client, HttpError
from .models import (
    DOCKER,
    GO,
    MAVEN,
    NPM,
    NUGET,
    PYPI,
    Component,
)

OSV_QUERYBATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"

# Our ecosystem name -> OSV's. Only exact-match ecosystems appear here; images are
# handled separately because they need a distro guess.
OSV_ECOSYSTEM = {
    PYPI: "PyPI",
    NPM: "npm",
    NUGET: "NuGet",
    MAVEN: "Maven",
    GO: "Go",
}

# Official image -> (distro package name template, distro family). The template
# takes the major version, because distros package e.g. postgresql15 separately.
IMAGE_OS_PACKAGE = {
    "node": ("nodejs", None),
    "python": ("python3", None),
    "postgres": ("postgresql{major}", "postgresql"),
    "redis": ("redis", None),
    "nginx": ("nginx", None),
    "mysql": ("mysql", None),
    "mariadb": ("mariadb", None),
    "golang": ("golang", None),
    "httpd": ("apache2", None),
    "rabbitmq": ("rabbitmq-server", None),
}

# Tag variant -> OSV distro ecosystem. Versions are the current stable lines; OSV
# requires a concrete ecosystem string, and advisories for a package rarely differ
# across adjacent releases of the same distro.
VARIANT_DISTRO = {
    "alpine": "Alpine:v3.21",
    "slim": "Debian:12",
    "bookworm": "Debian:12",
    "bookworm-slim": "Debian:12",
    "slim-bookworm": "Debian:12",
    "bullseye": "Debian:11",
    "bullseye-slim": "Debian:11",
    "trixie": "Debian:13",
    "trixie-slim": "Debian:13",
    "": "Debian:12",
}

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "MEDIUM": 2, "LOW": 3, "": 4}


# ---------------------------------------------------------------- CVSS scoring

# CVSS v3.1 base-score metric weights, from the published specification. OSV hands
# back a vector string rather than a number, so the score is computed here -- it is
# arithmetic on data the advisory supplied, not an opinion about severity.
_CVSS_W = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
# Privileges Required is weighted differently when the vulnerability can affect
# resources beyond its own security scope.
_CVSS_PR = {False: {"N": 0.85, "L": 0.62, "H": 0.27},
            True: {"N": 0.85, "L": 0.68, "H": 0.50}}


def _cvss_roundup(x: float) -> float:
    """The spec's own rounding: smallest one-decimal value >= x.

    Ordinary round() disagrees with published scores at the boundaries, which is
    how a computed 9.8 becomes a 9.7 that matches no advisory anyone can look up.
    """
    i = int(round(x * 100000))
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def cvss_base_score(vector: str | None) -> float | None:
    """CVSS v3.x base score from a vector string, or None if it cannot be computed.

    None rather than 0.0 for an absent, malformed or non-v3 vector: 0.0 is a real
    score meaning "no impact", and a missing measurement must not impersonate one.
    v2 and v4 vectors return None -- they use different formulas, and guessing an
    equivalent would invent a number.
    """
    if not isinstance(vector, str) or not vector:
        return None
    try:
        m = dict(p.split(":", 1) for p in vector.split("/") if ":" in p)
        if not str(m.get("CVSS", "")).startswith("3"):
            return None
        changed = m.get("S") == "C"
        iss = 1 - ((1 - _CVSS_W["C"][m["C"]])
                   * (1 - _CVSS_W["I"][m["I"]])
                   * (1 - _CVSS_W["A"][m["A"]]))
        impact = ((7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15) if changed
                  else 6.42 * iss)
        if impact <= 0:
            return 0.0
        expl = (8.22 * _CVSS_W["AV"][m["AV"]] * _CVSS_W["AC"][m["AC"]]
                * _CVSS_PR[changed][m["PR"]] * _CVSS_W["UI"][m["UI"]])
        raw = min(1.08 * (impact + expl), 10.0) if changed else min(impact + expl, 10.0)
        return _cvss_roundup(raw)
    except (KeyError, ValueError, TypeError):
        return None


@dataclass
class Vuln:
    id: str
    aliases: list[str] = field(default_factory=list)
    summary: str = ""
    severity: str = ""
    cvss: str = ""
    fixed_in: list[str] = field(default_factory=list)
    url: str = ""

    @property
    def score(self) -> int | None:
        """Danger on 0-100: the CVSS v3 base score x10, nothing invented.

        None when the advisory carries no usable v3 vector. Rendering a default
        there -- 0, or a midpoint -- would put a number beside a CVE that no
        source stands behind, which is worse than an honest blank.
        """
        base = cvss_base_score(self.cvss)
        return None if base is None else int(round(base * 10))

    @property
    def cve(self) -> str:
        """The CVE id if there is one -- what people actually search for."""
        for a in self.aliases:
            if a.startswith("CVE-"):
                return a
        return self.id


@dataclass
class VulnReport:
    """What is known about one component's exposure."""

    ecosystem: str = ""
    package: str = ""
    current_version: str | None = None
    target_version: str | None = None
    # Counts come from advisory *ids* and are always exact. The Vuln lists are a
    # capped, detailed subset for display -- fetching full details for all of them
    # cost one call each (node:18 alone has 50), which is not worth paying to
    # render a number.
    fixed_count: int = 0
    remaining_count: int = 0
    introduced_count: int = 0
    fixed_by_upgrade: list[Vuln] = field(default_factory=list)
    remaining_after_upgrade: list[Vuln] = field(default_factory=list)
    introduced_by_upgrade: list[Vuln] = field(default_factory=list)
    approximate: bool = False
    # True when the two version queries returned identical advisory sets, meaning
    # the version comparison did not discriminate at all. Distro package versions
    # (`5:7.0.15-1~deb12u1`) do not match image tags (`5`), and OSV then matches
    # every advisory for the package at both versions. Reporting that as "72 still
    # present after upgrade" would be confidently wrong.
    indiscriminate: bool = False
    # True when an approximate (image) query returned nothing. Zero advisories from
    # a guessed distro/package/version triple means "the mapping found nothing",
    # which is not the same as "this image is clean" -- and rendering nothing at
    # all made the two indistinguishable.
    unmatched: bool = False
    note: str | None = None
    error: str | None = None

    @property
    def current_count(self) -> int:
        return self.fixed_count + self.remaining_count

    @property
    def worst_severity(self) -> str:
        all_v = self.fixed_by_upgrade + self.remaining_after_upgrade
        if not all_v:
            return ""
        return min((v.severity for v in all_v), key=lambda s: SEVERITY_ORDER.get(s, 9))

    @property
    def has_data(self) -> bool:
        return self.error is None and bool(
            self.current_count or self.introduced_count or self.unmatched
        )

    @property
    def upgrade_effect_known(self) -> bool:
        """Whether "what would upgrading fix" can be answered at all."""
        return not self.indiscriminate and self.target_version is not None

    @property
    def detail_truncated(self) -> int:
        """How many advisories are counted but not detailed."""
        shown = (len(self.fixed_by_upgrade) + len(self.remaining_after_upgrade)
                 + len(self.introduced_by_upgrade))
        return max(0, self.current_count + self.introduced_count - shown)


# --------------------------------------------------------------- query building


def _image_query(c: Component) -> tuple[str, str, str] | None:
    """(osv_ecosystem, package, version) for a container image, or None."""
    from .version import parse

    short = c.name.split("/")[-1].lower()
    entry = IMAGE_OS_PACKAGE.get(short)
    if entry is None or not c.current:
        return None
    v = parse(c.current)
    if not v.valid or not v.release:
        return None
    distro = VARIANT_DISTRO.get(v.variant)
    if distro is None:
        return None
    template, _family = entry
    pkg = template.format(major=v.major)
    return distro, pkg, ".".join(str(x) for x in v.release)


def osv_target(c: Component, version: str | None) -> tuple[str, str, str] | None:
    """Map a component+version onto an OSV query, or None if unsupported."""
    if not version:
        return None
    eco = OSV_ECOSYSTEM.get(c.ecosystem)
    if eco:
        return eco, c.name, version.lstrip("vV")
    if c.ecosystem == DOCKER:
        base = _image_query(c)
        if base is None:
            return None
        distro, pkg, _ = base
        from .version import parse

        pv = parse(version)
        if not pv.valid or not pv.release:
            return None
        return distro, pkg, ".".join(str(x) for x in pv.release)
    return None


# ------------------------------------------------------------------- transport


def _post_json(url: str, payload: dict, client: Client) -> dict | None:
    """OSV needs POST, which the shared cached client does not do; cache by hash."""
    import hashlib

    key = f"osv:{url}:{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}"
    cached = client.cache.get(key)
    if cached is not None:
        return cached
    if client.offline:
        return client.cache.get(key, ignore_ttl=True)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "driftscope/0.1"},
    )
    try:
        client.requests_made += 1
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            json.JSONDecodeError, OSError):
        return None
    client.cache.put(key, body)
    return body


def _vuln_details(vuln_id: str, client: Client) -> Vuln | None:
    try:
        body = client.get_json(OSV_VULN + vuln_id)
    except HttpError:
        return None
    if not isinstance(body, dict):
        return None
    dbs = body.get("database_specific") or {}
    sev = ""
    if isinstance(dbs.get("severity"), str):
        sev = dbs["severity"].upper()
    cvss = ""
    for s in body.get("severity") or []:
        if isinstance(s, dict) and s.get("score"):
            cvss = s["score"]
            break
    fixed: list[str] = []
    for a in body.get("affected") or []:
        for r in a.get("ranges") or []:
            for ev in r.get("events") or []:
                if ev.get("fixed"):
                    fixed.append(ev["fixed"])
    return Vuln(
        id=body.get("id") or vuln_id,
        aliases=[a for a in (body.get("aliases") or []) if isinstance(a, str)],
        summary=(body.get("summary") or body.get("details") or "")[:220],
        severity=sev,
        cvss=cvss,
        fixed_in=sorted(set(fixed))[:6],
        url=f"https://osv.dev/vulnerability/{body.get('id') or vuln_id}",
    )


def cur_eco_of(rep: "VulnReport") -> str:
    """The OSV ecosystem a report was queried against, for its caveat text."""
    return rep.__dict__.get("_ecosystem_queried") or "the distro"


# --------------------------------------------------------------------- assess


def assess(
    components: list[tuple[Component, str | None]],
    client: Client,
    *,
    detail_per_component: int = 6,
    max_details: int = 40,
) -> dict[str, VulnReport]:
    """Assess many components at once.

    `components` is (component, target_version) pairs. Returns a map keyed by
    `component.key` for the ones OSV could answer.

    Cost: one batched call covering every component at both versions, plus a
    bounded number of detail lookups. Counts are derived from advisory ids and so
    are exact regardless of that bound; only the displayed descriptions are capped.
    """
    queries: list[dict] = []
    slots: list[tuple[str, str, tuple[str, str, str] | None, tuple[str, str, str] | None]] = []

    for c, target in components:
        cur_q = osv_target(c, c.current)
        tgt_q = osv_target(c, target)
        if cur_q is None:
            continue
        slots.append((c.key, c.ecosystem, cur_q, tgt_q))
        queries.append({"version": cur_q[2], "package": {"name": cur_q[1], "ecosystem": cur_q[0]}})
        queries.append(
            {"version": tgt_q[2], "package": {"name": tgt_q[1], "ecosystem": tgt_q[0]}}
            if tgt_q
            else {"version": cur_q[2], "package": {"name": cur_q[1], "ecosystem": cur_q[0]}}
        )

    if not queries:
        return {}

    body = _post_json(OSV_QUERYBATCH, {"queries": queries}, client)
    if not isinstance(body, dict):
        return {
            key: VulnReport(ecosystem=eco, package=cur[1], error="OSV query failed")
            for key, eco, cur, _ in slots
        }
    results = body.get("results") or []

    def ids(idx: int) -> list[str]:
        if idx >= len(results) or not isinstance(results[idx], dict):
            return []
        return [
            v.get("id") for v in (results[idx].get("vulns") or [])
            if isinstance(v, dict) and v.get("id")
        ]

    out: dict[str, VulnReport] = {}
    wanted: set[str] = set()
    plan: dict[str, tuple[list[str], list[str]]] = {}

    for i, (key, eco, cur, tgt) in enumerate(slots):
        cur_ids, tgt_ids = ids(i * 2), ids(i * 2 + 1)
        plan[key] = (cur_ids, tgt_ids)
        wanted |= set(cur_ids) | set(tgt_ids)
        rep = VulnReport(
            ecosystem=eco,
            package=cur[1],
            current_version=cur[2],
            target_version=(tgt[2] if tgt else None),
            approximate=(eco == DOCKER),
        )
        if eco == DOCKER:
            rep.__dict__["_ecosystem_queried"] = cur[0]
            rep.note = (
                f"advisories for the image's primary package ({cur[1]}) on {cur[0]}; "
                "this is not a full image scan and will not list OS packages you "
                "did not ask about"
            )
        if tgt is None:
            rep.note = ((rep.note + "; ") if rep.note else "") + (
                "no upgrade target resolved, so nothing can be said about what an "
                "upgrade would fix"
            )
        out[key] = rep

    # Exact counts first, from ids alone -- no network.
    buckets: dict[str, tuple[list[str], list[str], list[str]]] = {}
    for key, (cur_ids, tgt_ids) in plan.items():
        cur_set, tgt_set = set(cur_ids), set(tgt_ids)
        fixed = sorted(cur_set - tgt_set)
        remaining = sorted(cur_set & tgt_set)
        introduced = sorted(tgt_set - cur_set)
        buckets[key] = (fixed, remaining, introduced)
        rep = out[key]
        rep.fixed_count = len(fixed)
        rep.remaining_count = len(remaining)
        rep.introduced_count = len(introduced)

    # Then details for a bounded, prioritised subset: what an upgrade fixes and
    # what it does not are the two facts worth describing in words.
    needed: list[str] = []
    for key, (fixed, remaining, introduced) in buckets.items():
        for group in (introduced, fixed, remaining):
            for vid in group[:detail_per_component]:
                if vid not in needed:
                    needed.append(vid)
    details: dict[str, Vuln] = {}
    for vid in needed[:max_details]:
        v = _vuln_details(vid, client)
        if v:
            details[vid] = v

    rank = lambda v: (SEVERITY_ORDER.get(v.severity, 9), v.id)
    for key, (fixed, remaining, introduced) in buckets.items():
        rep = out[key]
        # Identical sets at both versions means the comparison told us nothing.
        # Verified: Debian:12 redis returns 72 advisories at both "5" and "8.8.1",
        # while its real package version returns 25.
        if rep.approximate and not (remaining or fixed or introduced):
            rep.unmatched = True
            rep.note = (
                f"no advisories matched {rep.package} {rep.current_version} on "
                f"{cur_eco_of(rep)}. For an image this usually means the "
                "distro/package/version guess did not line up, not that the image "
                "is clean -- run an image scanner for a real answer"
            )
        elif rep.approximate and remaining and not fixed and not introduced:
            rep.indiscriminate = True
            rep.note = (
                f"{len(remaining)} advisories are recorded for {rep.package} on "
                f"{cur_eco_of(rep)}, but the same set matches both {rep.current_version} "
                f"and {rep.target_version}: distro package versions do not follow image "
                "tags, so this could not be narrowed to your image or split into "
                "fixed/remaining. Treat it as an upper bound and run a real image "
                "scanner for a per-package answer"
            )
        rep.fixed_by_upgrade = sorted(
            (details[i] for i in fixed if i in details), key=rank
        )
        rep.remaining_after_upgrade = sorted(
            (details[i] for i in remaining if i in details), key=rank
        )
        rep.introduced_by_upgrade = sorted(
            (details[i] for i in introduced if i in details), key=rank
        )
    return out
