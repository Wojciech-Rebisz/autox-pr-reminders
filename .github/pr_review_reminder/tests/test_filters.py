# ruff: noqa: D103
"""Unit tests for PR user filters."""

from __future__ import annotations

from ..config import ReminderConfig, load_config_json_for_tests
from ..filters import (
    filter_actors,
    should_include_pull_request,
)


def test_exclude_author() -> None:
    config = load_config_json_for_tests({"exclude_users": ["dependabot[bot]"]})
    assert not should_include_pull_request(
        author="dependabot[bot]",
        requested_users=["alice"],
        actors=("alice",),
        config=config,
    )


def test_team_filter_requires_team_member() -> None:
    config = load_config_json_for_tests({"team_reviewers": ["alice", "bob"]})
    assert not should_include_pull_request(
        author="carol",
        requested_users=["outsider"],
        actors=("outsider",),
        config=config,
    )
    assert should_include_pull_request(
        author="carol",
        requested_users=["alice"],
        actors=("alice",),
        config=config,
    )


def test_team_filter_includes_authors_pr_waiting_on_external_reviewer() -> None:
    """Author on team: PR awaiting review from someone outside team still counts."""
    config = load_config_json_for_tests({"team_reviewers": ["alice"]})
    assert should_include_pull_request(
        author="alice",
        requested_users=["outsider"],
        actors=("outsider",),
        config=config,
    )


def test_filter_actors_keeps_team_only() -> None:
    config = load_config_json_for_tests(
        {
            "team_reviewers": ["alice", "bob"],
            "exclude_users": ["eve"],
        }
    )
    assert filter_actors(("alice", "outsider", "eve"), config) == ("alice",)


def test_exclude_all_requested_reviewers_skips_pr() -> None:
    config = load_config_json_for_tests({"exclude_users": ["bot"]})
    assert not should_include_pull_request(
        author="carol",
        requested_users=["bot"],
        actors=("bot",),
        config=config,
    )


def test_empty_team_includes_everyone() -> None:
    config = ReminderConfig()
    assert should_include_pull_request(
        author="anyone",
        requested_users=["stranger"],
        actors=("stranger",),
        config=config,
    )
