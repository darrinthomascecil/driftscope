"""Vulnerability assessment tests: exposure, and what an upgrade would clear."""

from __future__ import annotations

import json

import pytest

from driftscope import vuln
from driftscope.models import DOCKER, MAVEN, NPM, NUGET, PYPI, Component


class FakeClient:
    """Serves canned OSV responses. `batch` maps query index -> vuln ids."""

    def __init__(self, batch, details=None):
        self.batch = batch
        self.details = details or {}
        self.posts = 0
        self.gets = 0
        self.offline = False
        self.requests_made = 0

        class _C:
            enabled = False
            hits = 0

            def get(self, k, ignore_ttl=False):
                return None

            def put(self, k, v):
                pass

        self.cache = _C()

    def get_json(self, url, **kw):
        self.gets += 1
        vid = url.rsplit("/", 1)[-1]
        if vid not in self.details:
            raise vuln.HttpError("not found")
        return self.details[vid]


def _post(fake):
    def go(url, payload, client):
        client.posts += 1
        return {"results": [{"vulns": [{"id": i} for i in ids]} for ids in fake.batch]}

    return go


def detail(vid, severity="HIGH", summary="x", aliases=None):
    return {
        "id": vid,
        "aliases": aliases or [f"CVE-2024-{vid[-4:]}"],
        "summary": summary,
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N"}],
        "database_specific": {"severity": severity},
        "affected": [{"package": {"ecosystem": "npm", "name": "x"},
                      "ranges": [{"type": "ECOSYSTEM",
                                  "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}]}],
    }


def run(pairs, batch, details, monkeypatch, **kw):
    fake = FakeClient(batch, details)
    monkeypatch.setattr(vuln, "_post_json", _post(fake))
    return vuln.assess(pairs, fake, **kw), fake


# ------------------------------------------------------------ query mapping


@pytest.mark.parametrize(
    "eco,name,version,expected",
    [
        (PYPI, "django", "2.2.28", ("PyPI", "django", "2.2.28")),
        (NPM, "express", "4.16.0", ("npm", "express", "4.16.0")),
        (NUGET, "Newtonsoft.Json", "12.0.3", ("NuGet", "Newtonsoft.Json", "12.0.3")),
        (MAVEN, "g:a", "1.0", ("Maven", "g:a", "1.0")),
    ],
)
def test_package_ecosystems_map_directly(eco, name, version, expected):
    c = Component(ecosystem=eco, name=name, current=version, source="f")
    assert vuln.osv_target(c, version) == expected


def test_v_prefix_stripped():
    c = Component(ecosystem=vuln.GO, name="github.com/x/y", current="v1.2.3", source="f")
    assert vuln.osv_target(c, "v1.2.3")[2] == "1.2.3"


def test_image_maps_to_distro_by_variant():
    c = Component(ecosystem=DOCKER, name="node", current="18-slim", source="f")
    eco, pkg, ver = vuln.osv_target(c, "18-slim")
    assert eco == "Debian:12" and pkg == "nodejs" and ver == "18"
    c2 = Component(ecosystem=DOCKER, name="postgres", current="15-alpine", source="f")
    eco2, pkg2, _ = vuln.osv_target(c2, "15-alpine")
    assert eco2.startswith("Alpine") and pkg2 == "postgresql15"


def test_unknown_image_is_not_guessed():
    c = Component(ecosystem=DOCKER, name="acme/private-thing", current="1.0", source="f")
    assert vuln.osv_target(c, "1.0") is None


def test_unsupported_ecosystem_returns_none():
    c = Component(ecosystem="terraform-provider", name="hashicorp/azurerm",
                  current="3.0.0", source="f")
    assert vuln.osv_target(c, "3.0.0") is None


# ----------------------------------------------------- what upgrading fixes


def test_fixed_by_upgrade_is_a_set_difference(monkeypatch):
    pairs = [(Component(ecosystem=NPM, name="express", current="4.16.0", source="f"), "5.2.1")]
    reps, fake = run(pairs, [["A", "B"], ["B"]],
                     {"A": detail("A"), "B": detail("B")}, monkeypatch)
    r = reps["npm:express"]
    assert r.fixed_count == 1 and r.remaining_count == 1
    assert [v.id for v in r.fixed_by_upgrade] == ["A"]
    assert [v.id for v in r.remaining_after_upgrade] == ["B"]


def test_upgrade_that_fixes_nothing_is_reported_as_such(monkeypatch):
    pairs = [(Component(ecosystem=MAVEN, name="g:a", current="1.0", source="f"), "1.9")]
    reps, _ = run(pairs, [["A", "B"], ["A", "B"]],
                  {"A": detail("A"), "B": detail("B")}, monkeypatch)
    r = reps["maven:g:a"]
    assert r.fixed_count == 0 and r.remaining_count == 2


def test_vulnerability_introduced_by_the_upgrade(monkeypatch):
    pairs = [(Component(ecosystem=NPM, name="x", current="1.0.0", source="f"), "2.0.0")]
    reps, _ = run(pairs, [[], ["NEW"]], {"NEW": detail("NEW")}, monkeypatch)
    r = reps["npm:x"]
    assert r.introduced_count == 1
    assert [v.id for v in r.introduced_by_upgrade] == ["NEW"]


def test_clean_package_has_no_data(monkeypatch):
    pairs = [(Component(ecosystem=NPM, name="x", current="1.0.0", source="f"), "2.0.0")]
    reps, _ = run(pairs, [[], []], {}, monkeypatch)
    assert reps["npm:x"].has_data is False


# ------------------------------------------- zero results: clean, or unmatched?
#
# Added after mutation testing: deleting the `unmatched` guard entirely left the
# suite green. The flag existed, was verified by hand, and had no test on the code
# that sets it -- only on the reporter that renders it once set.


def test_zero_results_on_an_image_is_unmatched_not_clean(monkeypatch):
    """The postgres case. An approximate query returning nothing means the
    distro/package/version guess found no match -- which is emphatically not the
    same as the image being clean, and must not render as silence."""
    pairs = [(Component(ecosystem=DOCKER, name="postgres", current="15",
                        source="docker-compose.yml"), "18.2")]
    reps, _ = run(pairs, [[], []], {}, monkeypatch)
    r = reps["docker-image:postgres"]
    assert r.unmatched is True
    assert r.note and "not that the image" in r.note
    # has_data must be True so the report says something rather than nothing.
    assert r.has_data is True


def test_zero_results_on_a_package_is_genuinely_clean(monkeypatch):
    """The contrast that gives the flag meaning: an exact ecosystem query is
    trustworthy, so zero there really is zero and must NOT be flagged."""
    pairs = [(Component(ecosystem=NPM, name="left-pad", current="1.0.0",
                        source="package.json"), "1.3.0")]
    reps, _ = run(pairs, [[], []], {}, monkeypatch)
    r = reps["npm:left-pad"]
    assert r.unmatched is False
    assert r.has_data is False


def test_unmatched_and_indiscriminate_are_mutually_exclusive(monkeypatch):
    """They describe different failures -- nothing matched, versus everything
    matched identically -- and a report claiming both would be incoherent."""
    empty = [(Component(ecosystem=DOCKER, name="postgres", current="15",
                        source="f"), "18.2")]
    same = [(Component(ecosystem=DOCKER, name="redis", current="5",
                       source="f"), "8.8.1")]
    a, _ = run(empty, [[], []], {}, monkeypatch)
    b, _ = run(same, [["V1", "V2"], ["V1", "V2"]],
               {"V1": detail("V1"), "V2": detail("V2")}, monkeypatch)
    ra, rb = a["docker-image:postgres"], b["docker-image:redis"]
    assert (ra.unmatched, ra.indiscriminate) == (True, False)
    assert (rb.unmatched, rb.indiscriminate) == (False, True)


def test_unmatched_withholds_the_upgrade_claim(monkeypatch):
    """Nothing was measured, so nothing may be asserted about what an upgrade
    would fix."""
    pairs = [(Component(ecosystem=DOCKER, name="postgres", current="15",
                        source="f"), "18.2")]
    reps, _ = run(pairs, [[], []], {}, monkeypatch)
    r = reps["docker-image:postgres"]
    assert r.fixed_count == 0 and r.remaining_count == 0
    assert r.fixed_by_upgrade == [] and r.remaining_after_upgrade == []


# ------------------------------------------------------------- call budget


def test_one_batch_call_for_every_component(monkeypatch):
    pairs = [
        (Component(ecosystem=NPM, name=f"p{i}", current="1.0.0", source="f"), "2.0.0")
        for i in range(8)
    ]
    reps, fake = run(pairs, [[] for _ in range(16)], {}, monkeypatch)
    assert fake.posts == 1, "all components must share a single batched query"
    assert len(reps) == 8


def test_counts_are_exact_even_when_details_are_capped(monkeypatch):
    """Details are for prose; counts must never be truncated."""
    ids = [f"V{i:03}" for i in range(50)]
    pairs = [(Component(ecosystem=NPM, name="big", current="1.0.0", source="f"), "2.0.0")]
    reps, fake = run(pairs, [ids, ids[:12]], {i: detail(i) for i in ids},
                     monkeypatch, detail_per_component=3, max_details=5)
    r = reps["npm:big"]
    assert r.fixed_count == 38 and r.remaining_count == 12
    assert fake.gets <= 5, "detail fetches must respect the cap"
    assert r.detail_truncated > 0


def test_shared_advisory_is_fetched_once(monkeypatch):
    pairs = [
        (Component(ecosystem=NPM, name="a", current="1.0.0", source="f"), "2.0.0"),
        (Component(ecosystem=NPM, name="b", current="1.0.0", source="f"), "2.0.0"),
    ]
    reps, fake = run(pairs, [["SHARED"], [], ["SHARED"], []],
                     {"SHARED": detail("SHARED")}, monkeypatch)
    assert fake.gets == 1, "the same advisory must not be fetched per component"


# ------------------------------------------- refusing an unknowable answer


def test_identical_sets_on_an_image_are_flagged_indiscriminate(monkeypatch):
    """Verified against OSV: Debian:12 redis returns the same 72 advisories at
    both "5" and "8.8.1", because distro versions do not follow image tags.
    Reporting that as "72 still present after upgrade" would be wrong."""
    ids = ["A", "B", "C"]
    pairs = [(Component(ecosystem=DOCKER, name="redis", current="5", source="f"), "8.8.1")]
    reps, _ = run(pairs, [ids, ids], {i: detail(i) for i in ids}, monkeypatch)
    r = reps["docker-image:redis"]
    assert r.indiscriminate is True
    assert r.upgrade_effect_known is False
    assert "upper bound" in (r.note or "")


def test_a_discriminating_image_result_is_trusted(monkeypatch):
    """node did discriminate (50 -> 12), so that split is kept."""
    pairs = [(Component(ecosystem=DOCKER, name="node", current="18-slim", source="f"),
              "26.5-slim")]
    reps, _ = run(pairs, [["A", "B", "C"], ["C"]],
                  {i: detail(i) for i in "ABC"}, monkeypatch)
    r = reps["docker-image:node"]
    assert r.indiscriminate is False
    assert r.fixed_count == 2 and r.remaining_count == 1


def test_package_ecosystems_are_never_flagged_indiscriminate(monkeypatch):
    """Exact ecosystems compare versions properly; identical sets there are real."""
    pairs = [(Component(ecosystem=MAVEN, name="g:a", current="1.0", source="f"), "1.9")]
    reps, _ = run(pairs, [["A"], ["A"]], {"A": detail("A")}, monkeypatch)
    assert reps["maven:g:a"].indiscriminate is False


def test_indiscriminate_reading_does_not_escalate_severity():
    from driftscope import advisor  # noqa: F401  (documents the intent)
    from driftscope.vuln import VulnReport

    r = VulnReport(approximate=True, indiscriminate=True, remaining_count=72)
    assert r.current_count == 72
    assert r.upgrade_effect_known is False


def test_failed_batch_becomes_an_error_not_silence(monkeypatch):
    pairs = [(Component(ecosystem=NPM, name="x", current="1.0.0", source="f"), "2.0.0")]
    fake = FakeClient([])
    monkeypatch.setattr(vuln, "_post_json", lambda *a, **k: None)
    reps = vuln.assess(pairs, fake)
    assert reps["npm:x"].error and "OSV" in reps["npm:x"].error


# ------------------------------------------------------------ integration


@pytest.mark.network
def test_real_osv_finds_the_known_newtonsoft_advisory():
    from driftscope.engine import build_context

    ctx = build_context(no_cache=True)
    pairs = [(Component(ecosystem=NUGET, name="Newtonsoft.Json",
                        current="12.0.3", source="f"), "13.0.4")]
    r = vuln.assess(pairs, ctx.client)["nuget:Newtonsoft.Json"]
    assert r.fixed_count >= 1
    assert any("CVE-2024-21907" in v.cve for v in r.fixed_by_upgrade)


# ------------------------------------------------------------- CVSS -> 0..100
#
# OSV returns a CVSS *vector string*, not a number, so the score is computed from
# the published v3.1 formula. Every expected value below is the authoritative NVD
# base score for that real CVE -- not a plausible-looking figure. An earlier draft
# of this test invented "1.6" for a near-minimum vector; hand-computing it gave
# 1.8, and the formula was right. Ground truth or nothing.


@pytest.mark.parametrize("vector,expected,cve", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", 7.5, "CVE-2020-36518"),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0, "CVE-2021-44228"),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8, "CVE-2022-22965"),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:H", 8.8, "CVE-2023-4863"),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1, "CVE-2019-11358"),
])
def test_base_score_matches_nvd(vector, expected, cve):
    from driftscope.vuln import cvss_base_score

    assert cvss_base_score(vector) == expected, cve


def test_the_spec_rounding_is_used_not_ordinary_rounding():
    """CVSS rounds *up* to one decimal. Ordinary round() disagrees at the
    boundaries, which is how a computed 9.8 becomes a 9.7 that matches no
    advisory anyone can look up."""
    from driftscope.vuln import _cvss_roundup

    assert _cvss_roundup(4.02) == 4.1
    assert _cvss_roundup(4.00) == 4.0
    assert _cvss_roundup(1.745398776) == 1.8


def test_score_is_the_base_score_times_ten():
    from driftscope.vuln import Vuln

    v = Vuln(id="x", cvss="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    assert v.score == 98


@pytest.mark.parametrize("vector", [
    None, "", "not-a-vector", "7.5",
    "CVSS:2.0/AV:N/AC:L/Au:N/C:P/I:P/A:P",          # v2: different formula
    "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H",  # v4: different formula
    "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",  # invalid metric value
    "CVSS:3.1/AV:N/AC:L",                            # truncated
])
def test_unscoreable_vectors_are_none_not_zero(vector):
    """0.0 is a real score meaning "no impact". A missing measurement must not
    impersonate one -- the same rule that makes a failed count None rather than 0.
    v2 and v4 use different formulas; guessing an equivalent would invent a
    number."""
    from driftscope.vuln import Vuln, cvss_base_score

    assert cvss_base_score(vector) is None
    assert Vuln(id="x", cvss=vector or "").score is None


def test_a_genuine_zero_is_distinguishable_from_unscored():
    """The other direction: a vector with no impact really does score 0."""
    from driftscope.vuln import Vuln, cvss_base_score

    zero = "CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N"
    assert cvss_base_score(zero) == 0.0
    assert Vuln(id="x", cvss=zero).score == 0


def test_scope_change_uses_the_other_privilege_weights():
    """Scope-changed vectors weight PR differently and apply a 1.08 multiplier.
    Using one branch for both silently misprices every scope-changed advisory."""
    from driftscope.vuln import cvss_base_score

    unchanged = cvss_base_score("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H")
    changed = cvss_base_score("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H")
    assert unchanged == 8.8 and changed == 9.9
