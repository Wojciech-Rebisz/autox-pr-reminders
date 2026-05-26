# ruff: noqa: D103
"""Unit tests for pr_review_reminder.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ..pr_review_reminder import (
    PullRequestReminder,
    build_slack_payload,
    determine_action,
    format_actor,
    has_ignore_label,
    idle_hours,
    is_snoozed,
    needs_review,
    parse_github_slack_map,
    resolve_repositories,
)

_REPO_A = "acme-corp/example-service"
_REPO_B = "example-org/example-service-fork"


def test_resolve_repositories_defaults_to_current_repo() -> None:
    assert resolve_repositories("", _REPO_A) == [_REPO_A]


def test_resolve_repositories_splits_list() -> None:
    repos = resolve_repositories(f"{_REPO_A}, {_REPO_B}", "ignored/default")
    assert repos == [_REPO_A, _REPO_B]


def test_ignore_and_snooze_labels() -> None:
    now = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    assert has_ignore_label({"no-reminder"}, frozenset({"no-reminder", "wip"}))
    assert has_ignore_label({"wip"}, frozenset({"no-reminder", "wip"}))
    assert is_snoozed({"reminder-snooze"}, now=now)
    assert is_snoozed({"reminder-snooze-until:2026-05-30"}, now=now)
    assert not is_snoozed({"reminder-snooze-until:2026-05-20"}, now=now)


def test_idle_hours() -> None:
    now = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    assert idle_hours("2026-05-25T12:00:00Z", now=now) == pytest.approx(24.0)


def test_determine_action_reviewers() -> None:
    action, actors = determine_action(
        author="alice",
        reviews=[],
        requested_users=["bob"],
        requested_teams=[],
    )
    assert action == "reviewers"
    assert actors == ("bob",)


def test_determine_action_changes_requested() -> None:
    action, actors = determine_action(
        author="alice",
        reviews=[{"state": "CHANGES_REQUESTED", "submitted_at": "2026-05-26T10:00:00Z"}],
        requested_users=["bob"],
        requested_teams=[],
    )
    assert action.startswith("author")
    assert actors == ("alice",)


def test_determine_action_human_review_comment_for_author() -> None:
    action, actors = determine_action(
        author="alice",
        reviews=[
            {
                "state": "COMMENTED",
                "submitted_at": "2026-05-26T11:00:00Z",
                "user": {"login": "bob"},
            },
            {
                "state": "COMMENTED",
                "submitted_at": "2026-05-26T12:00:00Z",
                "user": {"login": "coderabbitai[bot]"},
            },
        ],
        requested_users=["carol"],
        requested_teams=[],
    )
    assert action == "author (review comment from bob)"
    assert actors == ("alice",)


def test_format_actor_only_mentions_toml_entries() -> None:
    slack_map = {"alice": "U111"}
    assert format_actor("alice", slack_map) == "<@U111>"
    assert format_actor("bob", slack_map) == "`bob`"


def test_needs_review_false_when_approved() -> None:
    assert not needs_review(
        author="alice",
        reviews=[
            {
                "state": "APPROVED",
                "submitted_at": "2026-05-26T10:00:00Z",
                "user": {"login": "bob"},
            }
        ],
        requested_users=[],
        requested_teams=[],
    )


def test_format_actor_uses_slack_map() -> None:
    assert format_actor("alice", {"alice": "U123"}) == "<@U123>"
    assert format_actor("bob", {}) == "`bob`"


def test_build_slack_payload_groups_by_repo() -> None:
    reminders = [
        PullRequestReminder(
            repository=_REPO_A,
            number=1,
            title="Add feature",
            url=f"https://github.com/{_REPO_A}/pull/1",
            author="alice",
            idle_hours=30,
            action="reviewers",
            actors=("bob",),
        ),
        PullRequestReminder(
            repository=_REPO_B,
            number=2,
            title="Sync downstream",
            url=f"https://github.com/{_REPO_B}/pull/2",
            author="carol",
            idle_hours=50,
            action="reviewers",
            actors=("dan",),
        ),
    ]
    payload = build_slack_payload(reminders, {})
    text = payload["blocks"][0]["text"]["text"]
    assert _REPO_A in text
    assert _REPO_B in text
    assert "#1 Add feature" in text


def test_parse_github_slack_map() -> None:
    assert parse_github_slack_map("alice:U1,bob:U2") == {"alice": "U1", "bob": "U2"}
