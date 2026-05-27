"""Load reminder team filters and Slack mentions from a TOML config file."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(".github/pr-review-reminder.toml")
DEFAULT_REPO_ICON = "pr-open-1"


@dataclass(frozen=True)
class ReminderConfig:
    """User lists and Slack mapping for PR review reminders."""

    exclude_users: frozenset[str] = field(default_factory=frozenset)
    team_reviewers: frozenset[str] = field(default_factory=frozenset)
    slack_mentions: dict[str, str] = field(default_factory=dict)
    repo_icons: dict[str, str] = field(default_factory=dict)
    default_repo_icon: str = DEFAULT_REPO_ICON

    @classmethod
    def from_path(cls, path: Path) -> ReminderConfig:
        """Load configuration from a TOML file."""
        if not path.is_file():
            logger.info("Config file %s not found; using empty user lists", path)
            return cls()
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> ReminderConfig:
        """Build configuration from a parsed TOML/JSON dict."""
        exclude = _normalize_login_set(data.get("exclude_users", []))
        team = _normalize_login_set(data.get("team_reviewers", []))
        slack_raw = data.get("slack_mentions", {})
        slack: dict[str, str] = {}
        if isinstance(slack_raw, dict):
            for github_login, slack_id in slack_raw.items():
                if github_login and slack_id:
                    slack[_normalize_login(str(github_login))] = str(slack_id).strip()
        icons = _parse_repo_icons(data.get("repo_icons", {}))
        default_icon = str(data.get("default_repo_icon", DEFAULT_REPO_ICON)).strip().strip(":")
        return cls(
            exclude_users=exclude,
            team_reviewers=team,
            slack_mentions=slack,
            repo_icons=icons,
            default_repo_icon=default_icon or DEFAULT_REPO_ICON,
        )


def _normalize_login(login: str) -> str:
    return login.strip().lower()


def _normalize_login_set(values: object) -> frozenset[str]:
    if not isinstance(values, list):
        return frozenset()
    return frozenset(_normalize_login(v) for v in values if isinstance(v, str) and v.strip())


def normalize_login(login: str) -> str:
    """Normalize a GitHub login for case-insensitive comparison."""
    return _normalize_login(login)


def resolve_config_path(raw: str | None) -> Path:
    """Resolve config path from CLI flag or environment."""
    path = raw or os.environ.get("PR_REVIEW_CONFIG", "")
    if path.strip():
        return Path(path.strip())
    return DEFAULT_CONFIG_PATH


def _parse_repo_icons(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    icons: dict[str, str] = {}
    for repo, icon in raw.items():
        if repo and icon:
            icons[str(repo).strip().lower()] = str(icon).strip().strip(":")
    return icons


def repo_icon_for(repository: str, config: ReminderConfig) -> str:
    """Return a Slack emoji token such as ``:pr-open-1:`` for a repository."""
    key = repository.strip().lower()
    name = config.repo_icons.get(key, config.default_repo_icon)
    return f":{name.strip(':')}:"


def merge_slack_maps(config: ReminderConfig, env_map: dict[str, str]) -> dict[str, str]:
    """Merge config Slack IDs with env overrides (env wins)."""
    merged = dict(config.slack_mentions)
    merged.update(env_map)
    return merged


def load_config_json_for_tests(data: dict) -> ReminderConfig:
    """Load config from a dict (used in unit tests)."""
    return ReminderConfig.from_dict(data)
