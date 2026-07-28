"""Registry response parsing for PyPI, npm, Go and the Node runtime.

The contract test next door proves every resolver returns a well-formed
`Resolution`; it says nothing about whether the numbers in it are right. This
file covers the bodies -- the part that turns a registry's JSON into a version
and a date, and the part that decides what to do when the payload is not the
shape the docs promise.

The recurring property: a resolver may return "I could not tell", but it must
never return a confident value it did not derive from the response.
"""

from __future__ import annotations

import pytest

from driftscope.http import HttpError
from driftscope.models import Component
from driftscope.resolvers.packages import (
    _go_escape,
    _go_repo,
    resolve_go,
    resolve_npm,
    resolve_pypi,
)


class StubClient:
    """Serves canned payloads by URL substring; records what was asked for."""

    def __init__(self, json_map=None, text_map=None, raises=None):
        self.json_map = json_map or {}
        self.text_map = text_map or {}
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
    def __init__(self, body=None):
        self.body = body

    def api(self, path, **kw):
        return self.body


class Ctx:
    def __init__(self, client=None, github=None):
        self.client = client or StubClient()
        self.github = github or StubGitHub()
        self.allow_az = False


def comp(name, current=None, ecosystem="pypi", **meta):
    return Component(
        ecosystem=ecosystem, name=name, current=current,
        source="manifest", meta=meta,
    )


def rel(res, version):
    return next((r for r in res.releases if r.version == version), None)


# ------------------------------------------------------------------------ PyPI

PYPI_BODY = {
    "info": {
        "version": "2.31.0",
        "home_page": "https://requests.readthedocs.io",
        "package_url": "https://pypi.org/project/requests/",
        "project_urls": {"Source": "https://github.com/psf/requests"},
        "classifiers": ["Development Status :: 5 - Production/Stable"],
    },
    "releases": {
        "2.25.0": [{"upload_time_iso_8601": "2020-11-11T10:00:00Z", "yanked": False}],
        "2.31.0": [{"upload_time_iso_8601": "2023-05-22T10:00:00Z", "yanked": False}],
        "2.30.0": [{"upload_time_iso_8601": "2023-05-03T10:00:00Z", "yanked": True}],
        "3.0.0rc1": [{"upload_time_iso_8601": "2024-01-01T10:00:00Z"}],
    },
}


def test_pypi_reads_versions_dates_and_repo():
    r = resolve_pypi(comp("requests", "2.25.0"), Ctx(StubClient({"pypi.org": PYPI_BODY})))

    assert r.latest_stable == "2.31.0"
    assert r.repo == "psf/requests"
    assert rel(r, "2.25.0").published.date().isoformat() == "2020-11-11"


def test_pypi_marks_prereleases_and_excludes_them_from_latest():
    r = resolve_pypi(comp("requests"), Ctx(StubClient({"pypi.org": PYPI_BODY})))

    assert rel(r, "3.0.0rc1").prerelease is True
    assert r.latest_stable == "2.31.0"


def test_a_fully_yanked_release_is_marked_and_cannot_be_the_target():
    """Yanked means "do not install this"; recommending an upgrade to it would
    be worse than recommending nothing."""
    r = resolve_pypi(comp("requests"), Ctx(StubClient({"pypi.org": PYPI_BODY})))

    assert rel(r, "2.30.0").yanked is True
    assert r.latest_stable != "2.30.0"


def test_a_partially_yanked_release_is_still_usable():
    """`yanked` is per-file. One bad wheel does not retire the version."""
    body = {"info": {"version": "1.0"}, "releases": {
        "1.0": [{"upload_time_iso_8601": "2024-01-01T00:00:00Z", "yanked": True},
                {"upload_time_iso_8601": "2024-01-01T00:00:00Z", "yanked": False}]}}
    r = resolve_pypi(comp("x"), Ctx(StubClient({"pypi.org": body})))

    assert rel(r, "1.0").yanked is False


def test_a_release_with_no_files_has_no_date_rather_than_a_wrong_one():
    body = {"info": {"version": "1.0"}, "releases": {"1.0": []}}
    r = resolve_pypi(comp("x"), Ctx(StubClient({"pypi.org": body})))

    assert rel(r, "1.0").published is None


def test_an_inactive_classifier_becomes_a_note():
    body = {
        "info": {"version": "1.0",
                 "classifiers": ["Development Status :: 7 - Inactive"]},
        "releases": {"1.0": []},
    }
    r = resolve_pypi(comp("x"), Ctx(StubClient({"pypi.org": body})))
    assert "Inactive" in r.note


def test_an_unparseable_declared_version_is_dropped_not_reported():
    """`info.version` is free text. Emitting something no comparator understands
    puts a value in the report that nothing downstream can act on -- reporting
    no answer is the honest degradation."""
    body = {"info": {"version": "the-latest-one"}, "releases": {}}
    r = resolve_pypi(comp("x"), Ctx(StubClient({"pypi.org": body})))

    assert r.latest_stable is None


def test_a_declared_version_rescues_an_empty_release_list():
    body = {"info": {"version": "4.2.0"}, "releases": {}}
    r = resolve_pypi(comp("x"), Ctx(StubClient({"pypi.org": body})))

    assert r.latest_stable == "4.2.0"


def test_an_http_failure_becomes_an_error_not_an_empty_answer():
    ctx = Ctx(StubClient(raises={"pypi.org": HttpError("HTTP 503", 503)}))
    r = resolve_pypi(comp("x"), ctx)

    assert r.error and r.latest_stable is None


# ------------------------------------------------------------------------- npm

NPM_BODY = {
    "dist-tags": {"latest": "4.18.2"},
    "time": {"4.17.1": "2019-05-26T10:00:00Z", "4.18.2": "2022-10-08T10:00:00Z"},
    "versions": {
        "4.17.1": {},
        "4.18.2": {},
        "5.0.0-beta.1": {},
        "4.16.0": {"deprecated": "security issue"},
    },
    "repository": {"url": "git+https://github.com/expressjs/express.git"},
    "homepage": "https://expressjs.com",
}


def test_npm_prefers_the_dist_tag_over_sorting():
    """`dist-tags.latest` is what `npm install` actually gives you, which may
    deliberately lag the highest version number."""
    r = resolve_npm(comp("express", "4.17.1", ecosystem="npm"),
                    Ctx(StubClient({"registry.npmjs.org": NPM_BODY})))

    assert r.latest_stable == "4.18.2"
    assert r.repo == "expressjs/express"
    assert rel(r, "4.17.1").published.date().isoformat() == "2019-05-26"


def test_npm_marks_deprecated_versions_as_yanked():
    r = resolve_npm(comp("express", ecosystem="npm"),
                    Ctx(StubClient({"registry.npmjs.org": NPM_BODY})))
    assert rel(r, "4.16.0").yanked is True


def test_npm_detects_prerelease_tags():
    r = resolve_npm(comp("express", ecosystem="npm"),
                    Ctx(StubClient({"registry.npmjs.org": NPM_BODY})))
    assert rel(r, "5.0.0-beta.1").prerelease is True


def test_a_package_level_deprecation_is_carried_through():
    body = {"deprecated": "no longer maintained", "versions": {"1.0.0": {}},
            "dist-tags": {"latest": "1.0.0"}, "time": {}}
    r = resolve_npm(comp("x", ecosystem="npm"),
                    Ctx(StubClient({"registry.npmjs.org": body})))

    assert r.deprecated is True
    assert "deprecated" in r.note


def test_a_scoped_package_name_survives_url_encoding():
    ctx = Ctx(StubClient({"registry.npmjs.org": NPM_BODY}))
    resolve_npm(comp("@scope/pkg", ecosystem="npm"), ctx)

    assert "@scope/pkg" in ctx.client.asked[0], ctx.client.asked


def test_npm_http_failure_is_an_error():
    ctx = Ctx(StubClient(raises={"npmjs": HttpError("HTTP 500", 500)}))
    assert resolve_npm(comp("x", ecosystem="npm"), ctx).error


# ------------------------------------------------------------- Node.js runtime

NODE_INDEX = [
    {"version": "v22.3.0", "date": "2024-06-11", "lts": False},
    {"version": "v20.15.0", "date": "2024-06-20", "lts": "Iron"},
    {"version": "v18.20.3", "date": "2024-05-21", "lts": "Hydrogen"},
    {"version": "", "date": "2024-01-01"},
]


def test_the_node_runtime_targets_lts_not_the_highest_version():
    """Recommending a non-LTS Node to a production service is the wrong advice
    even though the number is bigger."""
    ctx = Ctx(StubClient({"nodejs.org": NODE_INDEX}))
    r = resolve_npm(comp("node", "18.20.3", ecosystem="npm", runtime=True), ctx)

    assert r.latest_stable == "20.15.0"
    assert "LTS" in r.note
    assert r.repo == "nodejs/node"


def test_a_malformed_node_index_is_an_error_not_a_guess():
    ctx = Ctx(StubClient({"nodejs.org": {"unexpected": "shape"}}))
    r = resolve_npm(comp("node", ecosystem="npm", runtime=True), ctx)

    assert r.error and r.latest_stable is None


def test_node_entries_without_a_version_are_skipped():
    ctx = Ctx(StubClient({"nodejs.org": NODE_INDEX}))
    r = resolve_npm(comp("node", ecosystem="npm", runtime=True), ctx)

    assert all(x.version for x in r.releases)
    assert len(r.releases) == 3


# -------------------------------------------------------------------------- Go


def test_go_module_paths_escape_uppercase():
    """Uppercase is !-escaped in proxy paths; without it the proxy 404s."""
    assert _go_escape("github.com/BurntSushi/toml") == "github.com/!burnt!sushi/toml"


@pytest.mark.parametrize("mod,repo", [
    ("github.com/gorilla/mux", "gorilla/mux"),
    ("github.com/gorilla/mux/v2", "gorilla/mux"),
    ("golang.org/x/net", None),
    ("github.com", None),
])
def test_go_repo_inference(mod, repo):
    assert _go_repo(mod) == repo


def test_go_reads_the_version_list_and_dates_the_latest():
    ctx = Ctx(StubClient(
        json_map={"@latest": {"Version": "v1.8.1", "Time": "2023-10-18T10:00:00Z"}},
        text_map={"@v/list": "v1.7.0\nv1.8.0\nv1.8.1\n"},
    ))
    r = resolve_go(comp("github.com/gorilla/mux", "v1.7.0", ecosystem="go-module"), ctx)

    assert r.latest_stable == "v1.8.1"
    assert rel(r, "v1.8.1").published.date().isoformat() == "2023-10-18"
    assert r.repo == "gorilla/mux"


def test_go_fetches_the_date_of_the_pinned_version_specifically():
    """The proxy dates only @latest, so "how old is what we ship" needs one
    extra call -- but only one, not one per version."""
    ctx = Ctx(StubClient(
        json_map={
            "@latest": {"Version": "v1.8.1", "Time": "2023-10-18T10:00:00Z"},
            "@v/v1.7.0.info": {"Time": "2019-01-25T10:00:00Z"},
        },
        text_map={"@v/list": "v1.7.0\nv1.8.1\n"},
    ))
    r = resolve_go(comp("github.com/gorilla/mux", "v1.7.0", ecosystem="go-module"), ctx)

    assert rel(r, "v1.7.0").published.date().isoformat() == "2019-01-25"
    assert len([u for u in ctx.client.asked if ".info" in u]) == 1


def test_a_current_version_without_a_v_prefix_still_matches():
    ctx = Ctx(StubClient(
        json_map={"@latest": {"Version": "v1.8.1"},
                  "@v/v1.7.0.info": {"Time": "2019-01-25T10:00:00Z"}},
        text_map={"@v/list": "v1.7.0\nv1.8.1\n"},
    ))
    r = resolve_go(comp("github.com/gorilla/mux", "1.7.0", ecosystem="go-module"), ctx)

    assert rel(r, "v1.7.0").published is not None


def test_a_missing_info_endpoint_leaves_the_date_absent():
    ctx = Ctx(
        StubClient(
            json_map={"@latest": {"Version": "v1.8.1"}},
            text_map={"@v/list": "v1.7.0\nv1.8.1\n"},
            raises={".info": HttpError("HTTP 404", 404)},
        )
    )
    r = resolve_go(comp("github.com/gorilla/mux", "v1.7.0", ecosystem="go-module"), ctx)

    assert rel(r, "v1.7.0").published is None
    assert r.latest_stable == "v1.8.1"


def test_incompatible_versions_are_excluded_from_latest():
    """`+incompatible` marks a module that never adopted modules; treating it as
    newest would recommend an upgrade backwards."""
    ctx = Ctx(StubClient(
        json_map={"@latest": {"Version": "v1.8.1"}},
        text_map={"@v/list": "v1.8.1\nv2.0.0+incompatible\n"},
    ))
    r = resolve_go(comp("github.com/x/y", ecosystem="go-module"), ctx)

    assert r.latest_stable == "v1.8.1"


def test_a_latest_missing_from_the_list_is_still_included():
    ctx = Ctx(StubClient(
        json_map={"@latest": {"Version": "v1.9.0"}},
        text_map={"@v/list": "v1.8.1\n"},
    ))
    r = resolve_go(comp("github.com/x/y", ecosystem="go-module"), ctx)

    assert rel(r, "v1.9.0") is not None


def test_an_unreadable_version_list_still_yields_latest():
    ctx = Ctx(StubClient(
        json_map={"@latest": {"Version": "v1.9.0"}},
        raises={"@v/list": HttpError("HTTP 500", 500)},
    ))
    r = resolve_go(comp("github.com/x/y", ecosystem="go-module"), ctx)

    assert r.latest_stable == "v1.9.0"


def test_go_proxy_failure_is_an_error_but_keeps_the_repo():
    ctx = Ctx(StubClient(raises={"@latest": HttpError("HTTP 500", 500)}))
    r = resolve_go(comp("github.com/gorilla/mux", ecosystem="go-module"), ctx)

    assert r.error
    assert r.repo == "gorilla/mux", "the repo is inferred from the name, not the call"


def test_the_go_toolchain_reads_tags_from_the_go_repo():
    ctx = Ctx(github=StubGitHub([
        {"name": "go1.22.0"}, {"name": "go1.21.5"}, {"name": "weekly.2009"},
    ]))
    r = resolve_go(comp("golang.org/toolchain", "1.21.5", ecosystem="go-module"), ctx)

    assert r.latest_stable == "1.22.0"
    assert {x.version for x in r.releases} == {"1.22.0", "1.21.5"}


def test_an_unreadable_toolchain_tag_list_is_an_error():
    r = resolve_go(comp("golang.org/toolchain", ecosystem="go-module"),
                   Ctx(github=StubGitHub(None)))
    assert r.error and r.latest_stable is None
