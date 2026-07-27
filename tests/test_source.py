"""Scan-target parsing: which forge a URL addresses, and what to call the report.

No network and no cloning -- these are all pure string functions.
"""

from __future__ import annotations

import pytest

from driftscope.source import (
    AdoRef,
    looks_remote,
    normalize_remote,
    parse_ado_url,
    slug_for,
    strip_userinfo,
)

# The three shapes that turn up in the wild. The middle one is what the ADO
# "Clone" button copies to the clipboard, so it is the one users actually paste.
ADO_URLS = [
    ("https://dev.azure.com/contoso/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
    ("https://contoso@dev.azure.com/contoso/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
    ("https://contoso.visualstudio.com/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
]


@pytest.mark.parametrize("url,expected", ADO_URLS)
def test_parse_ado_url_shapes(url, expected):
    assert parse_ado_url(url) == expected


def test_ado_url_tolerates_git_suffix_and_trailing_slash():
    ref = AdoRef("contoso", "Platform", "payments")
    assert parse_ado_url("https://dev.azure.com/contoso/Platform/_git/payments.git") == ref
    assert parse_ado_url("https://dev.azure.com/contoso/Platform/_git/payments/") == ref


def test_ado_url_without_project_falls_back_to_repo_name():
    """ADO permits omitting the project when it matches the repo name, and the web
    UI resolves it the same way."""
    assert parse_ado_url("https://dev.azure.com/contoso/_git/payments") == AdoRef(
        "contoso", "payments", "payments"
    )


def test_legacy_host_tolerates_default_collection():
    assert parse_ado_url(
        "https://contoso.visualstudio.com/DefaultCollection/Platform/_git/payments"
    ) == AdoRef("contoso", "Platform", "payments")


@pytest.mark.parametrize("url", [
    "https://github.com/org/repo",
    "https://gitlab.com/org/repo",
    "git@github.com:org/repo.git",
    "org/repo",
    "/local/path",
    "",
    None,
])
def test_non_ado_urls_are_not_claimed(url):
    assert parse_ado_url(url) is None


def test_userinfo_is_stripped():
    """An ADO clone URL embeds `{org}@`, which reads as a credential in a report."""
    assert (strip_userinfo("https://contoso@dev.azure.com/contoso/P/_git/r")
            == "https://dev.azure.com/contoso/P/_git/r")
    # Anything without userinfo is returned untouched.
    assert (strip_userinfo("https://github.com/org/repo")
            == "https://github.com/org/repo")
    assert strip_userinfo(None) is None


# ------------------------------------------------------------------------ slug


def test_ado_slug_is_org_project_repo():
    """Regression: the generic remote regex reads an ADO path as `{project}/_git`,
    so every repo in a project produced the same `drift-Platform-_git` filename."""
    assert slug_for("https://dev.azure.com/contoso/Platform/_git/payments") == (
        "contoso-Platform-payments"
    )


def test_ado_slug_distinguishes_repos_in_one_project():
    a = slug_for("https://dev.azure.com/contoso/Platform/_git/payments")
    b = slug_for("https://dev.azure.com/contoso/Platform/_git/ledger")
    assert a != b


def test_github_slug_unchanged():
    assert slug_for("https://github.com/darrinthomascecil/driftscope-testapp") == (
        "darrinthomascecil-driftscope-testapp"
    )


def test_local_path_slug_unchanged():
    assert slug_for("/tmp/estate/") == "estate"


# ------------------------------------------------------------- normalize_remote


def test_bare_shorthand_still_means_github():
    """Changing this silently would break every existing invocation."""
    assert normalize_remote("org/repo") == "https://github.com/org/repo"


def test_ado_org_opts_shorthand_into_azure_devops():
    assert normalize_remote("Platform/payments", ado_org="contoso") == (
        "https://dev.azure.com/contoso/Platform/_git/payments"
    )


def test_ado_org_with_single_segment_uses_it_as_both():
    assert normalize_remote("payments", ado_org="contoso") == (
        "https://dev.azure.com/contoso/payments/_git/payments"
    )


def test_full_url_ignores_ado_org():
    url = "https://github.com/org/repo"
    assert normalize_remote(url, ado_org="contoso") == url


def test_ado_url_looks_remote():
    assert looks_remote("https://dev.azure.com/contoso/Platform/_git/payments")
