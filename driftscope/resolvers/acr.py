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
  release date of the underlying version upstream, which the registry itself
              does not know -- but the *image* may, if it was built with an
              `org.opencontainers.image.source` label. When it carries one, that
              URL is resolved to a GitHub or Azure DevOps repo and set as the
              resolution's `repo`/`forge`, so the same delta engine that dates a
              public image's upgrade window and lists the issues filed across it
              runs for the private one too. The issue window is bounded by the
              upstream project's own release dates, not the image build times:
              build times cluster when versions are rebuilt in a batch, and a
              collapsed window drops every issue in it (see ghub/delta.py).

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

# How many versions in the deployed -> latest span to open. Each costs up to
# three round trips, and a registry can hold hundreds of tags, so the span is
# capped rather than unbounded. `extra["span_truncated"]` says when the cap bit,
# because a silently shortened changelog reads as a shorter changelog.
SPAN_LIMIT = 12

# Where an image records what changed in it. There is no OCI standard field for
# a changelog -- the spec defines `description` for the image, not for the
# release -- so these are conventions, in preference order, and the report says
# a changelog is absent rather than implying nothing changed.
CHANGELOG_KEYS = (
    "io.driftscope.changelog",
    "org.label-schema.changelog",
    "changelog",
    "org.opencontainers.image.description",  # last resort: describes the image
)
KNOWN_ISSUE_KEYS = (
    "io.driftscope.known-issues",
    "io.driftscope.known_issues",
    "known-issues",
)
BREAKING_KEYS = (
    "io.driftscope.breaking",
    "io.driftscope.breaking-changes",
)

# Standard OCI annotations worth carrying into the report verbatim.
OCI_KEYS = (
    "org.opencontainers.image.title",
    "org.opencontainers.image.vendor",
    "org.opencontainers.image.source",
    "org.opencontainers.image.revision",
)


def _first_label(labels: dict, keys) -> str | None:
    for k in keys:
        v = (labels.get(k) or "").strip()
        if v:
            return v
    return None

# Two caches, because the two halves have different keys and different costs.
#
# The refresh token comes from an `az acr login` subprocess and is good for the
# whole registry, so it is worth paying for exactly once per host. The access
# token is scoped to one repository -- caching *that* per host was a real bug:
# whichever image resolved first minted a token for its own repository, and
# every other repository in the same registry then got a 401 that read as a
# permissions problem rather than as a cache key mistake.
_REFRESH: dict[str, str | None] = {}
_TOKENS: dict[tuple[str, str], str | None] = {}
# Resolution runs eight workers, and every image in one registry contends for
# these. Without the lock the first thread in publishes "not looked up yet" as
# though it were "no token exists", and its siblings report the registry as
# unauthenticated while the winner resolves it fine.
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
    key = (host, repo)
    with _TOKEN_LOCK:
        if key in _TOKENS:
            return _TOKENS[key]
        token = _mint_token(host, repo, ctx)
        _TOKENS[key] = token
        return token


def _refresh_token(host: str, ctx: Context) -> str | None:
    """The registry-wide refresh token, one `az` subprocess per host."""
    if host in _REFRESH:
        return _REFRESH[host]
    _REFRESH[host] = None
    if not ctx.allow_az:
        return None
    try:
        r = subprocess.run(
            ["az", "acr", "login", "-n", host.split(".")[0], "--expose-token",
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode == 0 and r.stdout.strip():
        _REFRESH[host] = r.stdout.strip()
    return _REFRESH[host]


def _mint_token(host: str, repo: str, ctx: Context) -> str | None:
    """Exchange the registry refresh token for one scoped to `repo`.

    Called under `_TOKEN_LOCK`, once per (host, repo).
    """
    if not ctx.allow_az:
        return None
    refresh = _refresh_token(host, ctx)
    if not refresh:
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


def _image_config(host: str, repo: str, tag: str, token: str) -> dict | None:
    """The image config blob for a tag, or None if it could not be fetched.

    One fetch yields both the build timestamp and the labels, so the changelog
    costs nothing beyond what the date already cost. None means "could not
    determine" -- the caller must not substitute a push time for a missing build
    date, which is the conflation this module exists to prevent.
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
        return _get(host, f"/v2/{repo}/blobs/{digest}", token)
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError):
        return None


def _labels(config: dict | None) -> dict:
    """Labels live under `config.Labels`, and are null rather than {} when unset."""
    if not config:
        return {}
    return ((config.get("config") or {}).get("Labels")) or {}


def _changelog_span(versions, current: str | None, latest: str | None):
    """The versions whose notes belong in a "what changed" summary.

    Everything strictly after what is deployed, up to and including the target.
    A version you are already on did not change anything for you, and versions
    beyond the target are not part of this upgrade.
    """
    if not current or not latest:
        return [v for v in versions[-1:]], False
    names = [v.raw for v in versions]
    try:
        lo = names.index(current)
    except ValueError:
        return [latest], False
    hi = names.index(latest) if latest in names else len(names) - 1
    span = names[lo + 1 : hi + 1]
    return span[-SPAN_LIMIT:], len(span) > SPAN_LIMIT


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

    # Open the config for what is deployed plus everything up to the target: one
    # fetch yields both the build date and that version's notes, so a changelog
    # costs nothing the date did not already cost.
    span, truncated = _changelog_span(ordered, c.current, latest)
    want = [t for t in ([c.current] if c.current in pushed else []) + span if t in pushed]
    configs = {t: _image_config(host, repo, t, token) for t in dict.fromkeys(want)}

    changelog, known_issues, breaking = [], [], []
    for tag in span:
        labels = _labels(configs.get(tag))
        if not labels:
            continue
        if entry := _first_label(labels, CHANGELOG_KEYS):
            changelog.append({"version": tag, "text": entry})
        if issue := _first_label(labels, KNOWN_ISSUE_KEYS):
            known_issues.append({"version": tag, "text": issue})
        if brk := _first_label(labels, BREAKING_KEYS):
            breaking.append({"version": tag, "text": brk})

    # Known issues in the version actually running are a different claim from
    # those in versions ahead of it: these are live, now, in production.
    current_labels = _labels(configs.get(c.current))
    current_issues = _first_label(current_labels, KNOWN_ISSUE_KEYS)

    releases = [
        Release(version=name, published=parse_iso((configs.get(name) or {}).get("created")))
        for name in pushed
    ]

    # A registry with no labels at all is the common case, and must read as
    # "nobody recorded anything" rather than as "nothing changed".
    documented = sum(1 for t in span if _labels(configs.get(t)))

    # The image may name its own source. When it does, the private registry stops
    # being a dead end: the upstream project's release notes and issue tracker can
    # answer "what changed" and "what broke" for the version span, exactly as they
    # do for a public image. `forge` routes the delta to GitHub or Azure DevOps.
    source = _first_label(current_labels, ("org.opencontainers.image.source",))
    from ..forge import forge_ref_from_url

    forge_ref = forge_ref_from_url(source)
    upstream_repo = forge_ref[1] if forge_ref else None
    forge_kind = forge_ref[0] if forge_ref else "github"

    if upstream_repo:
        note = (
            f"private registry: image dates are build times (they drive the age "
            f"figures); upstream project {upstream_repo} ({forge_kind}) supplies "
            "the release notes and issues, and the version window is bounded by "
            "that project's release dates when it dates its releases"
        )
    else:
        note = (
            "private registry: dates are image build times, not upstream release "
            "dates; the image names no source, so there is no upstream project to "
            "compare against"
        )
    if span and not documented and not upstream_repo:
        note += (
            f"; none of the {len(span)} version(s) ahead carry changelog labels, "
            "so what changed is unrecorded rather than empty"
        )

    return Resolution(
        latest_stable=latest,
        releases=releases,
        repo=upstream_repo,
        forge=forge_kind,
        homepage=source,
        note=note,
        extra={
            "registry": host,
            "pushed": pushed,
            "build_dates_sampled": sorted(configs),
            "dates_are_build_times": True,
            "changelog": changelog,
            "known_issues": known_issues,
            "current_known_issues": current_issues,
            "breaking": breaking,
            "span": span,
            "span_truncated": truncated,
            "documented_versions": documented,
            "oci": {k: v for k in OCI_KEYS if (v := current_labels.get(k))},
        },
    )
