"""Golden-output tests for the three reporters.

These exist to make refactors provably output-preserving. They assert nothing
about whether the output is *good* -- only that it has not changed. When a change
to a report is intended, review the diff and regenerate:

    DRIFTSCOPE_UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/test_golden.py

A diff in one of these files is the whole point; do not regenerate without reading
it. The fixture in `_report_fixture.py` deliberately exercises every reporter
branch, so a diff localises to the branch that moved.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from driftscope.report.html import to_html
from driftscope.report.render import to_json, to_markdown, to_text

from ._report_fixture import build_ado_report, build_report

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("DRIFTSCOPE_UPDATE_GOLDEN") == "1"

# The reporters stamp wall-clock generation time, which cannot be pinned from
# outside. Everything else in the fixture is a fixed literal.
_VOLATILE = (
    (re.compile(r'"generated_at": "[^"]*"'), '"generated_at": "<SCRUBBED>"'),
    (re.compile(r"\*\*Generated:\*\* .*"), "**Generated:** <SCRUBBED>"),
    (re.compile(r"<time[^>]*>[^<]*</time>"), "<time><SCRUBBED></time>"),
    (re.compile(r"Generated [^<·]*(?=<|·)"), "Generated <SCRUBBED>"),
)


def scrub(text: str) -> str:
    for pattern, repl in _VOLATILE:
        text = pattern.sub(repl, text)
    return text


def check(name: str, actual: str) -> None:
    path = GOLDEN / name
    actual = scrub(actual)
    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        if not UPDATE:
            pytest.skip(f"created missing golden {name}; re-run to assert against it")
        return
    expected = path.read_text()
    if actual != expected:
        # A unified diff is far more useful here than a 30KB equality dump.
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(), actual.splitlines(),
                fromfile=f"golden/{name}", tofile=f"actual/{name}", lineterm="",
            )
        )
        pytest.fail(
            f"{name} changed. Review the diff; if intended, regenerate with "
            f"DRIFTSCOPE_UPDATE_GOLDEN=1.\n\n{diff[:20000]}"
        )


@pytest.fixture(scope="module")
def report():
    return build_report()


def test_json_golden(report):
    check("report.json", to_json(report))


def test_markdown_golden(report):
    check("report.md", to_markdown(report))


def test_html_golden(report):
    check("report.html", to_html(report))


def test_html_golden_azure_devops():
    """Same findings, scanned from Azure DevOps: only the permalinks may differ."""
    check("report-ado.html", to_html(build_ado_report()))


def test_ado_permalinks_differ_from_github_only_in_shape():
    """A guard on the guard: if the two goldens were ever identical, the ADO one
    would be asserting nothing."""
    gh, ado = to_html(build_report()), to_html(build_ado_report())
    assert gh != ado
    assert "dev.azure.com/contoso/Platform/_git/payments" in ado
    assert "github.com/darrinthomascecil" not in ado
    # Anchored to the scanned commit, not a branch, on both forges.
    assert "version=GC0123456789abcdef0123456789abcdef01234567" in ado


def test_text_golden(report):
    check("report.txt", to_text(report))


def test_fixture_covers_every_branch(report):
    """Guards the fixture itself: if a future edit drops one of these shapes, the
    goldens silently stop covering that reporter branch."""
    sevs = {f.severity for f in report.findings}
    assert sevs == {"critical", "high", "medium", "low", "current", "unknown"}, sevs

    vulns = [f.vulns for f in report.findings if f.vulns is not None]
    assert any(v.indiscriminate for v in vulns), "indiscriminate case missing"
    assert any(v.unmatched for v in vulns), "unmatched case missing"
    assert any(
        not v.indiscriminate and not v.unmatched and v.fixed_count and v.introduced_count
        for v in vulns
    ), "normal fixed/remaining/introduced case missing"
    assert any(v.detail_truncated for v in vulns), "truncated detail list missing"

    vel = [f.velocity for f in report.findings if f.velocity][0]
    assert any(not w.complete for w in vel.windows), "incomplete window missing"
    assert vel.windows[-1].frustration is None, "None-frustration window missing"

    assert report.trend is not None
    assert len(report.trend.worsened) > 12, "12-row cap not exercised"
    assert report.source and report.source.get("commit"), "blob permalinks not exercised"


def test_findings_cite_their_source(report):
    """Every resolvable finding shows where 'how far behind' was determined -- the
    k8s one must point at the Kubernetes deprecation guide specifically."""
    from driftscope.report.render import source_of

    k8s = [f for f in report.findings if f.component.ecosystem == "k8s-api"][0]
    label, url = source_of(k8s.component, k8s.resolution)
    assert "deprecat" in label.lower() and "kubernetes.io" in url

    go = [f for f in report.findings if f.component.ecosystem == "go-module"][0]
    assert source_of(go.component, go.resolution)[1] == "https://proxy.golang.org"
