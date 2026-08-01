"""The per-issue analysis + impact ranking in the delta phase. No network.

analyze_issue() reads only fields the /search/issues response already carries and
turns each item into a scored IssueSignal; compute_delta ranks the window on that
score. These tests pin the classification (severity / fix-status / affected version)
and, most importantly, that the ranking leads with the highest-impact issue rather
than the newest -- the behaviour change this feature exists for.
"""

from __future__ import annotations

from datetime import datetime, timezone

from driftscope.forge import ReleaseRecord
from driftscope.ghub.delta import analyze_issue, compute_delta
from driftscope.models import Release, Resolution


def item(**kw) -> dict:
    """A raw GitHub search item with sensible defaults; override per test."""
    base = {
        "number": 1, "title": "something", "html_url": "https://x/1",
        "state": "open", "labels": [], "created_at": "2025-03-01T00:00:00Z",
        "body": "", "comments": 0, "reactions": {"total_count": 0},
    }
    base.update(kw)
    return base


# --------------------------------------------------------------- severity


def test_a_bug_label_alone_is_only_normal():
    assert analyze_issue(item(labels=[{"name": "bug"}])).severity == "normal"


def test_a_security_or_regression_label_is_critical():
    for lbl in ("security", "regression", "priority: critical", "sev1"):
        sig = analyze_issue(item(labels=[{"name": "bug"}, {"name": lbl}]))
        assert sig.severity == "critical", lbl


def test_crash_wording_in_the_body_lifts_severity_without_a_label():
    sig = analyze_issue(item(title="app dies on startup",
                             body="It segfaults immediately, total data loss."))
    assert sig.severity == "critical"


def test_plain_failure_wording_is_high_not_critical():
    sig = analyze_issue(item(title="Export throws an exception on empty input"))
    assert sig.severity == "high"


# --------------------------------------------------------------- fix status


def test_closed_completed_is_fixed_and_open_is_open():
    assert analyze_issue(item(state="open")).fix_status == "open"
    assert analyze_issue(
        item(state="closed", state_reason="completed")).fix_status == "fixed"


def test_not_planned_is_dismissed_and_demoted():
    common = dict(reactions={"+1": 20, "total_count": 20})
    dismissed = analyze_issue(item(state="closed", state_reason="not_planned", **common))
    still_open = analyze_issue(item(state="open", **common))
    assert dismissed.fix_status == "dismissed"
    # A won't-fix must rank below the very same issue left open: triaged-as-not-a-bug
    # is a demotion, so it does not float up on reaction count alone.
    assert dismissed.impact < still_open.impact


# --------------------------------------------------------------- affected version


def test_affected_version_from_a_version_milestone():
    sig = analyze_issue(item(milestone={"title": "2.18.1"}))
    assert sig.affected_version == "2.18.1"


def test_affected_version_mined_from_body_wording():
    sig = analyze_issue(item(body="This is broken in 3.2.0 and later."))
    assert sig.affected_version == "3.2.0"


def test_a_non_version_milestone_is_not_treated_as_a_version():
    assert analyze_issue(item(milestone={"title": "Backlog"})).affected_version is None


# --------------------------------------------------------------- reactions / excerpt


def test_only_positive_reactions_count_toward_impact():
    sig = analyze_issue(item(reactions={"+1": 10, "heart": 2, "-1": 5,
                                        "confused": 3, "total_count": 20}))
    assert sig.reactions == 12  # +1 and heart, not the negative ones


def test_excerpt_skips_template_headings_and_html_comments():
    body = "<!-- please fill this in -->\nSteps to reproduce:\nThe real first line here."
    assert analyze_issue(item(body=body)).body_excerpt == "The real first line here."


# --------------------------------------------------------------- ranking (the point)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class _RankForge:
    """A forge whose search returns three issues in a deliberately unhelpful order:
    the highest-impact one is NOT the newest, so a created-desc listing would bury it."""

    kind = "github"
    enabled = True
    last_query = None

    def releases(self, repo):
        return [
            ReleaseRecord(tag="1.0.0", published=_dt("2025-01-10T00:00:00"), body=""),
            ReleaseRecord(tag="2.0.0", published=_dt("2025-09-01T00:00:00"), body=""),
        ]

    def issue_counts(self, repo, day_from, day_to, **k):
        return (3, 1)

    @staticmethod
    def label_clause():
        return 'label:"bug"'

    def search_issues(self, query, *, limit=15, sort="created"):
        self.last_query = query
        return [
            # newest, but trivial -> should NOT lead
            item(number=3, title="typo in log message", created_at="2025-08-01T00:00:00Z"),
            # oldest, but a fixed crash with heavy reactions -> should lead
            item(number=1, title="crash on load", state="closed",
                 state_reason="completed", labels=[{"name": "bug"}],
                 reactions={"+1": 40, "total_count": 40}, comments=12,
                 created_at="2025-02-01T00:00:00Z"),
            # middle
            item(number=2, title="export fails intermittently",
                 reactions={"+1": 3, "total_count": 3},
                 created_at="2025-05-01T00:00:00Z"),
        ]

    def web_url(self, repo):
        return "x"


def _resolution():
    return Resolution(
        latest_stable="2.0.0",
        releases=[
            Release(version="1.0.0", published=_dt("2025-01-10T00:00:00")),
            Release(version="2.0.0", published=_dt("2025-09-01T00:00:00")),
        ],
        repo="o/r",
    )


def test_the_window_is_ranked_by_impact_not_recency():
    forge = _RankForge()
    d = compute_delta(forge, "o/r", "1.0.0", "2.0.0", _resolution())
    nums = [i.number for i in d.bug_issues]
    assert nums == [1, 2, 3], (
        "the fixed high-reaction crash must lead despite being the oldest"
    )
    top = d.bug_issues[0]
    assert top.severity == "critical" and top.fix_status == "fixed"


def test_closed_issues_are_included_now():
    """The old query pinned `is:open`, which hid fixed regressions -- the strongest
    signal. The listing must now carry closed issues too."""
    forge = _RankForge()
    d = compute_delta(forge, "o/r", "1.0.0", "2.0.0", _resolution())
    assert "is:open" not in (forge.last_query or "")
    assert any(i.fix_status == "fixed" for i in d.bug_issues)
