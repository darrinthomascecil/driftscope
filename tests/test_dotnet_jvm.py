"""NuGet / Maven scanner and resolver tests.

The fixture is the real repo used for the end-to-end test (github.com/
darrinthomascecil/driftscope-testapp -- both apps compile in CI), plus edge cases
that a minimal compiling app does not exercise: Central Package Management,
multi-targeting, the legacy packages.config format, Gradle, and unresolved
`${property}` / `$gradleVar` indirection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from driftscope.models import (
    DOTNET_SDK,
    DOTNET_TFM,
    JVM_TOOLCHAIN,
    MAVEN,
    NUGET,
    Component,
    Release,
    Resolution,
)
from driftscope.scanners import scan_tree

FIXTURE = Path(__file__).parent.parent / "fixtures" / "dotnet-jvm"


@pytest.fixture(scope="module")
def comps():
    got, files = scan_tree(FIXTURE)
    assert files >= 7
    return got


def find(cs, eco, name):
    for c in cs:
        if c.ecosystem == eco and c.name.lower() == name.lower():
            return c
    return None


def of(cs, eco):
    return [c for c in cs if c.ecosystem == eco]


# --------------------------------------------------------------- NuGet scan


def test_packagereference_attribute_version(comps):
    c = find(comps, NUGET, "Newtonsoft.Json")
    assert c is not None and c.current == "12.0.3"


def test_packagereference_child_element_version(comps):
    """<PackageReference><Version>2.10.0</Version></PackageReference>"""
    versions = {c.current for c in of(comps, NUGET) if c.name == "Serilog"}
    assert "2.10.0" in versions, "child-element form must be parsed"


def test_central_package_management(comps):
    """A version-less PackageReference is centrally pinned, not unpinned."""
    refs = [c for c in of(comps, NUGET) if c.name == "Dapper"]
    assert refs, "Dapper should be found"
    assert any(c.meta.get("central") for c in refs), "must be marked central"
    defn = next((c for c in refs if c.meta.get("central_definition")), None)
    assert defn is not None and defn.current == "2.0.123"


def test_msbuild_property_is_expanded(comps):
    """Version="$(XunitVersion)" resolves from the same file's PropertyGroup."""
    c = find(comps, NUGET, "xunit")
    assert c is not None and c.current == "2.4.2"


def test_legacy_packages_config(comps):
    c = find(comps, NUGET, "EntityFramework")
    assert c is not None and c.current == "6.2.0"
    assert c.meta["legacy_format"] is True


def test_multi_targeting_yields_each_tfm(comps):
    tfms = {c.name for c in of(comps, DOTNET_TFM)}
    assert {"net6.0", "net8.0"} <= tfms, "TargetFrameworks must split on ;"


def test_tfm_version_extracted(comps):
    c = find(comps, DOTNET_TFM, "net8.0")
    assert c is not None and c.current == "8.0"


def test_global_json_sdk(comps):
    c = find(comps, DOTNET_SDK, "dotnet-sdk")
    assert c is not None and c.current == "8.0.100"
    assert c.meta["rollForward"] == "latestMajor"


# --------------------------------------------------------------- Maven scan


def test_pom_dependency(comps):
    c = find(comps, MAVEN, "org.apache.commons:commons-lang3")
    assert c is not None and c.current == "3.9"


def test_pom_property_indirection_resolved(comps):
    """<version>${jackson.version}</version> with the property in the same file."""
    c = find(comps, MAVEN, "com.fasterxml.jackson.core:jackson-databind")
    assert c is not None and c.current == "2.15.2"


def test_java_release_property(comps):
    c = find(comps, JVM_TOOLCHAIN, "java")
    assert c is not None
    assert c.current in ("17", "11")  # pom says 17; gradle file says 11


def test_gradle_dependencies(comps):
    c = find(comps, MAVEN, "com.google.guava:guava")
    assert c is not None and c.current == "31.1-jre"
    j = find(comps, MAVEN, "org.junit.jupiter:junit-jupiter")
    assert j is not None and j.current == "5.9.0"
    assert j.meta["build_system"] == "gradle"


def test_gradle_variable_version_skipped(comps):
    """`$slf4jVersion` is not resolvable from the build file; do not invent one."""
    assert find(comps, MAVEN, "org.slf4j:slf4j-api") is None


def test_gradle_wrapper_version(comps):
    c = find(comps, JVM_TOOLCHAIN, "gradle")
    assert c is not None and c.current == "7.4.2"


# --------------------------------------------------------- runtime severity


def _ctx(now):
    from driftscope.http import Cache, Client, GitHub
    from driftscope.resolvers import Context

    cache = Cache(root=Path("/tmp/driftscope-test-cache"), enabled=False)
    return Context(
        client=Client(cache=cache, offline=True),
        github=GitHub(cache=cache, offline=True, enabled=False),
        allow_az=False,
        now=now,
    )


def test_supported_java_lts_is_not_critical():
    """Regression: Java 17 scored CRITICAL at "8 majors behind" -- it is a
    supported LTS and rating it CRITICAL trains people to ignore the report."""
    from datetime import datetime, timezone

    from driftscope import advisor
    from driftscope.resolvers.dotnet_jvm import resolve_jvm_toolchain
    from driftscope.version import lag_between

    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    c = Component(ecosystem=JVM_TOOLCHAIN, name="java", current="17", source="pom.xml")
    r = resolve_jvm_toolchain(c, _ctx(now))
    assert r.extra["lts"] is True
    assert r.extra["support_days_left"] > 1000
    sev = advisor.score(c, r, lag_between("17", r.latest_stable))
    assert sev == "low", f"supported LTS must not be critical, got {sev}"


def test_out_of_support_runtime_is_critical():
    from datetime import datetime, timezone

    from driftscope import advisor
    from driftscope.resolvers.dotnet_jvm import resolve_dotnet_tfm
    from driftscope.version import lag_between

    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    c = Component(ecosystem=DOTNET_TFM, name="net6.0", current="6.0", source="x.csproj")
    r = resolve_dotnet_tfm(c, _ctx(now))
    assert r.deprecated is True
    assert "support ended" in (r.note or "")
    assert advisor.score(c, r, lag_between("6.0", r.latest_stable)) == "critical"


def test_expiring_soon_runtime_is_high():
    """net8.0 support ends 2026-11-10 -- inside the 180-day window."""
    from datetime import datetime, timezone

    from driftscope import advisor
    from driftscope.resolvers.dotnet_jvm import resolve_dotnet_tfm
    from driftscope.version import lag_between

    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    c = Component(ecosystem=DOTNET_TFM, name="net8.0", current="8.0", source="x.csproj")
    r = resolve_dotnet_tfm(c, _ctx(now))
    assert r.deprecated is False
    assert advisor.score(c, r, lag_between("8.0", r.latest_stable)) == "high"


def test_netstandard_has_no_upgrade_target():
    from datetime import datetime, timezone

    from driftscope.resolvers.dotnet_jvm import resolve_dotnet_tfm

    c = Component(
        ecosystem=DOTNET_TFM, name="netstandard2.1", current="2.1", source="x.csproj"
    )
    r = resolve_dotnet_tfm(c, _ctx(datetime(2026, 7, 26, tzinfo=timezone.utc)))
    assert "compatibility surface" in (r.note or "")


# ---------------------------------------------------------------- advice


def test_nuget_advice_is_dotnet_specific():
    from driftscope import advisor
    from driftscope.version import lag_between

    c = Component(ecosystem=NUGET, name="Newtonsoft.Json", current="12.0.3", source="a.csproj")
    r = Resolution(latest_stable="13.0.4", releases=[Release(version="13.0.4")])
    a = advisor.advise(c, r, lag_between("12.0.3", "13.0.4"), "high")
    joined = " ".join(a.steps + a.cautions)
    assert "dotnet restore" in joined
    assert "NU1605" in joined or "NU1901" in joined
    assert "target framework floor" in joined


def test_central_package_advice_points_at_the_props_file():
    from driftscope import advisor
    from driftscope.version import lag_between

    c = Component(ecosystem=NUGET, name="Dapper", current="2.0.123", source="a.csproj",
                  meta={"central": True})
    r = Resolution(latest_stable="2.1.66", releases=[Release(version="2.1.66")])
    a = advisor.advise(c, r, lag_between("2.0.123", "2.1.66"), "medium")
    assert any("Directory.Packages.props" in s for s in a.steps)


def test_maven_advice_is_maven_specific():
    from driftscope import advisor
    from driftscope.version import lag_between

    c = Component(ecosystem=MAVEN, name="com.fasterxml.jackson.core:jackson-databind",
                  current="2.15.2", source="pom.xml")
    r = Resolution(latest_stable="2.19.0", releases=[Release(version="2.19.0")])
    a = advisor.advise(c, r, lag_between("2.15.2", "2.19.0"), "medium")
    joined = " ".join(a.steps + a.cautions)
    assert "mvn" in joined
    assert "nearest-wins" in joined
    assert "BOM" in joined


def test_maven_parent_advice_warns_about_fan_out():
    from driftscope import advisor
    from driftscope.version import lag_between

    c = Component(ecosystem=MAVEN, name="org.springframework.boot:spring-boot-starter-parent",
                  current="2.7.0", source="pom.xml", meta={"parent": True})
    r = Resolution(latest_stable="3.5.3", releases=[Release(version="3.5.3")])
    a = advisor.advise(c, r, lag_between("2.7.0", "3.5.3"), "high")
    assert "fan-out" in a.strategy


def test_note_is_not_duplicated_into_cautions():
    """Regression: res.note appeared both as a caution and as its own line."""
    from driftscope import advisor
    from driftscope.version import lag_between

    c = Component(ecosystem=NUGET, name="X", current="1.0.0", source="a.csproj")
    r = Resolution(latest_stable="2.0.0", note="a distinctive note string")
    a = advisor.advise(c, r, lag_between("1.0.0", "2.0.0"), "high")
    assert not any("a distinctive note string" in x for x in a.cautions)


# ------------------------------------------------------------ integration


@pytest.mark.network
def test_real_nuget_resolution():
    from driftscope.engine import build_context
    from driftscope.resolvers.dotnet_jvm import resolve_nuget

    ctx = build_context(no_github=True, no_az=True)
    c = Component(ecosystem=NUGET, name="Newtonsoft.Json", current="12.0.3", source="x")
    r = resolve_nuget(c, ctx)
    assert r.error is None, r.error
    assert r.latest_stable and r.latest_stable.startswith("13.")
    cur = r.release_for("12.0.3")
    assert cur is not None and cur.published is not None, "expected a publish date"


@pytest.mark.network
def test_real_maven_resolution():
    from driftscope.engine import build_context
    from driftscope.resolvers.dotnet_jvm import resolve_maven

    ctx = build_context(no_github=True, no_az=True)
    c = Component(
        ecosystem=MAVEN, name="org.apache.commons:commons-lang3", current="3.9", source="x"
    )
    r = resolve_maven(c, ctx)
    assert r.error is None, r.error
    assert r.latest_stable and r.latest_stable.startswith("3.")
    dated = [x for x in r.releases if x.published]
    assert dated, "Maven Central supplies timestamps for every version"
