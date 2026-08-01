"""Command-line surface: argument shapes, output paths and exit codes.

Exit codes are the contract with CI. `--fail-on high` returning 0 when a
critical finding exists would turn a gate into decoration, and there is no way
to notice from a report that looks correct.

This file also pins `trend --json <file>`, which was a NameError until the
coverage pass that produced these tests -- the code path had no test at all and
only the `--json -` spelling was ever exercised by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftscope import cli as cli_mod
from driftscope.cli import (
    ECOSYSTEM_GROUPS,
    _parse_ecosystems,
    build_parser,
    main,
    resolve_out_path,
)
from driftscope.engine import exit_code_for
from driftscope.models import (
    Finding,
    Report,
    SEV_CRITICAL,
    SEV_CURRENT,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
)


class FakeSource:
    def __init__(self, display="/repo/estate"):
        self.display = display


def report_with(*severities) -> Report:
    from driftscope.models import Component, Lag, Resolution

    return Report(
        root="/repo",
        findings=[
            Finding(
                component=Component(
                    ecosystem="npm", name=f"pkg{i}", current="1.0.0",
                    source="package.json",
                ),
                resolution=Resolution(latest_stable="2.0.0"),
                lag=Lag(),
                severity=s,
            )
            for i, s in enumerate(severities)
        ],
    )


# ------------------------------------------------------------------ exit codes


def test_no_gate_is_always_success():
    """Without --fail-on the tool reports; it does not judge."""
    assert exit_code_for(report_with(SEV_CRITICAL), None) == 0


@pytest.mark.parametrize("gate,worst,expected", [
    ("high", SEV_CRITICAL, 1),   # critical is worse than the gate
    ("high", SEV_HIGH, 1),       # the gate itself trips
    ("high", SEV_MEDIUM, 0),     # below the gate
    ("critical", SEV_HIGH, 0),
    ("critical", SEV_CRITICAL, 1),
    ("low", SEV_LOW, 1),
    ("low", SEV_CURRENT, 0),
])
def test_the_gate_trips_at_and_above_its_threshold(gate, worst, expected):
    assert exit_code_for(report_with(worst), gate) == expected


def test_one_bad_finding_among_many_good_ones_still_fails():
    rep = report_with(SEV_CURRENT, SEV_LOW, SEV_CRITICAL, SEV_CURRENT)
    assert exit_code_for(rep, "high") == 1


def test_an_empty_report_passes_any_gate():
    assert exit_code_for(report_with(), "low") == 0


# ------------------------------------------------------------- ecosystem flags


def test_no_filter_means_everything():
    """None, not an empty set -- an empty set would scan nothing."""
    assert _parse_ecosystems(None) is None
    assert _parse_ecosystems([]) is None


def test_a_group_name_expands_to_its_members():
    assert _parse_ecosystems(["deps"]) == ECOSYSTEM_GROUPS["deps"]


def test_comma_separated_values_are_split():
    got = _parse_ecosystems(["npm,pypi"])
    assert got == {"npm", "pypi"}


def test_groups_and_bare_names_combine():
    got = _parse_ecosystems(["ci", "npm"])
    assert got == ECOSYSTEM_GROUPS["ci"] | {"npm"}


def test_repeated_flags_accumulate():
    got = _parse_ecosystems(["npm", "pypi"])
    assert got == {"npm", "pypi"}


def test_blank_entries_are_ignored_rather_than_scanning_nothing():
    """`--ecosystems ""` is a typo, and treating it as a real filter would
    silently produce an empty report that looks like a clean estate."""
    assert _parse_ecosystems([" , "]) is None


def test_overlapping_groups_do_not_duplicate():
    got = _parse_ecosystems(["dotnet", "jvm"])
    assert "nuget" in got and "maven" in got
    assert len(got) == len(set(got))


# -------------------------------------------------------------- output paths


def test_auto_names_the_file_after_what_was_scanned():
    p = resolve_out_path(cli_mod.AUTO, FakeSource("/repo/estate"), "html")
    assert p == Path("drift-estate.html")


def test_a_directory_target_gets_the_generated_name_appended(tmp_path):
    p = resolve_out_path(str(tmp_path), FakeSource("/repo/estate"), "json")
    assert p == tmp_path / "drift-estate.json"


def test_a_trailing_slash_is_treated_as_a_directory_even_if_absent(tmp_path):
    target = f"{tmp_path}/not-created-yet/"
    p = resolve_out_path(target, FakeSource("/repo/estate"), "md")
    assert p.name == "drift-estate.md"


def test_an_explicit_filename_is_used_verbatim(tmp_path):
    p = resolve_out_path(str(tmp_path / "custom.html"), FakeSource(), "html")
    assert p.name == "custom.html"


def test_a_remote_source_names_the_file_after_the_repo():
    p = resolve_out_path(
        cli_mod.AUTO, FakeSource("https://github.com/owner/repo"), "html"
    )
    assert p == Path("drift-owner-repo.html")


def test_an_ado_source_names_the_file_after_org_project_repo():
    """Regression: the generic remote regex read an ADO path as `{project}/_git`,
    so every repo in a project wrote to the same filename."""
    p = resolve_out_path(
        cli_mod.AUTO,
        FakeSource("https://dev.azure.com/contoso/Platform/_git/payments"),
        "html",
    )
    assert p == Path("drift-contoso-Platform-payments.html")


# ------------------------------------------------------------------- parser


def test_scan_defaults_are_conservative():
    args = build_parser().parse_args(["scan", "."])

    assert args.fail_on is None, "the gate must be opt-in"
    assert args.offline is False
    assert args.velocity is False, "velocity costs API calls and is opt-in"
    assert args.workers == 8


@pytest.mark.parametrize("flag", ["--offline", "--no-github", "--no-cache", "--quiet"])
def test_the_restraint_flags_parse(flag):
    args = build_parser().parse_args(["scan", ".", flag])
    assert args


def test_an_unknown_subcommand_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nonsense"])


def test_a_remote_scan_without_byo_config_resolves_to_no_identity(monkeypatch):
    """Regression: `_resolve_identity` on a REMOTE target with no --identity, no
    --forge-auth, and no $DRIFTSCOPE_FORGE_AUTH must return None -- the default flow. This
    once raised `NameError: name 'os' is not defined` (a missing import), crashing every
    plain remote CLI scan; the unit path was uncovered because tests passed a registry.
    """
    monkeypatch.delenv("DRIFTSCOPE_FORGE_AUTH", raising=False)
    args = build_parser().parse_args(["scan", "https://dev.azure.com/x/y/_git/z"])
    assert cli_mod._resolve_identity(args) is None


def test_the_forge_auth_env_var_is_read_when_no_flag(monkeypatch, tmp_path):
    """The $DRIFTSCOPE_FORGE_AUTH branch is exercised (the exact line that had the bug):
    with it set to a real registry and no --identity, a remote target auto-resolves.
    """
    reg = tmp_path / "fa.json"
    reg.write_text(json.dumps({
        "version": 1,
        "identities": [{
            "name": "acme", "forge": "github",
            "match": {"hosts": ["github.com"], "owners": ["acme"]},
            "github": {"app_id": "1", "installation_id": "2",
                       "signing_key": {"vault_uri": "https://v.vault.azure.net", "key_name": "k"}},
        }],
    }))
    monkeypatch.setenv("DRIFTSCOPE_FORGE_AUTH", str(reg))
    args = build_parser().parse_args(["scan", "https://github.com/acme/widgets"])
    ident = cli_mod._resolve_identity(args)
    assert ident is not None and ident.name == "acme"


def test_an_invalid_fail_on_level_is_rejected_at_parse_time():
    """A typo'd threshold must not silently become "never fail"."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["scan", ".", "--fail-on", "catastrophic"])


def test_ecosystems_subcommand_lists_the_groups(capsys):
    assert main(["ecosystems"]) == 0
    out = capsys.readouterr().out

    assert "deps" in out and "containers" in out
    assert "live-only" in out


def test_no_subcommand_is_an_error_not_a_scan():
    with pytest.raises(SystemExit):
        main([])


# --------------------------------------------------------------------- trend


@pytest.fixture
def history_dir(tmp_path):
    """Two runs, the second worse than the first."""
    from driftscope import history

    d = tmp_path / "hist"
    d.mkdir()
    for i, (name, counts) in enumerate([
        ("2026-07-01T00-00-00Z.json", {"critical": 1, "high": 2}),
        ("2026-07-20T00-00-00Z.json", {"critical": 3, "high": 2}),
    ]):
        (d / name).write_text(json.dumps({
            "schema": "driftscope/1",
            "generated": f"2026-07-{1 + i * 19:02d}T00:00:00Z",
            "summary": counts,
            "findings": [],
        }))
    assert len(history.list_runs(d)) == 2
    return d


def test_trend_list_prints_each_run(history_dir, capsys):
    assert main(["trend", str(history_dir), "--list"]) == 0
    out = capsys.readouterr().out

    assert "2026-07-01" in out and "2026-07-20" in out


def test_listing_an_empty_history_is_an_error_not_an_empty_success(tmp_path, capsys):
    """Exit 0 with no output would read as "no drift" in CI."""
    empty = tmp_path / "none"
    empty.mkdir()
    assert main(["trend", str(empty), "--list"]) == 2


def test_one_run_cannot_be_compared_with_itself(tmp_path, capsys):
    from driftscope import history

    d = tmp_path / "one"
    d.mkdir()
    (d / "2026-07-01T00-00-00Z.json").write_text(json.dumps(
        {"schema": "driftscope/1", "generated": "2026-07-01T00:00:00Z",
         "summary": {}, "findings": []}))
    assert main(["trend", str(d)]) == 2
    assert "at least two runs" in capsys.readouterr().err


def test_trend_writes_json_to_a_named_file(history_dir, tmp_path):
    """Regression: this path referenced an undefined `src` and raised NameError.
    Only `--json -` had ever been exercised, and that branch returns early."""
    out = tmp_path / "trend.json"
    assert main(["trend", str(history_dir), "--json", str(out)]) == 0

    body = json.loads(out.read_text())
    assert "from_run" in body and "to_run" in body


def test_trend_json_to_stdout_still_works(history_dir, capsys):
    assert main(["trend", str(history_dir), "--json", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["to_run"]


def test_trend_json_to_a_directory_generates_a_name(history_dir, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    assert main(["trend", str(history_dir), "--json", str(dest)]) == 0

    written = list(dest.glob("*.json"))
    assert len(written) == 1 and written[0].name.startswith("drift-trend-")


def test_trend_text_output_is_the_default(history_dir, capsys):
    assert main(["trend", str(history_dir)]) == 0
    assert capsys.readouterr().out.strip()


def test_an_unreadable_run_is_an_error(history_dir, tmp_path, capsys):
    (history_dir / "2026-07-20T00-00-00Z.json").write_text("{ truncated")
    assert main(["trend", str(history_dir)]) == 2
    assert "could not read" in capsys.readouterr().err


# ------------------------------------------------------------------ interrupt


def test_ctrl_c_during_a_scan_exits_130_not_a_traceback(monkeypatch):
    """130 is the shell convention for SIGINT; a traceback would look like a bug
    in the tool rather than a deliberate stop."""
    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "cmd_scan", interrupted)
    assert main(["scan", "."]) == 130
