"""RubyGems and the Ruby runtime.

Two endpoints, both public and unauthenticated:

  /api/v1/versions/{gem}.json  every version with its publish date
  /api/v1/gems/{gem}.json      the current version, plus source/homepage URIs

The versions feed carries `created_at`, which is what makes "your pin is 1,400
days old" answerable rather than only "you are 3 minors behind".

Two Ruby-specific wrinkles are handled deliberately:

**Platform variants.** A gem can publish `1.2.3` alongside `1.2.3-x86_64-linux`
and `1.2.3-java`. Those are the same release compiled for different targets, not
newer versions, so only the pure-Ruby platform is compared. Mixing them would
report a platform swap as an upgrade -- the same class of mistake as comparing a
`-slim` Docker tag against an `-alpine` one.

**The runtime is scored on support, not distance.** Ruby 3.2 is two minors behind
4.0 and out of support; Ruby 3.4 is one behind and fully maintained. Distance says
those are similar. The support calendar says they are opposites, which is why the
runtime carries dates the same way .NET and the JVM do.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote

from ..http import HttpError
from ..models import Component, Release, Resolution
from ..version import latest_stable, parse
from . import Context, parse_iso

# Sourced from ruby-lang.org/en/downloads/branches. `None` means still in normal
# maintenance with no announced end date.
RUBY_SUPPORT_END = {
    "4.0": None,
    "3.4": None,
    "3.3": "2027-03-31",
    "3.2": "2026-04-01",
    "3.1": "2025-03-26",
    "3.0": "2024-04-23",
    "2.7": "2023-03-31",
    "2.6": "2022-04-12",
    "2.5": "2021-04-05",
    "2.4": "2020-03-31",
    "2.3": "2019-03-31",
    "2.2": "2018-03-31",
    "2.1": "2017-03-31",
    "2.0": "2016-02-24",
    "1.9": "2015-02-23",
}
RUBY_CURRENT = "4.0"
# Branches in security-only maintenance: still patched, but not receiving bug
# fixes, which is worth saying out loud rather than folding into "supported".
RUBY_SECURITY_ONLY = {"3.3"}

_GITHUB_URL = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+)")


def _repo_from(*candidates: object) -> str | None:
    for c in candidates:
        if isinstance(c, str):
            m = _GITHUB_URL.search(c)
            if m:
                return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
    return None


def _minor(version: str | None) -> str | None:
    """`3.1.2` -> `3.1`. Ruby's support calendar is per minor line."""
    v = parse(version or "")
    if not v.valid or not v.release:
        return None
    return f"{v.major}.{v.minor}"


def resolve_rubygem(c: Component, ctx: Context) -> Resolution:
    name = quote(c.name, safe="")
    try:
        body = ctx.client.get_json(f"https://rubygems.org/api/v1/versions/{name}.json")
    except HttpError as e:
        return Resolution(error=str(e))
    if not isinstance(body, list):
        return Resolution(error="unexpected rubygems payload")

    releases: list[Release] = []
    for v in body:
        if not isinstance(v, dict):
            continue
        number = v.get("number")
        if not isinstance(number, str) or not number:
            continue
        # Platform builds are the same release for a different target. Comparing
        # `1.2.3` against `1.2.3-x86_64-linux` would read a platform swap as an
        # upgrade, so only the pure-Ruby platform is considered.
        if (v.get("platform") or "ruby") != "ruby":
            continue
        releases.append(Release(
            version=number,
            published=parse_iso(v.get("created_at")),
            prerelease=bool(v.get("prerelease")),
            yanked=False,
        ))

    if not releases:
        return Resolution(
            note=f"no pure-Ruby platform releases listed for {c.name}",
        )

    usable = [r.version for r in releases if not r.prerelease]
    repo, homepage, deprecated, note = None, None, False, None
    try:
        meta = ctx.client.get_json(f"https://rubygems.org/api/v1/gems/{name}.json")
    except HttpError:
        meta = None
    if isinstance(meta, dict):
        repo = _repo_from(meta.get("source_code_uri"), meta.get("homepage_uri"),
                          meta.get("bug_tracker_uri"), meta.get("changelog_uri"))
        homepage = meta.get("homepage_uri") or meta.get("project_uri")
        if meta.get("yanked"):
            deprecated = True
            note = "gem yanked from RubyGems"

    return Resolution(
        latest_stable=latest_stable(usable),
        releases=releases,
        repo=repo,
        homepage=homepage,
        deprecated=deprecated,
        note=note,
    )


def resolve_ruby_runtime(c: Component, ctx: Context) -> Resolution:
    """Ruby itself, judged on its support calendar rather than version distance.

    Mirrors the .NET and JVM resolvers: `extra.support_days_left` is what the
    advisor scores on, so a maintained older line is not rated as risk merely for
    being behind.
    """
    line = _minor(c.current)
    if line is None:
        return Resolution(
            latest_stable=RUBY_CURRENT,
            homepage="https://www.ruby-lang.org/en/downloads/branches/",
            repo="ruby/ruby",
            note=f"could not read a Ruby version out of {c.current!r}",
        )

    known = line in RUBY_SUPPORT_END
    end = RUBY_SUPPORT_END.get(line)
    days_left = None
    if end:
        try:
            days_left = (
                datetime.fromisoformat(end).replace(tzinfo=timezone.utc) - ctx.now
            ).days
        except ValueError:
            days_left = None

    notes = []
    if not known:
        # An unlisted line is unknown, not fine. Saying nothing would let a
        # never-heard-of version render as supported.
        notes.append(
            f"Ruby {line} is not in the known support calendar; status unverified"
        )
    elif end is None:
        notes.append(f"Ruby {line} is in normal maintenance")
    elif days_left is not None and days_left < 0:
        notes.append(f"Ruby {line} reached end of life on {end}")
    else:
        phase = ("security maintenance only" if line in RUBY_SECURITY_ONLY
                 else "supported")
        notes.append(f"Ruby {line} is in {phase} until {end}")

    return Resolution(
        latest_stable=RUBY_CURRENT,
        releases=[Release(version=v) for v in sorted(RUBY_SUPPORT_END)],
        repo="ruby/ruby",
        homepage="https://www.ruby-lang.org/en/downloads/branches/",
        deprecated=bool(days_left is not None and days_left < 0),
        note="; ".join(notes) or None,
        extra={
            "support_end": end,
            "support_days_left": days_left,
            # The advisor reads `lts` to mean "a legitimate long-term choice".
            # Ruby has no LTS programme, so a line in normal maintenance is the
            # closest equivalent and security-only is explicitly not it.
            "lts": known and end is None,
            "security_only": line in RUBY_SECURITY_ONLY,
        },
    )
