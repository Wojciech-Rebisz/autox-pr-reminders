"""Filter pull requests and actors by configured GitHub nicknames."""

from __future__ import annotations

from .config import ReminderConfig, normalize_login


def pr_participants(
    *,
    author: str,
    requested_users: list[str],
    actors: tuple[str, ...],
) -> set[str]:
    """Return normalized GitHub logins involved in a pull request."""
    people = {normalize_login(author)}
    people.update(normalize_login(u) for u in requested_users)
    people.update(normalize_login(a) for a in actors)
    return people


def is_author_excluded(author: str, config: ReminderConfig) -> bool:
    """Return True when the PR author is on the exclude list."""
    return normalize_login(author) in config.exclude_users


def all_requested_reviewers_excluded(requested_users: list[str], config: ReminderConfig) -> bool:
    """Return True when every requested reviewer is excluded."""
    if not requested_users:
        return False
    return all(normalize_login(user) in config.exclude_users for user in requested_users)


def matches_team_filter(
    *,
    author: str,
    requested_users: list[str],
    actors: tuple[str, ...],
    config: ReminderConfig,
) -> bool:
    """Return True when the PR involves at least one configured team reviewer."""
    if not config.team_reviewers:
        return True
    return bool(pr_participants(author=author, requested_users=requested_users, actors=actors) & config.team_reviewers)


def should_include_pull_request(
    *,
    author: str,
    requested_users: list[str],
    actors: tuple[str, ...],
    config: ReminderConfig,
) -> bool:
    """Return True when a pull request passes user-based filters."""
    if is_author_excluded(author, config):
        return False
    if all_requested_reviewers_excluded(requested_users, config):
        return False
    if not matches_team_filter(
        author=author,
        requested_users=requested_users,
        actors=actors,
        config=config,
    ):
        return False
    return True


def filter_actors(actors: tuple[str, ...], config: ReminderConfig) -> tuple[str, ...]:
    """Keep only actors on the team list (if set) and not on the exclude list."""
    filtered: list[str] = []
    for actor in actors:
        key = normalize_login(actor)
        if key in config.exclude_users:
            continue
        if config.team_reviewers and key not in config.team_reviewers:
            continue
        filtered.append(actor)
    return tuple(filtered)
