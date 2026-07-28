"""Report sections: the ecosystem partition used to break a report into parts.

The property that matters most here is completeness. A newly registered
ecosystem that quietly failed to appear in any section would vanish from the
report while still being scanned and resolved -- a finding that exists, is
correct, and is never shown. That is the same failure the resolver contract test
guards against from the other side.
"""

from __future__ import annotations

import pytest

from driftscope.models import (
    Component, Finding, Lag, Resolution,
    SEV_CRITICAL, SEV_CURRENT, SEV_HIGH, SEV_LOW, SEV_MEDIUM, SEV_UNKNOWN,
    severity_rank,
)
from driftscope.report.summarize import (
    OTHER_SECTION, SECTIONS, group_findings, section_for,
)
from tests.test_resolver_contract import ECOSYSTEMS


def f(ecosystem, severity=SEV_HIGH, name="x"):
    return Finding(
        component=Component(ecosystem=ecosystem, name=name, current="1.0",
                            source="manifest"),
        resolution=Resolution(), lag=Lag(), severity=severity,
    )


def group(findings):
    return group_findings(findings, severity_rank=severity_rank)


# ------------------------------------------------------------- the partition


def test_every_registered_ecosystem_lands_in_a_named_section():
    """If this fails, a scanned ecosystem is being resolved and then dropped from
    the report -- add it to SECTIONS rather than relaxing the assertion."""
    unplaced = [
        eco for eco, _, _ in ECOSYSTEMS
        if section_for(eco) == OTHER_SECTION[0]
    ]
    assert unplaced == [], f"ecosystems with no report section: {unplaced}"


def test_the_sections_do_not_overlap():
    """`cli.ECOSYSTEM_GROUPS` overlaps on purpose (nuget is in deps and dotnet).
    Building report sections on those would print a finding twice."""
    seen = set()
    for _, _, ecos in SECTIONS:
        dupes = seen & set(ecos)
        assert not dupes, f"ecosystem in two sections: {dupes}"
        seen |= set(ecos)


def test_an_unknown_ecosystem_falls_into_other_rather_than_disappearing():
    assert section_for("some-future-ecosystem") == OTHER_SECTION[0]
    sections = group([f("some-future-ecosystem")])
    assert [s.key for s in sections] == [OTHER_SECTION[0]]


def test_every_finding_appears_exactly_once():
    findings = [f(eco) for _, _, ecos in SECTIONS for eco in ecos]
    placed = [x for s in group(findings) for x in s.findings]
    assert len(placed) == len(findings)
    assert {id(x) for x in placed} == {id(x) for x in findings}


# ----------------------------------------------------------------- ordering


def test_the_worst_section_comes_first():
    sections = group([
        f("npm", SEV_LOW), f("docker-image", SEV_CRITICAL), f("github-action", SEV_MEDIUM),
    ])
    assert [s.key for s in sections] == ["containers", "ci", "deps"]


def test_a_tie_on_severity_is_broken_by_how_many_sit_there():
    sections = group([
        f("npm", SEV_HIGH, "a"),
        f("docker-image", SEV_HIGH, "b"), f("helm-chart", SEV_HIGH, "c"),
    ])
    assert sections[0].key == "containers", "two highs should outrank one"


def test_findings_within_a_section_are_worst_first():
    s = group([f("npm", SEV_LOW, "a"), f("pypi", SEV_CRITICAL, "b")])[0]
    assert [x.severity for x in s.findings] == [SEV_CRITICAL, SEV_LOW]


def test_ordering_is_stable_for_the_same_input():
    findings = [f("npm", SEV_HIGH, "a"), f("pypi", SEV_HIGH, "b"),
                f("docker-image", SEV_HIGH, "c")]
    assert [s.key for s in group(findings)] == [s.key for s in group(findings)]


# ------------------------------------------------------------------- counts


def test_counts_are_per_severity_within_the_section():
    s = group([f("npm", SEV_HIGH, "a"), f("pypi", SEV_HIGH, "b"),
               f("maven", SEV_LOW, "c")])[0]
    assert s.counts == {SEV_HIGH: 2, SEV_LOW: 1}
    assert s.worst == SEV_HIGH


def test_actionable_excludes_current_and_unknown():
    """"3 of 5 need attention" is the useful number; counting a healthy component
    as work makes every section look worse than it is."""
    s = group([f("npm", SEV_HIGH, "a"), f("pypi", SEV_CURRENT, "b"),
               f("maven", SEV_UNKNOWN, "c"), f("nuget", SEV_LOW, "d")])[0]
    assert s.actionable == 2


def test_a_section_with_nothing_in_it_is_omitted_entirely():
    """Printing "Containers: none" claims something about an estate that may
    simply have no containers."""
    sections = group([f("npm")])
    assert [s.key for s in sections] == ["deps"]


def test_no_findings_yields_no_sections():
    assert group([]) == []


def test_labels_are_human_readable():
    s = group([f("terraform-provider")])[0]
    assert s.label == "Infrastructure as code"
