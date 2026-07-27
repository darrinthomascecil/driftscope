"""NuGet, Maven Central, and runtime/toolchain resolvers.

NuGet needs two endpoints: flat-container lists versions cheaply, and the
registration index carries publish dates, deprecation notices and the project URL
(which is where the GitHub repo comes from, enabling the issue delta).

Maven Central's solrsearch `core=gav` returns every version of a coordinate with
epoch-millisecond timestamps in one call, so it is unusually well-behaved.

.NET target frameworks and Java/Gradle versions are *runtime* versions whose real
risk is support expiry rather than feature drift, so their tables are pinned here
with support-end dates.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote

from ..http import HttpError
from ..models import Component, Release, Resolution
from ..version import latest_stable, sort_versions
from . import Context, parse_iso

_GITHUB_RE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+)")


def _repo_from(*urls) -> str | None:
    for u in urls:
        if isinstance(u, str):
            m = _GITHUB_RE.search(u)
            if m:
                return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
    return None


# ----------------------------------------------------------------- NuGet


def resolve_nuget(c: Component, ctx: Context) -> Resolution:
    ident = c.name.lower()
    try:
        flat = ctx.client.get_json(
            f"https://api.nuget.org/v3-flatcontainer/{quote(ident)}/index.json"
        )
    except HttpError as e:
        return Resolution(error=str(e))
    versions = [v for v in (flat.get("versions") or []) if isinstance(v, str)]
    if not versions:
        return Resolution(error="nuget returned no versions")

    # Registration index adds dates, deprecation and the project URL.
    dates: dict[str, object] = {}
    deprecated_any = False
    project_url = None
    unlisted: set[str] = set()
    try:
        reg = ctx.client.get_json(
            f"https://api.nuget.org/v3/registration5-semver1/{quote(ident)}/index.json"
        )
        pages = reg.get("items") or []
        # Only the newest and oldest pages are needed for latest + the pinned one.
        for page in _relevant_pages(pages, c.current):
            items = page.get("items")
            if items is None and isinstance(page.get("@id"), str):
                try:
                    page = ctx.client.get_json(page["@id"])
                    items = page.get("items")
                except HttpError:
                    continue
            for it in items or []:
                ce = it.get("catalogEntry") or {}
                v = ce.get("version")
                if not isinstance(v, str):
                    continue
                dt = parse_iso(ce.get("published"))
                if dt:
                    dates[v.lower()] = dt
                    # NuGet marks unlisted packages with a sentinel year 1900 date.
                    if dt.year <= 1900:
                        unlisted.add(v.lower())
                if ce.get("deprecation"):
                    deprecated_any = True
                project_url = project_url or ce.get("projectUrl")
    except HttpError:
        pass

    releases = [
        Release(
            version=v,
            published=dates.get(v.lower()),
            prerelease="-" in v,
            yanked=v.lower() in unlisted,
        )
        for v in versions
    ]
    usable = [r.version for r in releases if not r.yanked]
    newest = latest_stable(usable)

    # projectUrl is a marketing homepage far more often than a repo
    # (newtonsoft.com, serilog.net), which left every NuGet package without an
    # upstream repo and therefore without velocity or an issue delta. The nuspec's
    # <repository url> element is the actual VCS location.
    repo = _nuspec_repo(ident, newest, ctx) or _repo_from(project_url)

    return Resolution(
        latest_stable=newest,
        releases=releases,
        repo=repo,
        homepage=project_url or f"https://www.nuget.org/packages/{c.name}",
        deprecated=deprecated_any,
        note="package has a deprecation notice on NuGet" if deprecated_any else None,
    )


_NUSPEC_REPO = re.compile(r"<repository\b[^>]*\burl=[\"']([^\"']+)[\"']", re.I)


def _nuspec_repo(ident: str, version: str | None, ctx: Context) -> str | None:
    """Read <repository url> from the package's nuspec."""
    if not version:
        return None
    url = (
        f"https://api.nuget.org/v3-flatcontainer/{quote(ident)}/"
        f"{quote(version.lower())}/{quote(ident)}.nuspec"
    )
    try:
        xml = ctx.client.get_text(url)
    except HttpError:
        return None
    m = _NUSPEC_REPO.search(xml or "")
    return _repo_from(m.group(1)) if m else None


def _relevant_pages(pages: list, current: str | None) -> list:
    """Registration pages are ranged; fetch only those we need."""
    if not pages:
        return []
    if len(pages) <= 2:
        return pages
    picked = [pages[-1]]  # newest
    if current:
        for p in pages:
            lo, hi = p.get("lower"), p.get("upper")
            if isinstance(lo, str) and isinstance(hi, str):
                if lo.lower() <= current.lower() <= hi.lower():
                    picked.append(p)
                    break
    return picked


# ----------------------------------------------------------------- Maven


def resolve_maven(c: Component, ctx: Context) -> Resolution:
    if ":" not in c.name:
        return Resolution(error=f"malformed coordinate {c.name!r}")
    group, _, artifact = c.name.partition(":")
    url = (
        "https://search.maven.org/solrsearch/select"
        f"?q=g:{quote(group)}+AND+a:{quote(artifact)}&core=gav&rows=200&wt=json"
    )
    try:
        body = ctx.client.get_json(url)
    except HttpError as e:
        return Resolution(error=str(e))
    docs = ((body or {}).get("response") or {}).get("docs") or []
    if not docs:
        return Resolution(note=f"{c.name} not found on Maven Central")

    releases = []
    for d in docs:
        v = d.get("v")
        if not isinstance(v, str):
            continue
        ts = d.get("timestamp")
        published = None
        if isinstance(ts, (int, float)) and ts > 0:
            published = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        releases.append(
            Release(
                version=v,
                published=published,
                prerelease=bool(
                    re.search(r"(?:-|\.)(alpha|beta|rc|m\d|snapshot|cr\d|pre)", v, re.I)
                ),
            )
        )
    stable = [r.version for r in releases if not r.prerelease]
    ordered = sort_versions(stable)
    # The repo is inferred from group prefixes, so confirm it exists before
    # attributing issues and sentiment to it -- a wrong repo yields a confident
    # "no activity" verdict about a project that is in fact busy.
    repo = _maven_repo_hint(group, artifact)
    if repo and ctx.github.enabled and ctx.github.api(f"repos/{repo}") is None:
        repo = None
    return Resolution(
        latest_stable=(ordered[-1].raw if ordered else latest_stable([r.version for r in releases])),
        releases=releases,
        repo=repo,
        homepage=f"https://central.sonatype.com/artifact/{group}/{artifact}",
        note=None if len(docs) < 200 else "version list truncated at 200",
    )


# Group prefix -> GitHub org, for the common ecosystems. Used to enable the issue
# delta; anything not listed simply gets no delta rather than a wrong repo.
_MAVEN_ORGS = {
    "org.springframework.boot": "spring-projects/spring-boot",
    "org.springframework.cloud": "spring-cloud",
    "org.springframework": "spring-projects/spring-framework",
    "com.fasterxml.jackson": "FasterXML",
    "io.quarkus": "quarkusio/quarkus",
    "org.apache.kafka": "apache/kafka",
    "org.apache.commons": "apache",
    "com.google.guava": "google/guava",
    "org.junit.jupiter": "junit-team/junit5",
    "org.mockito": "mockito/mockito",
    "io.micrometer": "micrometer-metrics/micrometer",
    "org.slf4j": "qos-ch/slf4j",
    "ch.qos.logback": "qos-ch/logback",
    "org.hibernate": "hibernate",
    "io.netty": "netty/netty",
    "com.squareup.okhttp3": "square/okhttp",
    "org.jetbrains.kotlin": "JetBrains/kotlin",
}


# Apache Commons drops the major-version suffix from the repo name:
# commons-lang3 -> apache/commons-lang. Guessing the artifact name verbatim
# produced apache/commons-lang3, which does not exist.
_APACHE_SUFFIX = re.compile(r"^(commons-[a-z]+?)\d+$")


def _maven_repo_hint(group: str, artifact: str) -> str | None:
    for prefix, target in _MAVEN_ORGS.items():
        if group == prefix or group.startswith(prefix + "."):
            if "/" not in target:
                m = _APACHE_SUFFIX.match(artifact)
                if m:
                    return f"{target}/{m.group(1)}"
            # A bare org means we know the owner but not the repo name.
            return target if "/" in target else f"{target}/{artifact}"
    return None


# ------------------------------------------------------- runtimes / toolchains

# Support-end dates for .NET target frameworks. These are the real risk: an
# out-of-support TFM receives no security patches regardless of package versions.
DOTNET_SUPPORT_END = {
    "5.0": "2022-05-10", "6.0": "2024-11-12", "7.0": "2024-05-14",
    "8.0": "2026-11-10", "9.0": "2026-05-12", "10.0": "2028-11-14",
    "3.1": "2022-12-13", "2.1": "2021-08-21",
    "4.6": "2022-04-26", "4.6.1": "2022-04-26", "4.7": None, "4.8": None,
}
DOTNET_CURRENT_LTS = "10.0"

# Java LTS lines with support-end dates. A supported LTS is a legitimate
# production choice, so these dates -- not the distance to the newest release --
# are what decide risk. Java 17 is 8 "majors" behind 25 and entirely fine.
JAVA_LTS = ("8", "11", "17", "21", "25")
JAVA_SUPPORT_END = {
    "8": "2030-12-31",
    "11": "2032-01-31",
    "17": "2029-09-30",
    "21": "2031-09-30",
    "25": "2033-09-30",
}
JAVA_CURRENT_LTS = "25"


def resolve_dotnet_tfm(c: Component, ctx: Context) -> Resolution:
    ver = c.current
    if not ver:
        return Resolution(note=f"unrecognised target framework moniker {c.name!r}")
    if c.name.lower().startswith("netstandard"):
        return Resolution(
            latest_stable=ver,
            note="netstandard is a compatibility surface, not a supported runtime; "
            "no upgrade target",
        )
    end = DOTNET_SUPPORT_END.get(ver, "unknown")
    out_of_support = False
    if isinstance(end, str):
        try:
            out_of_support = datetime.fromisoformat(end).replace(
                tzinfo=timezone.utc
            ) < ctx.now
        except ValueError:
            pass
    days_left = None
    if isinstance(end, str):
        try:
            days_left = (datetime.fromisoformat(end).replace(tzinfo=timezone.utc) - ctx.now).days
        except ValueError:
            pass
    return Resolution(
        latest_stable=DOTNET_CURRENT_LTS,
        releases=[Release(version=v) for v in sorted(DOTNET_SUPPORT_END)],
        homepage="https://dotnet.microsoft.com/platform/support/policy/dotnet-core",
        deprecated=out_of_support,
        note=(
            f"{c.name} support ended {end} - receiving no security patches"
            if out_of_support
            else (f"{c.name} supported until {end}"
                  + (f" ({days_left} days left)" if days_left and days_left > 0 else "")
                  if isinstance(end, str) else None)
        ),
        extra={"runtime": True, "support_end": end, "support_days_left": days_left},
    )


def resolve_jvm_toolchain(c: Component, ctx: Context) -> Resolution:
    if c.name == "gradle":
        body = ctx.github.api("repos/gradle/gradle/releases?per_page=40")
        if not isinstance(body, list) or not body:
            return Resolution(error="could not read gradle releases", repo="gradle/gradle")
        rels = [
            Release(
                version=(r.get("tag_name") or "").lstrip("v"),
                published=parse_iso(r.get("published_at")),
                prerelease=bool(r.get("prerelease")) or "-" in (r.get("tag_name") or ""),
            )
            for r in body
            if r.get("tag_name")
        ]
        ordered = sort_versions([r.version for r in rels if not r.prerelease])
        return Resolution(
            latest_stable=ordered[-1].raw if ordered else None,
            releases=rels,
            repo="gradle/gradle",
            homepage="https://gradle.org/releases/",
        )

    # Java: the meaningful target is the newest LTS, not the newest release.
    major = (c.current or "").split(".")[0]
    if major == "1":  # 1.8 style
        major = (c.current or "").split(".")[1] if "." in (c.current or "") else major
    is_lts = major in JAVA_LTS
    end = JAVA_SUPPORT_END.get(major)
    days_left = None
    if end:
        try:
            days_left = (
                datetime.fromisoformat(end).replace(tzinfo=timezone.utc) - ctx.now
            ).days
        except ValueError:
            pass
    return Resolution(
        latest_stable=JAVA_CURRENT_LTS,
        releases=[Release(version=v) for v in JAVA_LTS],
        homepage="https://www.oracle.com/java/technologies/java-se-support-roadmap.html",
        deprecated=bool(days_left is not None and days_left < 0),
        note=(
            f"Java {major} is an LTS line supported until {end}"
            + (f" ({days_left // 365} years, {days_left % 365 // 30} months left)"
               if days_left and days_left > 0 else "")
            if is_lts
            else f"Java {major} is not an LTS release (six-month support window); "
            f"move to {JAVA_CURRENT_LTS}"
        ),
        extra={
            "runtime": True,
            "lts": is_lts,
            "support_end": end,
            "support_days_left": days_left,
        },
    )


def resolve_dotnet_sdk(c: Component, ctx: Context) -> Resolution:
    """global.json SDK pin -- an exact pin blocks CI on newer machines."""
    band = ".".join((c.current or "").split(".")[:2])
    end = DOTNET_SUPPORT_END.get(band, "unknown")
    out_of_support = False
    if isinstance(end, str):
        try:
            out_of_support = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) < ctx.now
        except ValueError:
            pass
    notes = [f"global.json pins the SDK to {c.current}"]
    if not c.meta.get("rollForward"):
        notes.append(
            "no rollForward policy set, so a machine without this exact SDK fails "
            "the build; consider rollForward: latestFeature"
        )
    if out_of_support:
        notes.append(f"SDK band {band} support ended {end}")
    days_left = None
    if isinstance(end, str):
        try:
            days_left = (datetime.fromisoformat(end).replace(tzinfo=timezone.utc) - ctx.now).days
        except ValueError:
            pass
    return Resolution(
        latest_stable=DOTNET_CURRENT_LTS,
        homepage="https://dotnet.microsoft.com/platform/support/policy/dotnet-core",
        deprecated=out_of_support,
        note="; ".join(notes),
        extra={"runtime": True, "support_end": end, "support_days_left": days_left},
    )
