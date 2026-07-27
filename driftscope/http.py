"""HTTP + on-disk cache + `gh` bridge. Standard library only, no pip installs.

Registry APIs are slow and rate-limited, and a scan hits the same endpoints
repeatedly across a monorepo, so every GET is cached on disk. `--offline` serves
only from cache and never opens a socket, which makes runs reproducible in CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USER_AGENT = "driftscope/0.1 (+https://github.com/)"
DEFAULT_TTL = 6 * 3600  # registry version lists move slowly


class HttpError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


@dataclass
class Cache:
    root: Path
    ttl: int = DEFAULT_TTL
    hits: int = 0
    misses: int = 0
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()
        return self.root / h[:2] / f"{h}.json"

    def get(self, key: str, *, ignore_ttl: bool = False) -> Any | None:
        if not self.enabled:
            return None
        p = self._path(key)
        # One stat instead of exists()+stat(): exists() is itself a stat call, and
        # this runs on every lookup in a scan that makes thousands of them.
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return None
        if not ignore_ttl and (time.time() - mtime) > self.ttl:
            return None
        try:
            payload = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        self.hits += 1
        return payload.get("body")

    def fresh(self, key: str) -> bool:
        """True when a live (within-TTL) entry exists, without reading it.

        Callers that only need to know whether a request will hit the network -- the
        search-throttle checks -- used `get()` for this, which read and JSON-parsed
        the whole body only to discard it, then `api()` read and parsed it a second
        time. It also double-counted the hit.
        """
        if not self.enabled:
            return False
        try:
            mtime = self._path(key).stat().st_mtime
        except OSError:
            return False
        return (time.time() - mtime) <= self.ttl

    def put(self, key: str, body: Any) -> None:
        """Write atomically, via a temp file unique to this writer.

        The temp name used to be derived from the key alone, so two threads
        writing the same URL raced: the first `replace()` consumed the shared temp
        file and the second raised FileNotFoundError. That is not hypothetical --
        resolution runs eight workers and a real scan resolves `rails/rails` from
        five components and `kubernetes/kubernetes` from four, all hitting one URL.
        The exception escaped `put`, propagated out of the resolver, and was caught
        by the dispatcher as a resolver failure, so the component rendered as
        `unknown` for no reason but a cache collision -- and only sometimes.

        `replace()` is atomic on POSIX and Windows, so concurrent writers now
        simply last-write-wins on identical content rather than colliding.
        """
        if not self.enabled:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps({"key": key, "at": time.time(), "body": body}))
            tmp.replace(p)
        except OSError:
            # A cache write must never fail a scan: the answer is already in hand.
            tmp.unlink(missing_ok=True)

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)


@dataclass
class Client:
    cache: Cache
    offline: bool = False
    timeout: float = 20.0
    retries: int = 2
    errors: list[str] = field(default_factory=list)
    requests_made: int = 0

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cache_key: str | None = None,
        ttl_override: int | None = None,
    ) -> Any:
        key = cache_key or url
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.offline:
            stale = self.cache.get(key, ignore_ttl=True)
            if stale is not None:
                return stale
            raise HttpError(f"offline and not cached: {url}")

        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url)
                req.add_header("User-Agent", USER_AGENT)
                req.add_header("Accept", "application/json")
                for k, v in (headers or {}).items():
                    req.add_header(k, v)
                self.requests_made += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8", "replace"))
                self.cache.put(key, body)
                return body
            except urllib.error.HTTPError as e:
                # 4xx are terminal; retrying a 404 just wastes time.
                if e.code in (404, 400, 401, 403) and e.code != 429:
                    raise HttpError(f"HTTP {e.code} for {url}", e.code) from e
                last = e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last = e
            if attempt < self.retries:
                time.sleep(0.6 * (2**attempt))
        raise HttpError(f"{type(last).__name__}: {last} for {url}")

    def get_text(self, url: str, *, cache_key: str | None = None) -> str:
        key = (cache_key or url) + "#text"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.offline:
            stale = self.cache.get(key, ignore_ttl=True)
            if stale is not None:
                return stale
            raise HttpError(f"offline and not cached: {url}")
        req = urllib.request.Request(url)
        req.add_header("User-Agent", USER_AGENT)
        try:
            self.requests_made += 1
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise HttpError(f"HTTP {e.code} for {url}", e.code) from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise HttpError(f"{type(e).__name__}: {e} for {url}") from e
        self.cache.put(key, text)
        return text


# --------------------------------------------------------------------------- gh

_GH_AVAILABLE: bool | None = None
_GH_TOKEN: str | None | bool = False  # False = not yet looked up


def gh_token() -> str | None:
    """The token `gh` already holds, read once per process.

    This keeps the zero-configuration promise -- no PAT to manage -- while paying
    the subprocess cost a single time instead of on every request.
    """
    global _GH_TOKEN
    if _GH_TOKEN is not False:
        return _GH_TOKEN  # type: ignore[return-value]
    _GH_TOKEN = None
    if shutil.which("gh"):
        try:
            r = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0 and r.stdout.strip():
                _GH_TOKEN = r.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
    return _GH_TOKEN  # type: ignore[return-value]


def gh_available() -> bool:
    """True when the `gh` CLI is installed and authenticated.

    We delegate GitHub auth to `gh` rather than managing a PAT: the token already
    exists, is kept in the keyring, and gh handles rate-limit headers.
    """
    global _GH_AVAILABLE
    if _GH_AVAILABLE is not None:
        return _GH_AVAILABLE
    if not shutil.which("gh"):
        _GH_AVAILABLE = False
        return False
    try:
        r = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=15
        )
        _GH_AVAILABLE = r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        _GH_AVAILABLE = False
    return _GH_AVAILABLE


# GitHub's search endpoint permits roughly 30 requests/minute for authenticated
# callers -- far tighter than the 5000/hour core limit. Velocity spends ~12 search
# calls per repo, so without pacing a multi-repo run gets throttled and the results
# come back empty.
SEARCH_PER_MINUTE = 26


@dataclass
class GitHub:
    cache: Cache
    offline: bool = False
    queries: int = 0
    enabled: bool = True
    throttled_waits: int = 0
    _search_times: list[float] = field(default_factory=list)
    # The delta phase calls this from a thread pool, so the window bookkeeping
    # needs a lock; without it two threads can both see a free slot.
    _search_lock: threading.Lock = field(default_factory=threading.Lock)

    def _await_search_slot(self) -> None:
        """Block until another search request fits inside the per-minute budget."""
        with self._search_lock:
            self._reserve_search_slot()

    def _reserve_search_slot(self) -> None:
        now = time.time()
        self._search_times = [t for t in self._search_times if now - t < 60.0]
        if len(self._search_times) >= SEARCH_PER_MINUTE:
            sleep_for = 60.0 - (now - self._search_times[0]) + 0.25
            if sleep_for > 0:
                self.throttled_waits += 1
                time.sleep(sleep_for)
                now = time.time()
                self._search_times = [t for t in self._search_times if now - t < 60.0]
        self._search_times.append(time.time())

    def api(self, path: str, *, paginate: bool = False) -> Any:
        """Call the GitHub API. Returns parsed JSON, or None on failure.

        Auth still comes from `gh` -- the token is read once via `gh auth token`
        and reused over plain HTTP, so there is still no PAT to manage.

        Measured on the same endpoint: `gh api` 517ms, urllib 432ms, urllib with
        a kept-alive connection 398ms. So dropping the subprocess is worth about
        16%, not the 3x an earlier (unfair) measurement suggested -- network
        round-trip dominates. The real lever is issuing *fewer* calls, which is
        why the velocity engine walks the issues list once instead of running
        three searches per window. Keep-alive was left out: another 8% did not
        justify holding a connection open across a threaded scan.
        """
        key = f"gh:{path}:{paginate}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.offline or not self.enabled:
            return self.cache.get(key, ignore_ttl=True)

        token = gh_token()
        if token is None:
            return self._api_via_cli(path, paginate=paginate, key=key)

        url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        try:
            self.queries += 1
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # 403/429 here is the rate limit; the caller distinguishes failure
            # from "no data", so returning None is what keeps that honest.
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None
        self.cache.put(key, body)
        return body

    def _api_via_cli(self, path: str, *, paginate: bool, key: str) -> Any:
        """Fallback for when no token can be read but `gh` itself works."""
        if not gh_available():
            return None
        cmd = ["gh", "api", "-H", "Accept: application/vnd.github+json"]
        if paginate:
            cmd.append("--paginate")
        cmd.append(path)
        try:
            self.queries += 1
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or not r.stdout.strip():
                return None
            out = r.stdout.strip()
            body = json.loads(out) if out.startswith(("{", "[")) else None
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            return None
        if body is not None:
            self.cache.put(key, body)
        return body

    def search_issues(self, q: str, *, limit: int = 40) -> list[dict]:
        from urllib.parse import quote

        path = f"search/issues?q={quote(q)}&per_page={min(limit, 100)}&sort=created&order=desc"
        if not self.cache.fresh(f"gh:{path}:False"):
            self._await_search_slot()
        body = self.api(path)
        if not isinstance(body, dict):
            return []
        return body.get("items", []) or []

    def search_count(self, q: str) -> int | None:
        """Result count, or None when the query failed.

        Returning 0 for a failed call is dangerous: GitHub's search API has a
        secondary rate limit around 30 requests/minute, and a throttled call was
        being read as "this project has no issues" -- a confident wrong answer.
        """
        from urllib.parse import quote

        path = f"search/issues?q={quote(q)}&per_page=1"
        if not self.cache.fresh(f"gh:{path}:False"):
            self._await_search_slot()
        body = self.api(path)
        if not isinstance(body, dict):
            return None
        return int(body.get("total_count") or 0)


def default_cache_dir() -> Path:
    env = os.environ.get("DRIFTSCOPE_CACHE")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "driftscope"
    return Path.home() / ".cache" / "driftscope"
