"""The forge seam, driven by recorded Azure DevOps payloads. No network.

The work-item and state fixtures are **real** responses from a live Scrum project,
trimmed to the fields driftscope requests. That matters: several past bugs in this
codebase were in hand-written test doubles rather than the code, so where a real
payload was obtainable it was recorded rather than invented. The tag fixtures are
hand-built to the documented shape, and say so -- no repository in the recording
organisation had any tags.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from driftscope.forge import (
    AZURE_DEVOPS,
    GITHUB,
    NOT_SAMPLED,
    Forge,
    IssueRecord,
    IssueWalk,
    ReleaseRecord,
    resolve_forge,
)
from driftscope.forge.ado import NON_ISSUE_TYPES, AdoForge
from driftscope.http import Cache

FIX = Path(__file__).parent / "fixtures" / "ado"


def load(name: str):
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.fixture
def ado(tmp_path, monkeypatch):
    """An AdoForge whose transport replays the recorded payloads."""
    forge = AdoForge(Cache(root=tmp_path / "cache"), enabled=True)
    monkeypatch.setattr("driftscope.forge.ado.ado_token", lambda: "TOKEN")

    states = load("states")
    tags = load("refs-tags")
    annotated = load("annotatedtags")

    def fake_call(url, *, body=None):
        forge.queries += 1
        if "wit/wiql" in url:
            return load("wiql")
        if "wit/workitemsbatch" in url:
            return load("workitemsbatch")
        if "/states" in url:
            from urllib.parse import unquote
            wi_type = unquote(url.split("workitemtypes/")[1].split("/states")[0])
            return {"value": [{"name": n, "category": c}
                              for n, c in states.get(wi_type, {}).items()]}
        if "annotatedtags/" in url:
            oid = url.split("annotatedtags/")[1].split("?")[0]
            return annotated.get(oid)
        if "/refs" in url:
            return tags
        return None

    monkeypatch.setattr(forge, "_call", fake_call)
    return forge


# ------------------------------------------------------------------- protocol


def test_both_clients_satisfy_the_protocol(ado):
    from driftscope.forge.github import GitHubForge
    from driftscope.http import Cache as C, GitHub

    assert isinstance(ado, Forge)
    assert isinstance(GitHubForge(GitHub(cache=C(root=Path("/tmp/x"), enabled=False))), Forge)


def test_resolution_without_a_forge_routes_to_github():
    """Every existing resolver omits `forge`, so the default has to be GitHub or
    twenty call sites would need editing."""
    forges = {GITHUB: "gh-client", AZURE_DEVOPS: "ado-client"}
    assert resolve_forge(None, forges) == "gh-client"
    assert resolve_forge(GITHUB, forges) == "gh-client"
    assert resolve_forge(AZURE_DEVOPS, forges) == "ado-client"


def test_unknown_forge_is_none_not_a_wrong_client():
    assert resolve_forge("bitbucket", {GITHUB: "gh"}) is None


# ------------------------------------------------------------- repo addressing


@pytest.mark.parametrize("spec,expected", [
    ("contoso/Platform/payments", ("contoso", "Platform", "payments")),
    ("https://dev.azure.com/contoso/Platform/_git/payments",
     ("contoso", "Platform", "payments")),
    # Two segments: the repo shares the project's name, as ADO permits.
    ("contoso/payments", ("contoso", "payments", "payments")),
])
def test_repo_spec_forms(spec, expected):
    assert AdoForge._split(spec) == expected


def test_unparseable_repo_spec_is_none():
    assert AdoForge._split("just-one-part") is None
    assert AdoForge._split("") is None


def test_web_url(ado):
    assert ado.web_url("contoso/Platform/payments") == (
        "https://dev.azure.com/contoso/Platform/_git/payments"
    )


# ----------------------------------------------------------------- work items


def test_issues_walk_returns_records(ado):
    walk = ado.issues_since("contoso/Platform/payments", date(2020, 1, 1))
    assert walk.readable
    assert walk.items
    assert all(isinstance(i, IssueRecord) for i in walk.items)


def test_test_artifacts_are_excluded_like_pull_requests(ado):
    """Real data: Test Cases, Test Suites and Test Plans were 16 of 60 items. They
    are process scaffolding, not reported problems, and counting them would inflate
    the backlog the same way counting merged PRs would on GitHub."""
    walk = ado.issues_since("contoso/Platform/payments", date(2020, 1, 1))
    kinds = {i.title for i in walk.items}
    raw = load("workitemsbatch")["value"]
    artifacts = [w for w in raw
                 if w["fields"]["System.WorkItemType"].lower() in NON_ISSUE_TYPES]
    assert artifacts, "fixture must contain artifact types or this asserts nothing"
    assert len(walk.items) == len(raw) - len(artifacts)
    for w in artifacts:
        assert w["fields"]["System.Title"] not in kinds
    assert any("artifact" in n for n in walk.notes)


def test_the_unsampled_sentinel_cannot_be_a_real_count():
    """The property, not the constant.

    Asserting `positive == NOT_SAMPLED` is a tautology: redefine the constant and
    both sides of the comparison move together. Mutation testing caught exactly
    that -- setting NOT_SAMPLED = 0 introduced the "unsampled reads as zero
    frustration" bug and the suite stayed green.

    What actually has to hold is that the sentinel is outside the range of a real
    measurement. A reaction count cannot be negative, so a negative sentinel is
    the thing that keeps "we did not look" distinguishable from "we looked and
    found none".
    """
    assert NOT_SAMPLED < 0


def test_reactions_are_not_sampled_and_say_so(ado):
    """The sentinel is the whole point: unsampled must not read as zero."""
    walk = ado.issues_since("contoso/Platform/payments", date(2020, 1, 1))
    assert walk.reactions_sampled is False
    assert all(i.positive < 0 and i.negative < 0 for i in walk.items)
    assert all(not i.reactions_sampled for i in walk.items)
    assert any("sentiment not measured" in n for n in walk.notes)


def test_a_genuine_zero_is_not_mistaken_for_unsampled():
    """The other direction: an item that really has no reactions must report
    sampled, so the two states stay distinct in both directions."""
    r = IssueRecord(number=1, title="t", url="u", state="New",
                    created=date(2026, 1, 1), closed=None,
                    positive=0, negative=0)
    assert r.reactions_sampled is True
    assert IssueRecord(number=1, title="t", url="u", state="New",
                       created=None, closed=None).reactions_sampled is False


def test_unreadable_project_is_none_not_empty(ado, monkeypatch):
    """A 403 on a private project must not look like a project with no work."""
    monkeypatch.setattr(ado, "_call", lambda url, *, body=None: None)
    walk = ado.issues_since("contoso/Platform/payments", date(2020, 1, 1))
    assert walk.items is None
    assert walk.readable is False


def test_empty_project_is_empty_not_none(ado, monkeypatch):
    real = ado._call
    monkeypatch.setattr(ado, "_call", lambda url, *, body=None:
                        {"workItems": []} if "wiql" in url else real(url, body=body))
    walk = ado.issues_since("contoso/Platform/payments", date(2020, 1, 1))
    assert walk.items == []
    assert walk.readable is True


# --------------------------------------------------- process-dependent closure


def test_closed_is_resolved_by_category_not_by_name(ado):
    """Recorded from one real project: Bug closes as `Done`, Test Case closes as
    `Closed`, and a Basic project would say `Closed` for everything. Hardcoding any
    single name silently reports zero closures on the others."""
    states = load("states")
    assert states["Bug"]["Done"] == "Completed"
    assert states["Test Case"]["Closed"] == "Completed"
    assert "Closed" not in states["Bug"], "the exact trap this guards against"

    assert ado._is_closed("contoso", "Platform",
                          {"System.WorkItemType": "Bug", "System.State": "Done"})
    assert not ado._is_closed("contoso", "Platform",
                              {"System.WorkItemType": "Bug", "System.State": "Committed"})
    assert not ado._is_closed("contoso", "Platform",
                              {"System.WorkItemType": "Bug", "System.State": "New"})


def test_removed_counts_as_closed(ado):
    assert ado._is_closed("contoso", "Platform",
                          {"System.WorkItemType": "Bug", "System.State": "Removed"})


def test_closed_date_field_short_circuits_the_state_lookup(ado):
    """Agile and CMMI populate ClosedDate; Basic and Scrum may not. Either path
    must reach the same answer."""
    assert ado._is_closed("contoso", "Platform",
                          {"Microsoft.VSTS.Common.ClosedDate": "2026-01-02T00:00:00Z"})


def test_state_map_is_fetched_once_per_type(ado):
    before = ado.queries
    for _ in range(5):
        ado._is_closed("contoso", "Platform",
                       {"System.WorkItemType": "Bug", "System.State": "Done"})
    assert ado.queries - before == 1, "the state map must be cached for the run"


# ----------------------------------------------------------------------- tags


def test_annotated_tags_carry_date_and_message(ado):
    rels = {r.tag: r for r in ado.releases("contoso/Platform/payments")}
    assert rels["v2.4.0"].published == date(2026, 1, 10)
    assert "BREAKING CHANGES" in rels["v2.4.0"].body


def test_lightweight_tag_has_no_date_and_is_not_invented(ado):
    """A lightweight tag points straight at a commit: no tagger, no date, no
    message. Dating it from the commit would make `days behind` mean something
    different per tag."""
    rels = {r.tag: r for r in ado.releases("contoso/Platform/payments")}
    lw = rels["nightly-2026-01-04"]
    assert lw.published is None
    assert lw.body == ""


def test_tag_bodies_feed_the_existing_breaking_note_miner(ado):
    """The delta phase's note miner takes (tag, body) pairs, so ADO tag messages
    work through it unchanged -- including the section-heading filter."""
    from driftscope.ghub.delta import _breaking_from_releases

    rels = ado.releases("contoso/Platform/payments")
    notes = _breaking_from_releases(
        [{"tag_name": r.tag, "body": r.body} for r in rels])
    assert any("legacyMode" in n for n in notes)
    assert not any(n.rstrip().endswith("BREAKING CHANGES:") for n in notes)


def test_releases_on_unreadable_repo_is_empty(ado, monkeypatch):
    monkeypatch.setattr(ado, "_call", lambda url, *, body=None: None)
    assert ado.releases("contoso/Platform/payments") == []


# --------------------------------------------------------------------- counts


def test_issue_counts_are_exact_not_estimated(ado):
    """WIQL returns the full ID set, so the count is a length rather than a search
    estimate -- no rate-limited zero is possible here."""
    total, still_open = ado.issue_counts(
        "contoso/Platform/payments", "2020-01-01", "2026-12-31")
    assert total is not None and still_open is not None
    assert total >= still_open >= 0


def test_failed_count_query_is_none_not_zero(ado, monkeypatch):
    """The bug this whole design exists to prevent: a failed query reading as a
    clean project."""
    monkeypatch.setattr(ado, "_call", lambda url, *, body=None: None)
    assert ado.issue_counts("contoso/Platform/payments", "2020-01-01", "2026-12-31") == (
        None, None
    )


def test_records_are_immutable():
    """Frozen so a reporter cannot quietly rewrite a measurement it is rendering."""
    r = IssueRecord(number=1, title="t", url="u", state="New",
                    created=date(2026, 1, 1), closed=None)
    with pytest.raises(Exception):
        r.number = 2  # type: ignore[misc]
    rel = ReleaseRecord(tag="v1")
    with pytest.raises(Exception):
        rel.tag = "v2"  # type: ignore[misc]


def test_issue_walk_defaults_to_unreadable():
    """The safe default: a walk nobody populated must not look like an empty
    project."""
    assert IssueWalk().readable is False
