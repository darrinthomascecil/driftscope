"""Terraform, NuGet, Maven and the runtime support tables.

Two behaviours here are load-bearing and easy to regress.

The Terraform registry does not return dates at all, so they are grafted on from
GitHub releases -- which pages at 100 while azurerm has 400+ versions, so an old
pin falls off the end and silently loses its date. The direct tag lookup that
compensates is tested here.

The .NET and Java resolvers deliberately do *not* measure distance to the newest
release. A supported LTS is a legitimate production choice: Java 17 is eight
majors behind 25 and entirely fine. Support-end dates decide risk instead, and a
regression that reintroduced version distance would make the tool cry wolf on
every well-run estate.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from driftscope.http import HttpError
from driftscope.models import Component
from driftscope.resolvers.dotnet_jvm import (
    DOTNET_CURRENT_LTS,
    _relevant_pages,
    resolve_dotnet_tfm,
    resolve_jvm_toolchain,
    resolve_maven,
    resolve_nuget,
)
from driftscope.resolvers.terraform import (
    provider_repo,
    resolve_module,
    resolve_provider,
)

NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


class StubClient:
    def __init__(self, json_map=None, text_map=None, raises=None):
        self.json_map, self.text_map = json_map or {}, text_map or {}
        self.raises = raises or {}
        self.asked: list[str] = []

    def _lookup(self, url, table):
        self.asked.append(url)
        for frag, exc in self.raises.items():
            if frag in url:
                raise exc
        for frag, payload in table.items():
            if frag in url:
                return payload
        raise HttpError(f"no stub for {url}", 404)

    def get_json(self, url, **kw):
        return self._lookup(url, self.json_map)

    def get_text(self, url, **kw):
        return self._lookup(url, self.text_map)


class StubGitHub:
    """Routes by path substring so releases and tag lookups differ."""

    def __init__(self, routes=None, enabled=True):
        self.routes = routes or {}
        self.enabled = enabled
        self.asked: list[str] = []

    def api(self, path, **kw):
        self.asked.append(path)
        for frag, body in self.routes.items():
            if frag in path:
                return body
        return None


class Ctx:
    def __init__(self, client=None, github=None, now=NOW):
        self.client = client or StubClient()
        self.github = github or StubGitHub()
        self.now = now
        self.allow_az = False


def comp(name, current=None, ecosystem="terraform-provider", **meta):
    return Component(
        ecosystem=ecosystem, name=name, current=current, source="main.tf", meta=meta
    )


def rel(res, version):
    return next((r for r in res.releases if r.version == version), None)


# ------------------------------------------------------------ provider naming


@pytest.mark.parametrize("name,repo", [
    ("hashicorp/azurerm", "hashicorp/terraform-provider-azurerm"),
    ("integrations/github", "integrations/terraform-provider-github"),
    ("azurerm", None),
])
def test_provider_repo_inference(name, repo):
    assert provider_repo(name) == repo


def test_an_unqualified_provider_is_an_error_not_a_guess():
    r = resolve_provider(comp("azurerm", "3.0.0"), Ctx())
    assert r.error and r.latest_stable is None


# ------------------------------------------------------------------- providers

PROVIDER_BODY = {"versions": ["3.0.0", "3.50.0", "4.0.0", "4.1.0-beta1"]}
RELEASES_100 = [
    {"tag_name": "v4.0.0", "published_at": "2026-07-14T00:00:00Z"},
    {"tag_name": "v3.50.0", "published_at": "2025-01-10T00:00:00Z"},
]


def test_a_provider_gets_versions_from_the_registry_and_dates_from_github():
    ctx = Ctx(
        StubClient({"/providers/hashicorp/azurerm": PROVIDER_BODY}),
        StubGitHub({"/releases?per_page=100": RELEASES_100}),
    )
    r = resolve_provider(comp("hashicorp/azurerm", "3.50.0"), ctx)

    assert r.latest_stable == "4.0.0"
    assert rel(r, "4.0.0").published.date().isoformat() == "2026-07-14"
    assert r.repo == "hashicorp/terraform-provider-azurerm"


def test_a_pin_older_than_the_releases_page_is_dated_directly():
    """Regression: the releases listing caps at 100 and azurerm has 400+
    versions, so an old pin fell off the end, lost its date, and silently killed
    days_behind -- the finding stayed but its age vanished."""
    ctx = Ctx(
        StubClient({"/providers/hashicorp/azurerm": PROVIDER_BODY}),
        StubGitHub({
            "/releases?per_page=100": RELEASES_100,
            "/releases/tags/v3.0.0": {"published_at": "2022-03-24T00:00:00Z"},
        }),
    )
    r = resolve_provider(comp("hashicorp/azurerm", "3.0.0"), ctx)

    assert rel(r, "3.0.0").published.date().isoformat() == "2022-03-24"


def test_the_tag_lookup_tries_both_v_prefixed_and_bare():
    ctx = Ctx(
        StubClient({"/providers/x/y": {"versions": ["1.0.0"]}}),
        StubGitHub({"/releases/tags/1.0.0": {"published_at": "2024-01-01T00:00:00Z"}}),
    )
    r = resolve_provider(comp("x/y", "1.0.0"), ctx)

    assert rel(r, "1.0.0").published is not None


def test_a_pin_that_cannot_be_dated_stays_undated_rather_than_borrowing():
    ctx = Ctx(
        StubClient({"/providers/x/y": {"versions": ["1.0.0", "2.0.0"]}}),
        StubGitHub({"/releases?per_page=100": [
            {"tag_name": "v2.0.0", "published_at": "2026-01-01T00:00:00Z"}]}),
    )
    r = resolve_provider(comp("x/y", "1.0.0"), ctx)

    assert rel(r, "1.0.0").published is None
    assert rel(r, "2.0.0").published is not None


def test_provider_prereleases_are_flagged_and_excluded():
    ctx = Ctx(StubClient({"/providers/hashicorp/azurerm": PROVIDER_BODY}))
    r = resolve_provider(comp("hashicorp/azurerm", "3.0.0"), ctx)

    assert rel(r, "4.1.0-beta1").prerelease is True
    assert r.latest_stable == "4.0.0"


def test_no_dates_anywhere_is_said_out_loud():
    """A version list with no dates is a partial answer, and the report should
    not leave the reader guessing why every age is blank."""
    ctx = Ctx(StubClient({"/providers/x/y": {"versions": ["1.0.0"]}}))
    r = resolve_provider(comp("x/y", "1.0.0"), ctx)

    assert r.note == "release dates unavailable"


def test_an_empty_version_list_is_an_error():
    ctx = Ctx(StubClient({"/providers/x/y": {"versions": []}}))
    assert resolve_provider(comp("x/y", "1.0.0"), ctx).error


def test_a_registry_failure_keeps_the_inferred_repo():
    ctx = Ctx(StubClient(raises={"/providers/": HttpError("HTTP 503", 503)}))
    r = resolve_provider(comp("hashicorp/azurerm", "3.0.0"), ctx)

    assert r.error
    assert r.repo == "hashicorp/terraform-provider-azurerm"


# ---------------------------------------------------------------------- core


def test_terraform_core_comes_from_github_releases():
    ctx = Ctx(github=StubGitHub({"hashicorp/terraform/releases": [
        {"tag_name": "v1.9.0", "published_at": "2026-06-01T00:00:00Z"},
        {"tag_name": "v1.10.0-beta1", "published_at": "2026-07-01T00:00:00Z",
         "prerelease": True},
    ]}))
    r = resolve_provider(comp("hashicorp/terraform", "1.5.0", core=True), ctx)

    assert r.latest_stable == "1.9.0"
    assert r.repo == "hashicorp/terraform"


def test_unreadable_terraform_releases_is_an_error():
    r = resolve_provider(comp("hashicorp/terraform", "1.5.0", core=True),
                         Ctx(github=StubGitHub()))
    assert r.error


# -------------------------------------------------------------------- modules


def test_a_non_registry_module_is_noted_not_errored():
    """A git or local module source has no version feed. That is a fact about
    the source, not a failure to look it up."""
    r = resolve_module(comp("../modules/net", ecosystem="terraform-module"), Ctx())

    assert r.error is None
    assert "non-registry" in r.note


def test_a_module_resolves_and_infers_its_repo_from_source():
    ctx = Ctx(StubClient({"/modules/Azure/network/azurerm": {
        "versions": ["5.0.0", "6.0.0"],
        "source": "https://github.com/Azure/terraform-azurerm-network.git",
    }}))
    r = resolve_module(comp("Azure/network/azurerm", "5.0.0",
                            ecosystem="terraform-module", registry=True), ctx)

    assert r.latest_stable == "6.0.0"
    assert r.repo == "Azure/terraform-azurerm-network"


def test_module_versions_may_arrive_as_objects():
    ctx = Ctx(StubClient({"/modules/a/b/c": {
        "versions": [{"version": "1.0.0"}, {"version": "2.0.0"}]}}))
    r = resolve_module(comp("a/b/c", ecosystem="terraform-module", registry=True), ctx)

    assert r.latest_stable == "2.0.0"


def test_a_module_with_only_a_top_level_version_still_resolves():
    ctx = Ctx(StubClient({"/modules/a/b/c": {"version": "3.1.0"}}))
    r = resolve_module(comp("a/b/c", ecosystem="terraform-module", registry=True), ctx)

    assert r.latest_stable == "3.1.0"


def test_a_malformed_module_address_is_an_error():
    r = resolve_module(comp("a/b", ecosystem="terraform-module", registry=True), Ctx())
    assert r.error


def test_a_module_with_no_versions_is_an_error():
    ctx = Ctx(StubClient({"/modules/a/b/c": {"versions": []}}))
    r = resolve_module(comp("a/b/c", ecosystem="terraform-module", registry=True), ctx)
    assert r.error


# ------------------------------------------------------------------- NuGet

FLAT = {"versions": ["12.0.1", "13.0.3", "13.0.4", "14.0.0-preview1"]}
REG = {"items": [{"items": [
    {"catalogEntry": {"version": "12.0.1", "published": "2018-11-27T00:00:00Z",
                      "projectUrl": "https://www.newtonsoft.com/json"}},
    {"catalogEntry": {"version": "13.0.4", "published": "2025-09-16T00:00:00Z"}},
    {"catalogEntry": {"version": "13.0.3", "published": "1900-01-01T00:00:00Z"}},
]}]}


def test_nuget_merges_the_flat_list_with_registration_dates():
    ctx = Ctx(StubClient({"flatcontainer": FLAT, "registration5": REG},
                         raises={".nuspec": HttpError("404", 404)}))
    r = resolve_nuget(comp("Newtonsoft.Json", "12.0.1", ecosystem="nuget"), ctx)

    assert rel(r, "12.0.1").published.date().isoformat() == "2018-11-27"
    assert r.latest_stable == "13.0.4"


def test_the_1900_sentinel_marks_a_package_unlisted():
    """NuGet dates unlisted packages to year 1900. Read literally that is a
    125-year-old package; read correctly it means "withdrawn"."""
    ctx = Ctx(StubClient({"flatcontainer": FLAT, "registration5": REG},
                         raises={".nuspec": HttpError("404", 404)}))
    r = resolve_nuget(comp("Newtonsoft.Json", "12.0.1", ecosystem="nuget"), ctx)

    assert rel(r, "13.0.3").yanked is True
    assert r.latest_stable != "13.0.3"


def test_nuget_prereleases_are_flagged():
    ctx = Ctx(StubClient({"flatcontainer": FLAT, "registration5": REG},
                         raises={".nuspec": HttpError("404", 404)}))
    r = resolve_nuget(comp("x", ecosystem="nuget"), ctx)
    assert rel(r, "14.0.0-preview1").prerelease is True


def test_the_nuspec_repository_beats_the_marketing_homepage():
    """projectUrl is newtonsoft.com or serilog.net far more often than a repo,
    which left every NuGet package without an upstream and therefore without an
    issue delta."""
    nuspec = '<package><metadata><repository type="git" ' \
             'url="https://github.com/JamesNK/Newtonsoft.Json.git" /></metadata></package>'
    ctx = Ctx(StubClient({"flatcontainer": FLAT, "registration5": REG},
                         text_map={".nuspec": nuspec}))
    r = resolve_nuget(comp("Newtonsoft.Json", "12.0.1", ecosystem="nuget"), ctx)

    assert r.repo == "JamesNK/Newtonsoft.Json"


def test_a_deprecation_notice_is_surfaced():
    reg = {"items": [{"items": [
        {"catalogEntry": {"version": "1.0.0", "published": "2020-01-01T00:00:00Z",
                          "deprecation": {"message": "use X"}}}]}]}
    ctx = Ctx(StubClient({"flatcontainer": {"versions": ["1.0.0"]},
                          "registration5": reg},
                         raises={".nuspec": HttpError("404", 404)}))
    r = resolve_nuget(comp("x", ecosystem="nuget"), ctx)

    assert r.deprecated is True and "deprecation" in r.note


def test_versions_survive_when_the_registration_index_fails():
    """Dates are a bonus; the version list is the answer. Losing the former must
    not lose the latter."""
    ctx = Ctx(StubClient({"flatcontainer": FLAT},
                         raises={"registration5": HttpError("HTTP 500", 500),
                                 ".nuspec": HttpError("404", 404)}))
    r = resolve_nuget(comp("x", "12.0.1", ecosystem="nuget"), ctx)

    assert r.latest_stable == "13.0.4"
    assert all(x.published is None for x in r.releases)


def test_an_empty_nuget_version_list_is_an_error():
    ctx = Ctx(StubClient({"flatcontainer": {"versions": []}}))
    assert resolve_nuget(comp("x", ecosystem="nuget"), ctx).error


def test_relevant_pages_narrows_a_long_registration_index():
    """A package with 40 registration pages should not cost 40 requests."""
    pages = [{"lower": f"{i}.0.0", "upper": f"{i}.9.9"} for i in range(12)]
    assert len(_relevant_pages(pages, "3.0.0")) < len(pages)


# ------------------------------------------------- .NET TFM support windows


def test_an_out_of_support_tfm_is_deprecated_and_says_when_it_ended():
    r = resolve_dotnet_tfm(comp("net6.0", "6.0", ecosystem="dotnet-tfm"), Ctx())

    assert r.deprecated is True
    assert "2024-11-12" in r.note and "no security patches" in r.note
    assert r.latest_stable == DOTNET_CURRENT_LTS


def test_a_supported_tfm_reports_days_remaining_rather_than_a_warning():
    r = resolve_dotnet_tfm(comp("net8.0", "8.0", ecosystem="dotnet-tfm"), Ctx())

    assert r.deprecated is False
    assert "supported until 2026-11-10" in r.note
    assert r.extra["support_days_left"] > 0


def test_netstandard_has_no_upgrade_target():
    """It is a compatibility surface, not a runtime. Telling someone to move
    netstandard2.0 to .NET 10 is a category error."""
    r = resolve_dotnet_tfm(
        comp("netstandard2.0", "2.0", ecosystem="dotnet-tfm"), Ctx())

    assert r.latest_stable == "2.0"
    assert "compatibility surface" in r.note


def test_a_framework_with_no_published_end_date_is_not_called_expired():
    """.NET Framework 4.8 ships with the OS and has no end date. `None` means
    "no date", and must not be read as "already ended"."""
    r = resolve_dotnet_tfm(comp("net48", "4.8", ecosystem="dotnet-tfm"), Ctx())

    assert r.deprecated is False
    assert r.extra["support_end"] is None


def test_an_unrecognised_tfm_is_noted_not_guessed():
    r = resolve_dotnet_tfm(comp("netcoreapp1.0", None, ecosystem="dotnet-tfm"), Ctx())
    assert r.latest_stable is None and "unrecognised" in r.note


# ------------------------------------------------------ Java / JVM toolchain


@pytest.mark.parametrize("current", ["17", "17.0.9", "1.8"])
def test_a_supported_lts_java_is_not_treated_as_dangerously_behind(current):
    """Java 17 is eight majors behind 25 and entirely fine. If this ever starts
    measuring version distance the tool cries wolf on every well-run estate."""
    r = resolve_jvm_toolchain(comp("java", current, ecosystem="jvm-toolchain"), Ctx())

    assert r.deprecated is False
    assert r.extra.get("support_days_left", 1) > 0


def test_gradle_resolves_from_its_releases():
    ctx = Ctx(github=StubGitHub({"gradle/gradle/releases": [
        {"tag_name": "v8.10", "published_at": "2026-01-01T00:00:00Z"},
        {"tag_name": "v9.0-rc-1", "published_at": "2026-06-01T00:00:00Z"},
    ]}))
    r = resolve_jvm_toolchain(comp("gradle", "8.0", ecosystem="jvm-toolchain"), ctx)

    assert r.latest_stable == "8.10"
    assert r.repo == "gradle/gradle"


def test_unreadable_gradle_releases_is_an_error():
    r = resolve_jvm_toolchain(comp("gradle", "8.0", ecosystem="jvm-toolchain"),
                              Ctx(github=StubGitHub()))
    assert r.error


# --------------------------------------------------------------------- Maven

def maven_body(*docs):
    return {"response": {"docs": list(docs)}}


def doc(v, ts=None):
    d = {"v": v}
    if ts is not None:
        d["timestamp"] = ts
    return d


def test_maven_reads_versions_and_millisecond_timestamps():
    """Maven Central dates in epoch *milliseconds*; treating them as seconds
    would put every release in 1970."""
    ctx = Ctx(StubClient({"solrsearch": maven_body(
        doc("2.15.2", 1685_000_000_000), doc("2.19.0", 1745_000_000_000))}))
    r = resolve_maven(
        comp("com.fasterxml.jackson.core:jackson-databind", "2.15.2",
             ecosystem="maven"), ctx)

    assert r.latest_stable == "2.19.0"
    assert rel(r, "2.15.2").published.year == 2023


@pytest.mark.parametrize("v", [
    "3.0.0-alpha1", "3.0.0-beta2", "1.0-rc1", "2.0-M3", "1.0-SNAPSHOT",
])
def test_maven_prerelease_shapes_are_recognised(v):
    ctx = Ctx(StubClient({"solrsearch": maven_body(doc("1.0.0"), doc(v))}))
    r = resolve_maven(comp("g:a", ecosystem="maven"), ctx)

    assert rel(r, v).prerelease is True
    assert r.latest_stable == "1.0.0"


def test_a_zero_timestamp_is_no_date_rather_than_the_epoch():
    ctx = Ctx(StubClient({"solrsearch": maven_body(doc("1.0.0", 0))}))
    r = resolve_maven(comp("g:a", ecosystem="maven"), ctx)
    assert rel(r, "1.0.0").published is None


def test_a_malformed_coordinate_is_an_error():
    assert resolve_maven(comp("no-colon", ecosystem="maven"), Ctx()).error


def test_an_artifact_absent_from_central_is_noted_not_errored():
    """Absent is a fact about the artifact; error implies we failed to look."""
    ctx = Ctx(StubClient({"solrsearch": maven_body()}))
    r = resolve_maven(comp("g:a", ecosystem="maven"), ctx)

    assert r.error is None and "not found" in r.note


def test_a_truncated_maven_listing_says_so():
    ctx = Ctx(StubClient({"solrsearch": maven_body(*(doc(f"1.0.{i}") for i in range(200)))}))
    r = resolve_maven(comp("g:a", ecosystem="maven"), ctx)
    assert "truncated" in r.note


def test_an_inferred_maven_repo_is_confirmed_before_it_is_trusted():
    """The repo is guessed from the group prefix. Attributing issues to a wrong
    repo yields a confident "no activity" verdict about a busy project."""
    ctx = Ctx(
        StubClient({"solrsearch": maven_body(doc("3.9"))}),
        StubGitHub({}),   # every repo lookup returns None -> unconfirmed
    )
    r = resolve_maven(comp("org.apache.commons:commons-lang3", "3.9",
                           ecosystem="maven"), ctx)

    assert r.repo is None, "an unconfirmed repo must be dropped, not reported"


def test_a_confirmed_maven_repo_is_kept():
    ctx = Ctx(
        StubClient({"solrsearch": maven_body(doc("3.9"))}),
        StubGitHub({"repos/": {"full_name": "apache/commons-lang"}}),
    )
    r = resolve_maven(comp("org.apache.commons:commons-lang3", "3.9",
                           ecosystem="maven"), ctx)

    assert r.repo == "apache/commons-lang"


def test_maven_http_failure_is_an_error():
    ctx = Ctx(StubClient(raises={"solrsearch": HttpError("HTTP 503", 503)}))
    assert resolve_maven(comp("g:a", ecosystem="maven"), ctx).error
