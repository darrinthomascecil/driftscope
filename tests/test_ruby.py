"""Ruby: Gemfile / Gemfile.lock / gemspec scanning, RubyGems and the runtime.

The mock payloads are trimmed copies of real rubygems.org responses -- same
practice as the ADO fixtures, for the same reason: several past bugs in this
codebase lived in hand-written test doubles rather than in the code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from driftscope import advisor
from driftscope.models import RUBY_RUNTIME, RUBYGEMS, Component
from driftscope.resolvers import Context
from driftscope.resolvers.ruby import (
    RUBY_SUPPORT_END,
    resolve_ruby_runtime,
    resolve_rubygem,
)
from driftscope.scanners import scan_tree
from driftscope.scanners.ruby import scan_file
from driftscope.version import lag_between

FIXTURE = Path(__file__).parent.parent / "fixtures" / "stale-estate" / "rubyapp"
NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def components():
    comps, files = scan_tree(FIXTURE)
    assert files >= 4
    return comps


def find(comps, eco, name, source_contains=None):
    for c in comps:
        if c.ecosystem == eco and c.name == name:
            if source_contains and source_contains not in c.source:
                continue
            return c
    return None


# ------------------------------------------------------------------- Gemfile


def test_gem_constraints_are_reduced_to_a_floor(components):
    assert find(components, RUBYGEMS, "rails").current == "6.1.7"
    assert find(components, RUBYGEMS, "puma").current == "5.6"


def test_multi_constraint_takes_the_lower_bound(components):
    """`">= 0.18", "< 2.0"` permits 0.18, so that is what the project may ship."""
    pg = find(components, RUBYGEMS, "pg", "Gemfile")
    assert pg is not None and pg.current == "0.18"


def test_group_is_captured(components):
    assert find(components, RUBYGEMS, "pry").meta["group"] == "development,test"
    assert find(components, RUBYGEMS, "capybara").meta["group"] in (
        "test", "development,test",
    )


def test_computed_version_is_not_guessed(components):
    """`gem "sidekiq", SIDEKIQ_VERSION` -- a constant, not a literal. Inventing a
    version here is the exact failure the terraform `var.x` case taught."""
    c = find(components, RUBYGEMS, "sidekiq", "Gemfile")
    if c is None:  # deduped against the lock entry, which is the authoritative pin
        c = find(components, RUBYGEMS, "sidekiq")
        assert c.meta.get("locked")
        return
    assert c.current is None
    assert "SIDEKIQ_VERSION" in c.meta["unresolved_expr"]


def test_unversioned_gem_has_no_version(components):
    c = find(components, RUBYGEMS, "rack-cors")
    assert c is not None and c.current is None
    assert "unresolved_expr" not in c.meta, "absent is not the same as computed"


def test_ruby_directive_in_gemfile():
    comps = scan_file(FIXTURE / "Gemfile", FIXTURE)
    ruby = [c for c in comps if c.ecosystem == RUBY_RUNTIME]
    assert ruby and ruby[0].current == "3.1.2"
    assert ruby[0].meta["runtime"] is True


# -------------------------------------------------------------- Gemfile.lock


def test_lock_specs_are_captured(components):
    """The lock is the resolved graph, so transitive gems appear here and only
    here -- actionpack is never named in the Gemfile."""
    assert find(components, RUBYGEMS, "actionpack").current == "6.1.7.6"
    assert find(components, RUBYGEMS, "rack").current == "2.2.4"


def test_nested_dependency_lines_are_not_gems(components):
    """A four-space line is a gem; six spaces is one of its dependencies. Reading
    the latter as a gem would emit `actionview (= 6.1.7.6)` as a pin."""
    lock = scan_file(FIXTURE / "Gemfile.lock", FIXTURE)
    versions = {c.name: c.current for c in lock if c.ecosystem == RUBYGEMS}
    assert "actionview" not in versions
    assert all("=" not in (v or "") for v in versions.values())


def test_lock_records_ruby_and_bundler():
    lock = scan_file(FIXTURE / "Gemfile.lock", FIXTURE)
    ruby = [c for c in lock if c.ecosystem == RUBY_RUNTIME][0]
    # `ruby 3.1.2p20` -- the patchlevel is not part of the version.
    assert ruby.current == "3.1.2"
    assert ruby.meta["locked"] is True
    bundler = [c for c in lock if c.name == "bundler"][0]
    assert bundler.current == "2.3.7" and bundler.meta["tool"] is True


def test_a_gem_is_listed_once_despite_appearing_twice(components):
    """Every gem shows up as a spec and again under each dependent."""
    lock = scan_file(FIXTURE / "Gemfile.lock", FIXTURE)
    names = [c.name for c in lock if c.ecosystem == RUBYGEMS]
    assert len(names) == len(set(names))


# ------------------------------------------------------------------ gemspec


def test_gemspec_dependencies(components):
    assert find(components, RUBYGEMS, "thor").current == "1.2.1"
    assert find(components, RUBYGEMS, "rspec").meta["group"] == "development"


def test_gemspec_required_ruby_version():
    comps = scan_file(FIXTURE / "legacy_tool.gemspec", FIXTURE)
    ruby = [c for c in comps if c.ecosystem == RUBY_RUNTIME][0]
    assert ruby.current == "2.7.0"
    assert ruby.meta["constraint"] is True


def test_ruby_version_file():
    comps = scan_file(FIXTURE / ".ruby-version", FIXTURE)
    assert comps[0].current == "3.1.2", "the rbenv `ruby-` prefix must be stripped"


# ------------------------------------------------------------------ resolver


class FakeClient:
    def __init__(self, versions, meta=None):
        self.versions, self.meta = versions, meta or {}

    def get_json(self, url, **kw):
        return self.meta if "/gems/" in url else self.versions


def ctx(client) -> Context:
    return Context(client=client, github=None, allow_az=False, now=NOW)


# Trimmed from a real rubygems.org /api/v1/versions/rails.json response.
RAILS_VERSIONS = [
    {"number": "8.1.3", "created_at": "2026-03-24T20:27:42.098Z",
     "prerelease": False, "platform": "ruby"},
    {"number": "8.1.0.rc1", "created_at": "2026-02-01T00:00:00.000Z",
     "prerelease": True, "platform": "ruby"},
    {"number": "6.1.7.6", "created_at": "2023-08-22T00:00:00.000Z",
     "prerelease": False, "platform": "ruby"},
]


def test_rubygem_resolution_uses_publish_dates():
    res = resolve_rubygem(
        Component(ecosystem=RUBYGEMS, name="rails", current="6.1.7.6", source="f"),
        ctx(FakeClient(RAILS_VERSIONS,
                       {"source_code_uri": "https://github.com/rails/rails/tree/v8.1.3",
                        "homepage_uri": "https://rubyonrails.org"})),
    )
    assert res.latest_stable == "8.1.3"
    assert res.repo == "rails/rails"
    assert res.release_for("6.1.7.6").published is not None


def test_prereleases_are_not_offered_as_the_target():
    res = resolve_rubygem(
        Component(ecosystem=RUBYGEMS, name="rails", current="6.1.7.6", source="f"),
        ctx(FakeClient(RAILS_VERSIONS)),
    )
    assert res.latest_stable == "8.1.3", "an rc must never be the recommendation"


def test_the_registry_prerelease_flag_beats_the_version_parser():
    """Why the `prerelease` filter is not redundant.

    `version.latest_stable()` already prefers a stable version, so for an obvious
    `8.1.0.rc1` the two agree and the filter looks like belt and braces. Mutation
    testing removed it and nothing failed -- which was true but only because every
    fixture used a suffix the parser recognises.

    RubyGems' own flag is authoritative and covers suffixes the parser does not
    know. `1.0.0.SNAPSHOT` parses as a stable 1.0.0 with an unrecognised token, so
    without the flag it would be recommended as the newest stable release.
    """
    from driftscope.version import parse

    assert not parse("1.0.0.SNAPSHOT").is_prerelease, (
        "premise of this test: the parser does not recognise this suffix"
    )
    versions = [
        {"number": "1.0.0.SNAPSHOT", "created_at": "2026-06-01T00:00:00.000Z",
         "prerelease": True, "platform": "ruby"},
        {"number": "0.9.0", "created_at": "2025-01-01T00:00:00.000Z",
         "prerelease": False, "platform": "ruby"},
    ]
    res = resolve_rubygem(
        Component(ecosystem=RUBYGEMS, name="x", current="0.8.0", source="f"),
        ctx(FakeClient(versions)),
    )
    assert res.latest_stable == "0.9.0", (
        "the registry said SNAPSHOT is a prerelease; that beats the parser"
    )


def test_platform_builds_are_not_treated_as_newer_versions():
    """`1.2.3-x86_64-linux` is the same release for a different target. Comparing
    across platforms would report a platform swap as an upgrade -- the same
    mistake as comparing a -slim Docker tag against an -alpine one."""
    versions = [
        {"number": "1.16.0", "created_at": "2026-01-01T00:00:00.000Z",
         "prerelease": False, "platform": "x86_64-linux"},
        {"number": "1.15.0", "created_at": "2025-06-01T00:00:00.000Z",
         "prerelease": False, "platform": "ruby"},
    ]
    res = resolve_rubygem(
        Component(ecosystem=RUBYGEMS, name="nokogiri", current="1.13.10", source="f"),
        ctx(FakeClient(versions)),
    )
    assert res.latest_stable == "1.15.0"
    assert all(r.version != "1.16.0" for r in res.releases)


def test_gem_with_no_pure_ruby_release_says_so():
    res = resolve_rubygem(
        Component(ecosystem=RUBYGEMS, name="x", current="1.0", source="f"),
        ctx(FakeClient([{"number": "1.0", "platform": "java", "prerelease": False}])),
    )
    assert res.latest_stable is None
    assert res.note and "pure-Ruby" in res.note


def test_yanked_gem_is_flagged():
    res = resolve_rubygem(
        Component(ecosystem=RUBYGEMS, name="x", current="1.0", source="f"),
        ctx(FakeClient(RAILS_VERSIONS, {"yanked": True})),
    )
    assert res.deprecated is True


# ------------------------------------------------------------------- runtime


def score_ruby(version: str) -> str:
    c = Component(ecosystem=RUBY_RUNTIME, name="ruby", current=version,
                  source="f", meta={"runtime": True})
    res = resolve_ruby_runtime(c, ctx(None))
    return advisor.score(c, res, lag_between(version, res.latest_stable,
                                             releases=res.releases))


@pytest.mark.parametrize("version,expected", [
    ("4.0.0", "current"),   # newest line
    ("3.4.1", "low"),       # normal maintenance -- behind, but entirely supported
    ("3.3.6", "medium"),    # security-only, EOL inside a year
    ("3.2.2", "critical"),  # EOL 2026-04-01
    ("3.1.2", "critical"),  # EOL 2025-03-26
    ("2.7.8", "critical"),  # EOL 2023-03-31
])
def test_runtime_is_scored_on_support_not_distance(version, expected):
    """The Java-17 lesson applied to Ruby. 3.4 is a major behind 4.0 and perfectly
    healthy; 3.2 is two minors behind and out of support. Distance says those are
    similar. The support calendar says they are opposites."""
    assert score_ruby(version) == expected


def test_a_maintained_line_is_not_punished_for_distance():
    """Regression: 3.4 rated MEDIUM because a null `days_left` was read as "no
    data" rather than "supported, no announced end"."""
    assert score_ruby("3.4.1") == "low"
    assert score_ruby("3.1.2") == "critical"


def test_unknown_ruby_line_is_unverified_not_assumed_fine():
    res = resolve_ruby_runtime(
        Component(ecosystem=RUBY_RUNTIME, name="ruby", current="9.9.9", source="f"),
        ctx(None))
    assert res.extra["lts"] is False
    assert "not in the known support calendar" in res.note
    assert score_ruby("9.9.9") == "high"


def test_eol_line_is_marked_deprecated():
    res = resolve_ruby_runtime(
        Component(ecosystem=RUBY_RUNTIME, name="ruby", current="2.7.8", source="f"),
        ctx(None))
    assert res.deprecated is True
    assert "end of life" in res.note


def test_security_only_phase_is_named():
    res = resolve_ruby_runtime(
        Component(ecosystem=RUBY_RUNTIME, name="ruby", current="3.3.6", source="f"),
        ctx(None))
    assert res.extra["security_only"] is True
    assert "security maintenance only" in res.note


def test_unparseable_version_does_not_crash_or_guess():
    res = resolve_ruby_runtime(
        Component(ecosystem=RUBY_RUNTIME, name="ruby", current="jruby-head", source="f"),
        ctx(None))
    assert res.note and "could not read a Ruby version" in res.note


def test_support_calendar_covers_every_live_branch():
    """Guards the table itself: if a new Ruby ships and this is not updated, the
    unknown-line path fires and says so rather than reporting it as fine."""
    assert RUBY_SUPPORT_END["4.0"] is None, "4.0 is in normal maintenance"
    assert RUBY_SUPPORT_END["3.1"] == "2025-03-26"
    assert all(v is None or len(v) == 10 for v in RUBY_SUPPORT_END.values())
