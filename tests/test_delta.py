"""GitHub delta note mining. No network."""

from __future__ import annotations

from driftscope.forge import ReleaseRecord
from driftscope.ghub.delta import (
    _breaking_from_releases,
    _is_section_heading,
    _match_release,
    _releases_between,
)

# Trimmed from the real hashicorp/terraform-provider-azurerm release bodies that
# surfaced this bug during a live scan.
AZURERM_BODY = """DEPRECATIONS:

* `azurerm_kubernetes_cluster` - the `ebpf_data_plane` property has been deprecated and superseded by the `network_data_plane` property
* `azurerm_api_management` - the `policy` block has been deprecated is superseded by the `azurerm_api_management_policy` resource

FEATURES:

* New resource: `azurerm_nginx_configuration`

ENHANCEMENTS:

* dependencies: updating to `v0.20240610.1112550` of `github.com/hashicorp/go-azure-sdk`
"""


def test_section_headings_are_not_breaking_notes():
    """Regression: `DEPRECATIONS:` matched the breaking patterns and was quoted as
    a finding, inflating the breaking-change count with content-free entries."""
    notes = _breaking_from_releases([{"tag_name": "v3.108.0", "body": AZURERM_BODY}])
    assert notes, "the two real deprecation lines should still be captured"
    assert not any(n.endswith("DEPRECATIONS:") for n in notes)
    assert all("ebpf_data_plane" in n or "api_management_policy" in n for n in notes), notes
    assert len(notes) == 2


def test_real_prose_is_still_captured():
    body = "* `azurerm_thing` - the `old` property has been removed in favour of `new`\n"
    notes = _breaking_from_releases([{"tag_name": "v4.0.0", "body": body}])
    assert len(notes) == 1
    assert notes[0].startswith("v4.0.0: ")


def test_heading_detection():
    for heading in (
        "DEPRECATIONS:",
        "BREAKING CHANGES:",
        "BUG FIXES:",
        "Notes for upgraders:",
    ):
        assert _is_section_heading(heading), heading
    for prose in (
        "`azurerm_x` - the `y` property has been deprecated and superseded by `z`",
        "Minimum required version of Terraform is now 1.3:",
        "removed support for the legacy authentication flow",
    ):
        assert not _is_section_heading(prose), prose


# --------------------------------------------------- the version window itself
#
# The window is the delta's foundation: get its bounds wrong and every release
# and issue attributed to it is wrong too. It was originally computed by comparing
# ISO-8601 timestamps as *strings*, which is only correct when every timestamp
# happens to share one format. Two forms break it, and both occur in real data.


def rel(tag: str, published: str | None, body: str = "") -> ReleaseRecord:
    """A ReleaseRecord from an ISO string, exactly as the forge adapter builds it."""
    from driftscope.forge.github import _as_instant

    return ReleaseRecord(tag=tag, published=_as_instant(published), body=body)


def test_fractional_seconds_do_not_invert_the_ordering():
    """`12:00:00.123456Z` is LATER than `12:00:00Z`, but sorts earlier as a string
    because '.' (0x2E) precedes 'Z' (0x5A).

    Registries differ here: GitHub emits whole seconds, while a datetime round
    tripped through parse_iso keeps microseconds, so a window bound and a release
    date routinely arrive in different forms.
    """
    releases = [
        rel("v1.1", "2023-07-05T12:00:00.500000Z"),   # inside, just after the floor
        rel("v1.2", "2023-08-01T00:00:00Z"),
    ]
    got = _releases_between(releases, "2023-07-05T12:00:00Z", "2023-09-01T00:00:00Z")
    assert {r.tag for r in got} == {"v1.1", "v1.2"}


def test_a_non_utc_offset_is_compared_as_an_instant():
    """`14:00+02:00` and `12:00Z` are the same instant. Compared as strings, the
    first looks two hours later, so a release exactly on the boundary lands on the
    wrong side of it."""
    releases = [rel("v2.0", "2023-07-05T14:00:00+02:00")]  # == 12:00:00Z
    # Floor is that same instant, and the bound is exclusive, so it must be OUT.
    got = _releases_between(releases, "2023-07-05T12:00:00Z", "2023-09-01T00:00:00Z")
    assert got == [], "a release at the exclusive floor must be excluded"
    # One second later it is genuinely inside.
    got = _releases_between(releases, "2023-07-05T11:59:59Z", "2023-09-01T00:00:00Z")
    assert [r.tag for r in got] == ["v2.0"]


def test_window_bounds_are_half_open():
    """Exclusive floor, inclusive ceiling: the version you are ON is not part of
    what changed, the version you are moving TO is."""
    releases = [
        rel("from", "2023-01-01T00:00:00Z"),
        rel("mid", "2023-06-01T00:00:00Z"),
        rel("to", "2023-12-01T00:00:00Z"),
    ]
    got = _releases_between(releases, "2023-01-01T00:00:00Z", "2023-12-01T00:00:00Z")
    assert [r.tag for r in got] == ["mid", "to"]


def test_releases_are_returned_oldest_first():
    releases = [rel("c", "2023-09-01T00:00:00Z"), rel("a", "2023-02-01T00:00:00Z"),
                rel("b", "2023-05-01T00:00:00Z")]
    got = _releases_between(releases, "2023-01-01T00:00:00Z", "2023-12-01T00:00:00Z")
    assert [r.tag for r in got] == ["a", "b", "c"]


def test_undated_releases_are_skipped_not_placed():
    """A lightweight git tag has no tagger date. It cannot be put in a window, and
    dating it from its commit would make `days behind` mean something different
    per tag."""
    releases = [
        rel("good", "2023-06-01T00:00:00Z"),
        rel("lightweight", None),
        rel("junk", "not-a-date"),
    ]
    got = _releases_between(releases, "2023-01-01T00:00:00Z", "2023-12-01T00:00:00Z")
    assert [r.tag for r in got] == ["good"]


def test_no_releases_yields_an_empty_window():
    assert _releases_between([], "2023-01-01T00:00:00Z", "2023-12-01T00:00:00Z") == []


def test_match_release_handles_the_tag_shapes_projects_use():
    releases = [rel("v1.2.3", "2023-01-01T00:00:00Z"),
                rel("2.0.0", "2023-06-01T00:00:00Z"),
                rel("myproj-3.0.0", "2023-09-01T00:00:00Z")]
    assert _match_release(releases, "1.2.3").tag == "v1.2.3"
    assert _match_release(releases, "v1.2.3").tag == "v1.2.3"
    assert _match_release(releases, "2.0.0").tag == "2.0.0"
    assert _match_release(releases, "3.0.0").tag == "myproj-3.0.0"
    assert _match_release(releases, "9.9.9") is None
    assert _match_release(releases, "") is None


def test_untagged_release_never_matches_a_version():
    """A dated release with no tag counts toward cadence but cannot identify a
    version -- matching it would date the window from the wrong release."""
    assert _match_release([rel("", "2023-01-01T00:00:00Z")], "1.0.0") is None


def test_notes_are_capped_and_deduplicated():
    line = "* `azurerm_x` - the `y` property has been removed\n"
    releases = [{"tag_name": f"v3.{i}.0", "body": line * 5} for i in range(20)]
    notes = _breaking_from_releases(releases)
    assert len(notes) <= 25
    assert len(notes) == len(set(notes)), "duplicates within a release must collapse"


# ------------------------------------------------- which labels count as a bug
#
# BUG_LABELS existed as a module constant and was never referenced: every query
# hardcoded label:"bug". Measured against kubernetes/kubernetes over one window,
# that spelling returns 0 while kind/bug returns 332 -- reported as "no labelled
# bugs found", which is the confident wrong zero this codebase exists to prevent.


def test_the_label_clause_is_a_single_or_qualifier():
    """GitHub reads one comma-separated `label:` as OR. Repeating the qualifier is
    AND, and no issue carries every bug-ish label at once -- that spelling returns
    0, which is exactly the bug this replaced."""
    from driftscope.forge import BUG_LABELS
    from driftscope.forge.github import GitHubForge

    clause = GitHubForge.label_clause()
    assert clause.count("label:") == 1, "repeating the qualifier means AND"
    assert clause.startswith('label:"')
    for lbl in BUG_LABELS:
        assert f'"{lbl}"' in clause


def test_the_conventions_that_actually_occur_are_covered():
    from driftscope.forge import BUG_LABELS

    for lbl in ("bug", "kind/bug", "type/bug", "regression", "defect"):
        assert lbl in BUG_LABELS, f"{lbl} is a real convention and must be queried"


def test_counts_query_uses_every_label(monkeypatch):
    from driftscope.forge import BUG_LABELS
    from driftscope.forge.github import GitHubForge

    seen = []

    class GH:
        enabled, queries = True, 0
        def search_count(self, q):
            seen.append(q); return 7
        def api(self, path, **kw): return None

    total, opened = GitHubForge(GH()).issue_counts("k8s/k8s", "2024-01-01", "2024-06-01")
    assert (total, opened) == (7, 7)
    assert seen and all("kind/bug" in q for q in seen), (
        "a project using kind/bug must not report zero"
    )
    assert all(q.count("label:") == 1 for q in seen)
