"""The transport layer: cache, retries, and the GitHub client.

This is the most-used module in the project and was the least tested -- 43% when
this file was written. That gap mattered more than the number suggests: every
bug found in the ADO and ACR work was in network-facing code, none was caught by
a test, and each surfaced only against a live service.

The property under test throughout is the one the whole codebase is organised
around: a call that *failed* must stay distinguishable from a call that
*succeeded and found nothing*. `GitHub.api` returning None on a 403 is not
defensive coding, it is the mechanism by which a throttled search stops being
reported as "this project has no issues".

Nothing here opens a socket.
"""

from __future__ import annotations

import json
import time
import urllib.error

import pytest

from driftscope import http as http_mod
from driftscope.http import Cache, Client, GitHub, HttpError, default_cache_dir


class FakeResponse:
    def __init__(self, payload, *, raw: bytes | None = None):
        self._raw = raw if raw is not None else json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


def expire(monkeypatch, cache):
    """Jump past the TTL.

    The baseline is captured first on purpose: a lambda that calls `time.time()`
    after patching `time.time` calls itself.
    """
    now = time.time()
    monkeypatch.setattr(http_mod.time, "time", lambda: now + cache.ttl + 10)


@pytest.fixture
def cache(tmp_path):
    return Cache(root=tmp_path / "c")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retry backoff is real seconds; tests assert the count, not the wall clock."""
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def clear_gh_globals(monkeypatch):
    monkeypatch.setattr(http_mod, "_GH_TOKEN", False)
    monkeypatch.setattr(http_mod, "_GH_AVAILABLE", None)


# ------------------------------------------------------------------------ cache


def test_a_miss_is_none_not_an_error(cache):
    assert cache.get("nothing-here") is None


def test_round_trip(cache):
    cache.put("k", {"a": 1})
    assert cache.get("k") == {"a": 1}
    assert cache.hits == 1


def test_an_expired_entry_is_a_miss_but_still_readable_when_asked(cache, monkeypatch):
    """`--offline` deliberately serves stale data; the TTL is about freshness,
    not about deletion. Losing the distinction would make offline useless."""
    cache.put("k", {"a": 1})
    expire(monkeypatch, cache)

    assert cache.get("k") is None
    assert cache.get("k", ignore_ttl=True) == {"a": 1}


def test_fresh_reports_liveness_without_reading_the_body(cache, monkeypatch):
    """`fresh()` exists because the throttle checks used `get()` for this, which
    parsed the whole body to discard it and double-counted the hit."""
    assert cache.fresh("k") is False
    cache.put("k", {"a": 1})
    assert cache.fresh("k") is True
    assert cache.hits == 0, "fresh() must not count as a hit"

    expire(monkeypatch, cache)
    assert cache.fresh("k") is False


def test_a_disabled_cache_stores_nothing_and_returns_nothing(tmp_path):
    c = Cache(root=tmp_path / "c", enabled=False)
    c.put("k", {"a": 1})

    assert c.get("k") is None
    assert c.fresh("k") is False


def test_corrupt_json_reads_as_a_miss_not_a_crash(cache):
    """A half-written or truncated entry must not take down a scan."""
    cache.put("k", {"a": 1})
    cache._path("k").write_text("{not json")
    assert cache.get("k") is None


def test_clear_empties_the_cache_and_leaves_it_usable(cache):
    cache.put("k", {"a": 1})
    cache.clear()

    assert cache.get("k") is None
    cache.put("k2", {"b": 2})
    assert cache.get("k2") == {"b": 2}


def test_keys_are_hashed_into_subdirectories(cache):
    """URLs contain slashes and are longer than a filename may be."""
    key = "https://registry.example/" + "x" * 400
    cache.put(key, {"ok": True})

    p = cache._path(key)
    assert p.parent.name == p.stem[:2]
    assert cache.get(key) == {"ok": True}


# --------------------------------------------------------------- Client.get_json


def test_a_cached_body_short_circuits_the_network(cache, monkeypatch):
    cache.put("https://x/a", {"cached": True})
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("went to the network despite a cache hit"),
    )
    assert Client(cache=cache).get_json("https://x/a") == {"cached": True}


def test_a_successful_call_is_cached(cache, monkeypatch):
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen", lambda *a, **k: FakeResponse({"v": 1})
    )
    c = Client(cache=cache)

    assert c.get_json("https://x/a") == {"v": 1}
    assert cache.get("https://x/a") == {"v": 1}
    assert c.requests_made == 1


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_4xx_is_terminal_and_not_retried(cache, monkeypatch, code):
    """Retrying a 404 only spends time. The status is carried on the exception so
    a caller can tell "does not exist" from "could not reach"."""
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise http_error(code)

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", boom)
    with pytest.raises(HttpError) as ei:
        Client(cache=cache).get_json("https://x/a")

    assert ei.value.status == code
    assert len(calls) == 1, "a terminal status must not be retried"


def test_a_500_is_retried_then_raises(cache, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise http_error(500)

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", boom)
    with pytest.raises(HttpError):
        Client(cache=cache, retries=2).get_json("https://x/a")

    assert len(calls) == 3, "initial attempt plus two retries"


def test_a_transient_failure_that_then_succeeds_returns_the_body(cache, monkeypatch):
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.URLError("connection reset")
        return FakeResponse({"v": "recovered"})

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", flaky)
    assert Client(cache=cache).get_json("https://x/a") == {"v": "recovered"}


def test_malformed_json_is_retried_like_a_transport_failure(cache, monkeypatch):
    """A truncated response is indistinguishable from a dropped connection, and
    is just as likely to succeed on a second attempt."""
    calls = []

    def bad(*a, **k):
        calls.append(1)
        return FakeResponse(None, raw=b"{ truncated")

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", bad)
    with pytest.raises(HttpError):
        Client(cache=cache, retries=1).get_json("https://x/a")

    assert len(calls) == 2


def test_offline_serves_stale_and_otherwise_refuses(cache, monkeypatch):
    """Offline must never reach the network, and must never invent an answer."""
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("opened a socket while offline"),
    )
    cache.put("https://x/have", {"stale": True})
    expire(monkeypatch, cache)
    c = Client(cache=cache, offline=True)

    assert c.get_json("https://x/have") == {"stale": True}
    with pytest.raises(HttpError, match="offline and not cached"):
        c.get_json("https://x/missing")


def test_a_cache_key_override_separates_entries_for_one_url(cache, monkeypatch):
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen", lambda *a, **k: FakeResponse({"v": 1})
    )
    Client(cache=cache).get_json("https://x/a", cache_key="custom")

    assert cache.get("custom") == {"v": 1}
    assert cache.get("https://x/a") is None


def test_custom_headers_reach_the_request(cache, monkeypatch):
    seen = {}

    def capture(req, timeout=None):
        seen.update(req.headers)
        return FakeResponse({"v": 1})

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", capture)
    Client(cache=cache).get_json("https://x/a", headers={"X-Custom": "yes"})

    assert seen.get("X-custom") == "yes"
    assert "User-agent" in seen


# ------------------------------------------------------------- Client.get_text


def test_get_text_keys_separately_from_json(cache, monkeypatch):
    """Same URL, different parse. Sharing a key would hand JSON to a text caller."""
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: FakeResponse(None, raw=b"plain body"),
    )
    assert Client(cache=cache).get_text("https://x/a") == "plain body"

    assert cache.get("https://x/a#text") == "plain body"
    assert cache.get("https://x/a") is None


def test_get_text_does_not_retry_and_reports_the_status(cache, monkeypatch):
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(404)),
    )
    with pytest.raises(HttpError) as ei:
        Client(cache=cache).get_text("https://x/a")
    assert ei.value.status == 404


def test_get_text_offline_serves_stale(cache, monkeypatch):
    cache.put("https://x/a#text", "stale text")
    expire(monkeypatch, cache)
    assert Client(cache=cache, offline=True).get_text("https://x/a") == "stale text"


# ------------------------------------------------------------------- gh helpers


def test_gh_token_is_read_once(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)

        class R:
            returncode = 0
            stdout = "ghp_abc\n"
        return R()

    monkeypatch.setattr(http_mod.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(http_mod.subprocess, "run", fake_run)

    assert http_mod.gh_token() == "ghp_abc"
    assert http_mod.gh_token() == "ghp_abc"
    assert len(calls) == 1, "the subprocess must be paid for once per process"


def test_no_gh_binary_means_no_token(monkeypatch):
    monkeypatch.setattr(http_mod.shutil, "which", lambda _: None)
    assert http_mod.gh_token() is None


def test_a_failing_gh_yields_none_rather_than_an_empty_token(monkeypatch):
    """An empty string would be sent as `Bearer `, which 401s and reads as a
    permissions problem rather than as "not logged in"."""
    class R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(http_mod.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(http_mod.subprocess, "run", lambda *a, **k: R())
    assert http_mod.gh_token() is None


def test_gh_availability_is_cached_and_needs_a_zero_exit(monkeypatch):
    calls = []

    class R:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(http_mod.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        http_mod.subprocess, "run", lambda *a, **k: (calls.append(1), R())[1]
    )
    assert http_mod.gh_available() is True
    assert http_mod.gh_available() is True
    assert len(calls) == 1


def test_gh_unavailable_when_the_subprocess_explodes(monkeypatch):
    monkeypatch.setattr(http_mod.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        http_mod.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no exec")),
    )
    assert http_mod.gh_available() is False


# ---------------------------------------------------------------- GitHub.api


@pytest.fixture
def gh(cache, monkeypatch):
    monkeypatch.setattr(http_mod, "gh_token", lambda: "TOKEN")
    return GitHub(cache=cache)


def test_api_sends_the_token_and_the_api_version(gh, monkeypatch):
    seen = {}

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        seen.update(req.headers)
        return FakeResponse([{"tag_name": "v1"}])

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", capture)
    assert gh.api("repos/o/r/releases") == [{"tag_name": "v1"}]

    assert seen["url"] == "https://api.github.com/repos/o/r/releases"
    assert seen["Authorization"] == "Bearer TOKEN"
    assert seen["X-github-api-version"] == "2022-11-28"
    assert gh.queries == 1


def test_an_absolute_url_is_passed_through(gh, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda req, timeout=None: (seen.update(url=req.full_url), FakeResponse({}))[1],
    )
    gh.api("https://api.github.com/rate_limit")
    assert seen["url"] == "https://api.github.com/rate_limit"


@pytest.mark.parametrize("code", [403, 429, 500])
def test_api_returns_none_on_failure_never_an_empty_result(gh, monkeypatch, code):
    """The load-bearing one. 403 here is the rate limit, and a caller that reads
    it as an empty list reports "no issues" for a project that was simply never
    asked -- the exact confident-wrong-answer this codebase exists to avoid."""
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(code)),
    )
    result = gh.api("search/issues?q=x")

    assert result is None
    assert result != [] and result != {}


def test_api_returns_none_on_a_transport_failure(gh, monkeypatch):
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("dns")),
    )
    assert gh.api("repos/o/r") is None


def test_a_failed_call_is_not_cached(gh, monkeypatch, cache):
    """Caching a None would turn one throttled minute into a whole TTL of
    pretending the project has no data."""
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(403)),
    )
    gh.api("repos/o/r")
    assert cache.get("gh:repos/o/r:False") is None


def test_a_disabled_client_serves_cache_only(cache, monkeypatch):
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("called the API while disabled"),
    )
    cache.put("gh:repos/o/r:False", {"stale": True})
    expire(monkeypatch, cache)

    # Stale is served deliberately: better an old answer, labelled, than none.
    assert GitHub(cache=cache, enabled=False).api("repos/o/r") == {"stale": True}
    assert GitHub(cache=cache, offline=True).api("repos/o/r") == {"stale": True}


def test_no_token_falls_back_to_the_gh_cli(cache, monkeypatch):
    monkeypatch.setattr(http_mod, "gh_token", lambda: None)
    monkeypatch.setattr(http_mod, "gh_available", lambda: True)

    class R:
        returncode = 0
        stdout = '[{"tag_name":"v9"}]'

    monkeypatch.setattr(http_mod.subprocess, "run", lambda *a, **k: R())
    g = GitHub(cache=cache)

    assert g.api("repos/o/r/releases") == [{"tag_name": "v9"}]
    assert cache.get("gh:repos/o/r/releases:False") == [{"tag_name": "v9"}]


def test_the_cli_fallback_gives_up_when_gh_is_unusable(cache, monkeypatch):
    monkeypatch.setattr(http_mod, "gh_token", lambda: None)
    monkeypatch.setattr(http_mod, "gh_available", lambda: False)
    assert GitHub(cache=cache).api("repos/o/r") is None


@pytest.mark.parametrize("stdout,rc", [("", 0), ("not json", 0), ("[]", 1)])
def test_the_cli_fallback_returns_none_on_junk(cache, monkeypatch, stdout, rc):
    monkeypatch.setattr(http_mod, "gh_token", lambda: None)
    monkeypatch.setattr(http_mod, "gh_available", lambda: True)

    class R:
        returncode = rc
        stdout = ""

    R.stdout = stdout
    monkeypatch.setattr(http_mod.subprocess, "run", lambda *a, **k: R())
    assert GitHub(cache=cache).api("repos/o/r") is None


def test_paginate_reaches_the_cli_and_keys_separately(cache, monkeypatch):
    seen = {}

    class R:
        returncode = 0
        stdout = "[]"

    monkeypatch.setattr(http_mod, "gh_token", lambda: None)
    monkeypatch.setattr(http_mod, "gh_available", lambda: True)
    monkeypatch.setattr(
        http_mod.subprocess, "run",
        lambda cmd, **k: (seen.update(cmd=cmd), R())[1],
    )
    GitHub(cache=cache).api("repos/o/r/issues", paginate=True)

    assert "--paginate" in seen["cmd"]
    assert cache.get("gh:repos/o/r/issues:True") == []


# ------------------------------------------------------------ search throttling


def test_search_issues_returns_the_items(gh, monkeypatch):
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: FakeResponse({"items": [{"number": 1}], "total_count": 1}),
    )
    assert gh.search_issues("repo:o/r is:issue") == [{"number": 1}]


def test_a_failed_search_yields_an_empty_list_not_a_crash(gh, monkeypatch):
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(403)),
    )
    assert gh.search_issues("q") == []


def test_a_failed_count_is_none_and_never_zero(gh, monkeypatch):
    """Regression, and the reason `search_count` exists in this shape: a
    throttled call returning 0 was read as "this project has no issues"."""
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(403)),
    )
    assert gh.search_count("q") is None


def test_a_real_zero_count_is_zero(gh, monkeypatch):
    """The other half: a successful search that found nothing must say 0, not
    None, or a genuinely quiet project reads as unmeasurable."""
    monkeypatch.setattr(
        http_mod.urllib.request, "urlopen",
        lambda *a, **k: FakeResponse({"total_count": 0, "items": []}),
    )
    assert gh.search_count("q") == 0


def test_searches_beyond_the_per_minute_budget_wait(cache, monkeypatch):
    """Velocity spends ~12 searches per repo, so a multi-repo run without pacing
    gets throttled and the results come back empty."""
    slept: list[float] = []
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: slept.append(s))
    g = GitHub(cache=cache)
    now = time.time()
    g._search_times = [now] * http_mod.SEARCH_PER_MINUTE

    g._reserve_search_slot()

    assert slept and slept[0] > 0
    assert g.throttled_waits == 1


def test_a_search_within_budget_does_not_wait(cache, monkeypatch):
    monkeypatch.setattr(
        http_mod.time, "sleep", lambda s: pytest.fail("waited with slots free")
    )
    g = GitHub(cache=cache)
    g._reserve_search_slot()

    assert g.throttled_waits == 0
    assert len(g._search_times) == 1


def test_stale_timestamps_fall_out_of_the_window(cache, monkeypatch):
    monkeypatch.setattr(
        http_mod.time, "sleep", lambda s: pytest.fail("waited on expired slots")
    )
    g = GitHub(cache=cache)
    g._search_times = [time.time() - 120] * http_mod.SEARCH_PER_MINUTE

    g._reserve_search_slot()
    assert len(g._search_times) == 1


def test_a_cached_search_skips_the_throttle_entirely(gh, monkeypatch, cache):
    """Waiting for a rate-limit slot to serve a cache hit would be pure delay."""
    from urllib.parse import quote

    path = f"search/issues?q={quote('q')}&per_page=1"
    cache.put(f"gh:{path}:False", {"total_count": 7})
    monkeypatch.setattr(
        gh, "_await_search_slot", lambda: pytest.fail("throttled a cache hit")
    )
    assert gh.search_count("q") == 7


# ------------------------------------------------------------------ cache dir


def test_cache_dir_prefers_the_explicit_override(monkeypatch):
    monkeypatch.setenv("DRIFTSCOPE_CACHE", "/tmp/explicit")
    assert default_cache_dir().as_posix() == "/tmp/explicit"


def test_cache_dir_honours_xdg(monkeypatch):
    monkeypatch.delenv("DRIFTSCOPE_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg")
    assert default_cache_dir().as_posix() == "/tmp/xdg/driftscope"


def test_cache_dir_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("DRIFTSCOPE_CACHE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert default_cache_dir().as_posix().endswith("/.cache/driftscope")
