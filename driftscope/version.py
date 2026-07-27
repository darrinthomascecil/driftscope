"""Version algebra.

Version strings in infrastructure are not just semver. This module handles the four
shapes that actually turn up:

  semver   1.2.3, v1.2.3, 3.0.0-rc.1, 4.5.0+build
  calver   2025-06-01, 2025-06-01-preview   (Azure API versions, some charts)
  k8sapi   v1, v1beta1, v2beta2            (ordered by group rules, not numbers)
  opaque   3.12-slim, alpine, latest       (Docker tags: variant-qualified)

Comparison only ever happens between versions of the same `kind`, and for Docker
tags only within the same `variant` -- comparing 3.12-slim to 3.13-alpine is
meaningless and we refuse rather than guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime, timezone

SEMVER = "semver"
CALVER = "calver"
K8SAPI = "k8sapi"
OPAQUE = "opaque"

# Ordered weakest -> strongest. Anything not listed sorts before "" (final).
_PRE_RANK = {
    "dev": -60,
    "alpha": -50,
    "a": -50,
    "preview": -45,
    "beta": -40,
    "b": -40,
    "rc": -30,
    "pre": -30,
    "": 0,
}

_PRE_TOKENS = tuple(k for k in _PRE_RANK if k)

_CALVER_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[-.](.+))?$")
_SEMVER_RE = re.compile(
    r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?"  # up to 4 numeric segments
    # Optional pre-release. The token is letters-only so that "beta1" splits into
    # ("beta", 1) rather than being swallowed whole as an unknown token.
    r"(?:[-_.]?([A-Za-z]+)[-_.]?(\d+)?)?"
    r"(?:\+([0-9A-Za-z.-]+))?$"  # build metadata (ignored)
)
_K8SAPI_RE = re.compile(r"^v(\d+)(?:(alpha|beta)(\d+))?$")

# Docker tag variants we recognise; comparison is scoped within a variant.
# Atomic variant tokens. Compounds are composed from these, so "bullseye-slim" is
# a distinct variant from "slim" -- which matters, because the first also pins an
# older Debian base and must not be offered as an upgrade for the second.
_VARIANT_TOKENS = (
    "alpine",
    "slim",
    "trixie",
    "bookworm",
    "bullseye",
    "buster",
    "jammy",
    "focal",
    "noble",
    "windowsservercore",
    "nanoserver",
    "distroless",
)

# Longest token first, so "bullseye-slim" is consumed before "slim" matches. This
# is precomputed because the sort was previously re-run on every iteration of the
# match loop, for every tag parsed.
_VARIANT_BY_LENGTH = tuple(sorted(_VARIANT_TOKENS, key=len, reverse=True))


def normalize(s: str) -> str:
    """Loose canonical form for equality checks."""
    return (s or "").strip().lstrip("vV").lower()


@dataclass(frozen=True)
class Version:
    raw: str
    kind: str
    release: tuple[int, ...] = ()
    pre: tuple[int, int] | None = None  # (rank, number)
    variant: str = ""
    valid: bool = True

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None

    @property
    def major(self) -> int:
        return self.release[0] if self.release else 0

    @property
    def minor(self) -> int:
        return self.release[1] if len(self.release) > 1 else 0

    @property
    def patch(self) -> int:
        return self.release[2] if len(self.release) > 2 else 0

    def sort_key(self) -> tuple:
        # Pad release so 1.2 and 1.2.0 compare equal.
        rel = tuple(self.release) + (0,) * (4 - len(self.release))
        pre = self.pre if self.pre is not None else (0, 0)
        return (rel, pre)

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.raw


def _pre_from(token: str | None, num: str | None) -> tuple[int, int] | None:
    if not token:
        return None
    t = token.lower()
    if t not in _PRE_RANK:
        # Unknown alpha token (e.g. "slim", "final"): not a pre-release marker.
        return None
    return (_PRE_RANK[t], int(num) if num else 0)


def _split_variant(tag: str) -> tuple[str, str]:
    """Split a Docker tag into (version-ish part, variant).

    The variant is the *whole* trailing token chain, not merely the last token:
    `18-slim` and `18-bullseye-slim` are different variants. Collapsing both to
    "slim" left the resolver choosing arbitrarily between `26.5-slim` and
    `26.5-bullseye-slim`, and it recommended the older Debian base.
    """
    low = tag.lower()
    found: list[str] = []
    while low:
        for v in _VARIANT_BY_LENGTH:
            if low == v:
                found.insert(0, v)
                low = ""
                break
            if low.endswith("-" + v):
                found.insert(0, v)
                low = low[: -(len(v) + 1)]
                break
        else:
            break
    return tag[: len(low)], "-".join(found)


@lru_cache(maxsize=8192)
def _parse_cached(raw: str, hint: str | None) -> Version:
    return _parse_uncached(raw, hint=hint)


def parse(raw: str | None, *, hint: str | None = None) -> Version:
    """Parse a version string; memoised because a large scan parses the same few
    thousand strings over 100k times."""
    if raw is None:
        return Version(raw="", kind=OPAQUE, valid=False)
    return _parse_cached(raw, hint)


def _parse_uncached(raw: str | None, *, hint: str | None = None) -> Version:
    """Parse a version string. `hint` forces a kind when the caller knows it."""
    if raw is None:
        return Version(raw="", kind=OPAQUE, valid=False)
    s = raw.strip()
    if not s:
        return Version(raw=raw, kind=OPAQUE, valid=False)

    if hint == K8SAPI or (hint is None and _K8SAPI_RE.match(s)):
        m = _K8SAPI_RE.match(s)
        if m:
            major = int(m.group(1))
            stage, num = m.group(2), m.group(3)
            pre = _pre_from(stage, num) if stage else None
            return Version(raw=raw, kind=K8SAPI, release=(major,), pre=pre)

    m = _CALVER_RE.match(s)
    if m:
        y, mo, d, suffix = m.groups()
        pre = None
        if suffix:
            sl = suffix.lower()
            for tok in _PRE_TOKENS:
                if tok in sl:
                    pre = (_PRE_RANK[tok], 0)
                    break
            else:
                pre = (_PRE_RANK["preview"], 0)  # unknown suffix on a date == not GA
        return Version(raw=raw, kind=CALVER, release=(int(y), int(mo), int(d)), pre=pre)

    body, variant = _split_variant(s)
    cand = body.lstrip("vV")
    m = _SEMVER_RE.match(cand)
    if m and cand:
        nums = [int(g) for g in m.group(1, 2, 3, 4) if g is not None]
        pre = _pre_from(m.group(5), m.group(6))
        return Version(
            raw=raw, kind=SEMVER, release=tuple(nums), pre=pre, variant=variant
        )

    return Version(raw=raw, kind=OPAQUE, variant=variant, valid=False)


def comparable(a: Version, b: Version) -> tuple[bool, str | None]:
    if not a.valid or not b.valid:
        return False, f"unparseable version ({a.raw!r} vs {b.raw!r})"
    if a.kind != b.kind:
        return False, f"different version schemes ({a.kind} vs {b.kind})"
    if a.kind == SEMVER and a.variant != b.variant:
        return False, f"different image variants ({a.variant or 'default'} vs {b.variant or 'default'})"
    return True, None


def compare(a: Version, b: Version) -> int:
    """-1 if a<b, 0 if equal, 1 if a>b. Assumes comparable()."""
    ka, kb = a.sort_key(), b.sort_key()
    return (ka > kb) - (ka < kb)


def sort_versions(versions: list[str], *, hint: str | None = None) -> list[Version]:
    parsed = [parse(v, hint=hint) for v in versions]
    valid = [p for p in parsed if p.valid]
    valid.sort(key=lambda p: p.sort_key())
    return valid


def latest_stable(versions: list[str], *, hint: str | None = None) -> str | None:
    """Highest non-prerelease version. Falls back to highest prerelease if none.

    A single max-scan rather than a full sort plus two list builds: only the top
    element is ever used, and a busy provider ships several hundred versions that
    this is called on once per component.
    """
    best_stable: tuple[tuple, Version] | None = None
    best_any: tuple[tuple, Version] | None = None
    for v in versions:
        p = parse(v, hint=hint)
        if not p.valid:
            continue
        k = p.sort_key()
        # `>=` keeps the last of equally-ranked versions, matching the stable sort
        # this replaced ("1.0" and "1.0.0" rank equal; the later one wins).
        if best_any is None or k >= best_any[0]:
            best_any = (k, p)
        if not p.is_prerelease and (best_stable is None or k >= best_stable[0]):
            best_stable = (k, p)
    chosen = best_stable or best_any
    return chosen[1].raw if chosen else None


def constraint_floor(raw: str | None) -> str | None:
    """Extract a concrete-ish version from a constraint expression.

    ">= 3.0, < 4.0" -> "3.0";  "~> 3.75.0" -> "3.75.0";  "^1.2.3" -> "1.2.3"
    Returns None when no version-looking token is present (e.g. "*", "latest").
    """
    if not raw:
        return None
    # Prefer the token attached to an equality/pessimistic operator.
    for pat in (r"==\s*([0-9][^\s,|]*)", r"~>\s*([0-9][^\s,|]*)", r"\^\s*([0-9][^\s,|]*)",
                r">=\s*([0-9][^\s,|]*)", r"~\s*([0-9][^\s,|]*)"):
        m = re.search(pat, raw)
        if m:
            return m.group(1).rstrip(".*")
    # An upper bound alone ("<2", "<=1.9") states a ceiling, not the version in
    # use. Returning it would claim the project is on 2.x when it is capped below
    # 2.x -- the opposite of the truth.
    if re.search(r"<=?\s*[0-9]", raw) and not re.search(r">=?\s*[0-9]", raw):
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)*(?:[-.][A-Za-z0-9]+)*)", raw)
    return m.group(1) if m else None


def is_pinned(raw: str | None) -> bool:
    """True when the constraint names one exact version."""
    if not raw:
        return False
    s = raw.strip()
    if re.fullmatch(r"v?[0-9][^\s,|<>=~^*]*", s):
        return True
    return bool(re.fullmatch(r"==\s*v?[0-9][^\s,|]*", s))


def _calver_to_dt(v: Version):
    """A calver version's own date, as a datetime."""
    if v.kind != CALVER or len(v.release) < 3:
        return None
    try:
        return datetime(v.release[0], v.release[1], v.release[2], tzinfo=timezone.utc)
    except ValueError:
        return None


def lag_between(
    current: str | None,
    latest: str | None,
    *,
    releases: list | None = None,
    hint: str | None = None,
    current_published: datetime | None = None,
    latest_published: datetime | None = None,
    now: datetime | None = None,
):
    """Compute a Lag. `releases` is a list of models.Release for counting."""
    from .models import Lag

    cv, lv = parse(current, hint=hint), parse(latest, hint=hint)
    ok, why = comparable(cv, lv)
    if not ok:
        # Version *strings* may be incomparable (opaque AKS node image names,
        # mismatched Docker variants) while the dates remain perfectly meaningful.
        # "I cannot compare these strings, but yours is 400 days older" is useful,
        # so dates are computed independently of string comparability.
        incomparable = Lag(comparable=False, reason=why)
        if current_published and latest_published:
            incomparable.days_behind = max(
                0, (latest_published - current_published).days
            )
        if current_published and now:
            incomparable.current_age_days = max(0, (now - current_published).days)
        return incomparable

    if compare(cv, lv) >= 0:
        lag = Lag(comparable=True)
    else:
        lag = Lag(
            comparable=True,
            behind_major=max(0, lv.major - cv.major),
            behind_minor=max(0, lv.minor - cv.minor) if lv.major == cv.major else 0,
            behind_patch=max(0, lv.patch - cv.patch)
            if (lv.major, lv.minor) == (cv.major, cv.minor)
            else 0,
        )

    # A calendar version *is* its own date, so no registry timestamp is needed --
    # and for Azure API versions none exists (`az provider show` returns bare
    # version strings). Derive the dates from the versions themselves.
    if cv.kind == CALVER and current_published is None:
        current_published = _calver_to_dt(cv)
    if lv.kind == CALVER and latest_published is None:
        latest_published = _calver_to_dt(lv)

    if releases:
        # cv's sort key and its kind/variant are loop-invariant. Calling
        # comparable()/compare() per release re-padded cv's tuple and allocated a
        # result tuple for every version in a list that runs to several hundred
        # entries for a busy provider. The checks below are comparable() inlined --
        # cv.valid is already guaranteed by the early return above.
        cv_key = cv.sort_key()
        newer, newer_stable = 0, 0
        for r in releases:
            rv = parse(r.version, hint=hint)
            if not rv.valid or rv.kind != cv.kind:
                continue
            if rv.kind == SEMVER and rv.variant != cv.variant:
                continue
            if rv.sort_key() <= cv_key:
                continue
            newer += 1
            if not rv.is_prerelease:
                newer_stable += 1
        lag.releases_behind = newer
        lag.stable_releases_behind = newer_stable

    if current_published and latest_published:
        lag.days_behind = max(0, (latest_published - current_published).days)
    if current_published and now:
        lag.current_age_days = max(0, (now - current_published).days)

    return lag
