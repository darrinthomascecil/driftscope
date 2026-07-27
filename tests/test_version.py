"""Version algebra tests. Pure, no network."""

from __future__ import annotations

import pytest

from driftscope.version import (
    CALVER,
    K8SAPI,
    OPAQUE,
    SEMVER,
    comparable,
    compare,
    constraint_floor,
    is_pinned,
    lag_between,
    latest_stable,
    parse,
)


@pytest.mark.parametrize(
    "raw,kind,release",
    [
        ("1.2.3", SEMVER, (1, 2, 3)),
        ("v4.5.0", SEMVER, (4, 5, 0)),
        ("3.75.0", SEMVER, (3, 75, 0)),
        ("2025-06-01", CALVER, (2025, 6, 1)),
        ("2023-04-01-preview", CALVER, (2023, 4, 1)),
        ("v1", K8SAPI, (1,)),
        ("v2beta2", K8SAPI, (2,)),
        ("1.0.0.4", SEMVER, (1, 0, 0, 4)),
    ],
)
def test_parse_kinds(raw, kind, release):
    v = parse(raw)
    assert v.kind == kind
    assert v.release == release


def test_prerelease_detection():
    assert parse("3.0.0-rc.1").is_prerelease
    assert parse("2.0.0-beta1").is_prerelease
    assert parse("2023-04-01-preview").is_prerelease
    assert parse("v1beta1").is_prerelease
    assert not parse("1.2.3").is_prerelease
    assert not parse("2025-06-01").is_prerelease


def test_docker_variants_are_isolated():
    slim = parse("3.12-slim")
    alpine = parse("3.12-alpine")
    assert slim.variant == "slim"
    assert alpine.variant == "alpine"
    ok, why = comparable(slim, alpine)
    assert not ok and "variant" in why


def test_scheme_mismatch_refused():
    ok, why = comparable(parse("1.2.3"), parse("2025-06-01"))
    assert not ok and "scheme" in why


def test_ordering():
    assert compare(parse("1.2.3"), parse("1.10.0")) == -1
    assert compare(parse("2.0.0"), parse("2.0.0-rc.1")) == 1  # release beats rc
    assert compare(parse("1.2"), parse("1.2.0")) == 0
    assert compare(parse("2025-09-01"), parse("2025-06-01")) == 1


def test_latest_stable_skips_prereleases():
    assert latest_stable(["1.0.0", "2.0.0-rc.1", "1.9.0"]) == "1.9.0"
    # Falls back to a prerelease only when there is no stable release at all.
    assert latest_stable(["2.0.0-rc.1"]) == "2.0.0-rc.1"


def test_azure_api_calver_ordering():
    versions = ["2024-10-01", "2025-06-01", "2026-05-01", "2026-06-01-preview"]
    assert latest_stable(versions) == "2026-05-01"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("~> 3.0", "3.0"),
        (">= 1.3", "1.3"),
        ("==2.20.0", "2.20.0"),
        ("^4.16.0", "4.16.0"),
        (">=1.5.0", "1.5.0"),
        # A bare upper bound is a ceiling, not the version in use: "<2" means the
        # project is capped below 2.x, so reporting "2" would invert the truth.
        ("<2", None),
        ("<=1.9", None),
        (">=1.0,<2", "1.0"),
        ("*", None),
        ("", None),
    ],
)
def test_constraint_floor(raw, expected):
    assert constraint_floor(raw) == expected


def test_is_pinned():
    assert is_pinned("==2.20.0")
    assert is_pinned("3.4.3")
    assert not is_pinned("~> 3.0")
    assert not is_pinned("^4.16.0")


def test_lag_major_minor_patch():
    lag = lag_between("3.0", "4.51.0")
    assert lag.comparable
    assert lag.behind_major == 1
    # minor/patch are only meaningful within the same major
    assert lag.behind_minor == 0


def test_lag_same_major_counts_minor():
    lag = lag_between("1.2.0", "1.9.4")
    assert lag.behind_major == 0
    assert lag.behind_minor == 7


def test_lag_not_comparable_is_flagged():
    lag = lag_between("3.12-slim", "3.13-alpine")
    assert not lag.comparable
    assert "variant" in lag.reason


def test_lag_counts_releases_behind():
    from driftscope.models import Release

    releases = [Release(version=v) for v in ("1.0.0", "1.1.0", "1.2.0", "2.0.0-rc.1", "2.0.0")]
    lag = lag_between("1.0.0", "2.0.0", releases=releases)
    assert lag.releases_behind == 4
    assert lag.stable_releases_behind == 3  # the rc does not count


def test_lag_days_behind():
    from datetime import datetime, timedelta, timezone

    old = datetime(2022, 1, 1, tzinfo=timezone.utc)
    new = old + timedelta(days=800)
    lag = lag_between("1.0.0", "2.0.0", current_published=old, latest_published=new)
    assert lag.days_behind == 800


def test_current_version_not_negative():
    lag = lag_between("5.0.0", "4.0.0")  # ahead of latest (e.g. prerelease pin)
    assert lag.behind_major == 0
    assert lag.comparable


def test_opaque_versions_refused():
    assert parse("latest").kind == OPAQUE
    assert not parse("latest").valid


def test_calver_dates_derived_from_the_version_itself():
    """Azure API versions carry no registry timestamps, but the version IS a date."""
    from datetime import datetime, timezone

    lag = lag_between("2021-10-01", "2026-02-01", now=datetime(2026, 7, 26, tzinfo=timezone.utc))
    assert lag.days_behind == 1584           # 2021-10-01 -> 2026-02-01
    assert lag.current_age_days == 1759      # 2021-10-01 -> today
    assert lag.behind_major == 5             # five calendar years


def test_supplied_dates_win_over_derived_calver_dates():
    from datetime import datetime, timezone

    real = datetime(2022, 1, 1, tzinfo=timezone.utc)
    lag = lag_between("2021-10-01", "2026-02-01", current_published=real)
    # 2022-01-01 -> 2026-02-01, not 2021-10-01 -> 2026-02-01
    assert lag.days_behind == 1492


def test_semver_gets_no_derived_dates():
    lag = lag_between("1.0.0", "2.0.0")
    assert lag.days_behind is None


@pytest.mark.parametrize(
    "tag,variant",
    [
        ("18-slim", "slim"),
        ("26.5-slim", "slim"),
        # The distro qualifier is part of the variant: bullseye-slim pins an older
        # Debian base, so it is not an upgrade target for a plain -slim pin.
        ("26.5-bullseye-slim", "bullseye-slim"),
        ("26.5.0-trixie", "trixie"),
        ("3.12-alpine", "alpine"),
        ("1.2.3", ""),
        ("alpine", "alpine"),
        ("trixie-slim", "trixie-slim"),
    ],
)
def test_variant_is_the_whole_suffix_chain(tag, variant):
    assert parse(tag).variant == variant


def test_plain_slim_does_not_compare_against_distro_qualified_slim():
    """Regression: node 18-slim resolved to 26.5-bullseye-slim, an older base."""
    plain, qualified = parse("26.5-slim"), parse("26.5-bullseye-slim")
    ok, why = comparable(plain, qualified)
    assert not ok and "variant" in why
    # and a plain pin still compares fine against a plain candidate
    ok2, _ = comparable(parse("18-slim"), plain)
    assert ok2
