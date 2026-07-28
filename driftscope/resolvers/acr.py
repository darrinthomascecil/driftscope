"""Azure Container Registry: versions and dates for images you host yourself.

Every other resolver in this package asks a public index about a public artifact.
This one asks a registry the user owns about an image nobody else can see, which
is precisely why it matters: an internal image is the one with no upstream
maintainer nudging it forward, so it is the likeliest thing in an estate to be
years stale, and until now `oci.resolve_image` answered `registry <host> not
supported for tag enumeration` and gave up.

------------------------------------------------------------------- three dates

The subtlety this module exists to handle is that an image has more than one
date, and they disagree by years:

  push time   when the tag arrived in *this* registry. ACR reports it as
              `createdTime`, which reads like the image's birthday and is not.
              Import four images built between 2025 and 2026 and all four get
              today's push time.
  build time  the `created` field inside the image config blob -- when this
              exact image was assembled. This is the one that answers "how old
              is the thing running in production".
  release date of the underlying version upstream, which a private registry
              simply does not know.

Reporting push time as the image's age is wrong in the direction that hides the
problem: a three-year-old image re-pushed during a registry migration reads as
brand new. So `Release.published` here is always a *build* time, and is None
when it was not fetched -- never a push time standing in for one. Push times are
kept separately in `extra["pushed"]`, because for an internal registry they
genuinely answer a different question (when was this promoted?).

Build time costs two or three extra round trips per tag -- resolve the manifest,
follow a multi-arch index to a child, fetch the config blob -- so it is sampled
for the tags that matter (the one in use and the newest) rather than for every
tag in a list that may run to hundreds. `extra["build_dates_sampled"]` records
which, so a None elsewhere is legibly "not asked" rather than "no answer".

Auth is `az acr login --expose-token`, exchanged for a registry access token.
That keeps the no-keys rule: the credential is the one `az login` already
minted, there is no admin password and no PAT.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from ..models import Component, Release, Resolution
from ..version import sort_versions
from . import Context, parse_iso

ACR_HOST_SUFFIX = ".azurecr.io"

# Accepted in preference order: an index first, since a modern image is usually
# multi-arch and the single-manifest types are the fallback.
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

# How many tags to fetch a build date for. Two is the useful minimum: what is
# deployed, and what it would move to.
BUILD_DATE_SAMPLE = 2

_TOKENS: dict[str, str | None] = {}
# Resolution runs eight workers, and every image in one registry contends for the
# same token. Without this lock the first thread in publishes "no token yet" as
# though it were "no token exists", and every sibling reports the registry as
# unauthenticated while the winner resolves it fine -- two findings for the same
# registry disagreeing within one report.
_TOKEN_LOCK = threading.Lock()


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Strip the bearer token when a blob GET is redirected to blob storage.

    ACR answers `/v2/{repo}/blobs/{digest}` with a 302 to a pre-signed Azure
    Storage URL. urllib replays every header on a redirect, and storage rejects a
    request carrying both its own signature and someone else's bearer token --
    with a 403, which reads as a permissions problem rather than as the redirect
    artifact it is.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def parse_acr_ref(name: str) -> tuple[str, str] | None:
    """`myreg.azurecr.io/team/app` -> `("myreg.azurecr.io", "team/app")`."""
    host, _, repo = name.partition("/")
    if not repo or not host.lower().endswith(ACR_HOST_SUFFIX):
        return None
    return host, repo


def _registry_token(host: str, repo: str, ctx: Context) -> str | None:
    """A pull/metadata token for one repository, minted once per registry.

    Cached per host rather than per repository: the scope differs, but a scan
    touching several repositories in one registry is common and the exchange is
    the expensive half.
    """
    with _TOKEN_LOCK:
        if host in _TOKENS:
            return _TOKENS[host]
        token = _mint_token(host, repo, ctx)
        _TOKENS[host] = token
        return token


def _mint_token(host: str, repo: str, ctx: Context) -> str | None:
    """The actual exchange. Called once per registry, under `_TOKEN_LOCK`."""
    if not ctx.allow_az:
        return None

    registry = host.split(".")[0]
    try:
        r = subprocess.run(
            ["az", "acr", "login", "-n", registry, "--expose-token",
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    refresh = r.stdout.strip()
    if r.returncode != 0 or not refresh:
        return None

    # Actions within one scope are comma-separated. A space here is a 400 that
    # says only "Bad Request".
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "service": host,
            "scope": f"repository:{repo}:pull,metadata_read",
            "refresh_token": refresh,
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"https://{host}/oauth2/token", data=body), timeout=30
        ) as resp:
            return json.loads(resp.read()).get("access_token")
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None


def _get(host: str, path: str, token: str, accept: str | None = None):
    req = urllib.request.Request(f"https://{host}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    if accept:
        req.add_header("Accept", accept)
    with _opener.open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _build_date(host: str, repo: str, tag: str, token: str) -> datetime | None:
    """The `created` timestamp inside the image config, or None.

    None here means "could not determine", and the caller must not substitute a
    push time for it -- that is the conflation this module exists to prevent.
    """
    try:
        m = _get(host, f"/v2/{repo}/manifests/{tag}", token, MANIFEST_ACCEPT)
        if "manifests" in m:
            # A multi-arch index. Any concrete child carries the same build
            # timestamp; `unknown` platforms are attestation blobs, not images.
            child = next(
                (
                    x
                    for x in m["manifests"]
                    if (x.get("platform") or {}).get("os") not in (None, "unknown")
                ),
                None,
            )
            if child is None:
                return None
            m = _get(host, f"/v2/{repo}/manifests/{child['digest']}", token, MANIFEST_ACCEPT)
        digest = (m.get("config") or {}).get("digest")
        if not digest:
            return None
        return parse_iso(_get(host, f"/v2/{repo}/blobs/{digest}", token).get("created"))
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError):
        return None


def resolve_acr_image(c: Component, ctx: Context) -> Resolution | None:
    """Versions and dates for an `*.azurecr.io` image, or None if not one."""
    ref = parse_acr_ref(c.name)
    if ref is None:
        return None
    host, repo = ref

    token = _registry_token(host, repo, ctx)
    if not token:
        return Resolution(
            note=f"{host}: no Azure credential (run `az login`), tags not enumerated"
        )

    try:
        body = _get(host, f"/acr/v1/{repo}/_tags?n=500&orderby=timedesc", token)
    except urllib.error.HTTPError as e:
        # 401/403 must not read as "an empty registry".
        return Resolution(error=f"{host}/{repo}: HTTP {e.code} listing tags")
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return Resolution(error=f"{host}/{repo}: {type(e).__name__} listing tags")

    entries = body.get("tags") or []
    if not entries:
        return Resolution(repo=None, note=f"{host}/{repo}: no tags in the registry")

    pushed = {
        t["name"]: t.get("createdTime")
        for t in entries
        if isinstance(t, dict) and t.get("name")
    }
    ordered = sort_versions(list(pushed))
    latest = ordered[-1].raw if ordered else None

    # Sample build dates only where they change a decision: the tag in use and
    # the tag it would move to.
    want = [t for t in (c.current, latest) if t and t in pushed][:BUILD_DATE_SAMPLE]
    built = {t: _build_date(host, repo, t, token) for t in want}

    releases = [
        Release(version=name, published=built.get(name)) for name in pushed
    ]
    return Resolution(
        latest_stable=latest,
        releases=releases,
        note=(
            "private registry: dates are image build times, not upstream release "
            "dates; there is no upstream project to compare against"
        ),
        extra={
            "registry": host,
            "pushed": pushed,
            "build_dates_sampled": want,
            "dates_are_build_times": True,
        },
    )
