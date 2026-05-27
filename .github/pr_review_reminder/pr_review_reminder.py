"""Post Slack reminders for open pull requests awaiting review."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import ReminderConfig, merge_slack_maps, normalize_login, repo_icon_for, resolve_config_path
from .filters import should_include_pull_request

logger = logging.getLogger(__name__)

_API = "https://api.github.com"
_SNOOZE_LABEL = "reminder-snooze"
_SNOOZE_UNTIL_PREFIX = "reminder-snooze-until:"


@dataclass(frozen=True)
class PullRequestReminder:
    """A pull request that should appear in a reminder message."""

    repository: str
    number: int
    title: str
    url: str
    author: str
    idle_hours: float
    action: str
    actors: tuple[str, ...]
    reviewers: tuple[str, ...] = ()
    reviewer_teams: tuple[str, ...] = ()


class GitHubApiError(Exception):
    """Raised when the GitHub API returns an unexpected error."""


class GitHubClient:
    """Minimal GitHub REST client using urllib."""

    def __init__(self, token: str) -> None:
        """Store the GitHub API bearer token."""
        self._token = token

    def _request(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        url = f"{_API}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "autox-pr-review-reminder",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            # Truncate response bodies so logs never carry large or unexpected payloads.
            snippet = body[:200] + ("…" if len(body) > 200 else "")
            raise GitHubApiError(f"GitHub API {exc.code} for {path}: {snippet}") from exc

    def list_open_pulls(self, repo: str) -> list[dict[str, Any]]:
        """Return all open pull requests for a repository."""
        pulls: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request(
                f"/repos/{repo}/pulls",
                params={"state": "open", "per_page": "100", "page": str(page)},
            )
            if not batch:
                break
            pulls.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return pulls

    def list_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Return submitted reviews for a pull request."""
        return self._request(f"/repos/{repo}/pulls/{pr_number}/reviews")

    def list_requested_reviewers(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Return users and teams currently requested on a pull request."""
        return self._request(f"/repos/{repo}/pulls/{pr_number}/requested_reviewers")


def parse_label_names(labels: list[dict[str, Any]]) -> set[str]:
    """Extract label names from the GitHub API label objects."""
    return {label["name"] for label in labels}


def parse_github_slack_map(raw: str) -> dict[str, str]:
    """Parse ``github_login:SLACK_ID`` pairs from a comma-separated string."""
    mapping: dict[str, str] = {}
    if not raw.strip():
        return mapping
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.warning("Skipping invalid github-slack map entry: %s", entry)
            continue
        github_login, slack_id = entry.split(":", 1)
        mapping[github_login.strip()] = slack_id.strip()
    return mapping


def is_snoozed(label_names: set[str], *, now: datetime) -> bool:
    """Return True when reminder snooze labels are active."""
    if _SNOOZE_LABEL in label_names:
        return True
    for name in label_names:
        if not name.startswith(_SNOOZE_UNTIL_PREFIX):
            continue
        date_text = name[len(_SNOOZE_UNTIL_PREFIX) :]
        try:
            until = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            logger.warning("Invalid snooze label date: %s", name)
            continue
        if now < until + timedelta(days=1):
            return True
    return False


def has_ignore_label(label_names: set[str], ignore_labels: frozenset[str]) -> bool:
    """Return True when the pull request has a configured ignore label."""
    return bool(label_names.intersection(ignore_labels))


def parse_iso8601(value: str) -> datetime:
    """Parse a GitHub timestamp into an aware UTC datetime."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(UTC)


def idle_hours(updated_at: str, *, now: datetime) -> float:
    """Return hours since the pull request was last updated."""
    updated = parse_iso8601(updated_at)
    return max((now - updated).total_seconds() / 3600.0, 0.0)


def latest_meaningful_review(reviews: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest non-comment review, if any."""
    meaningful = [r for r in reviews if r.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}]
    if not meaningful:
        return None
    return max(meaningful, key=lambda r: r.get("submitted_at") or r.get("created_at") or "")


def is_bot_login(login: str) -> bool:
    """Return True for GitHub App / bot accounts."""
    lower = login.lower()
    return lower.endswith("[bot]") or lower.endswith("-bot") or login == "github-actions[bot]"


def human_logins(logins: list[str]) -> tuple[str, ...]:
    """Drop bot accounts from a list of GitHub logins."""
    return tuple(login for login in logins if login and not is_bot_login(login))


def team_slugs(teams: list[dict[str, Any]]) -> tuple[str, ...]:
    """Extract team slugs from the requested-reviewers API payload."""
    return tuple(team.get("slug", team.get("name", "team")) for team in teams)


_REVIEW_STATES_FOR_PARTICIPANTS = frozenset(
    {"COMMENTED", "CHANGES_REQUESTED", "APPROVED", "DISMISSED"}
)


def collect_pr_reviewers(
    *,
    author: str,
    requested_users: list[str],
    reviews: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Merge requested reviewers with humans who submitted a review (no bots, not author)."""
    author_key = normalize_login(author)
    seen: set[str] = set()
    ordered: list[str] = []

    def add(login: str) -> None:
        if not login or is_bot_login(login):
            return
        key = normalize_login(login)
        if key == author_key or key in seen:
            return
        seen.add(key)
        ordered.append(login)

    for login in requested_users:
        add(login)

    for review in reviews:
        if review.get("state") not in _REVIEW_STATES_FOR_PARTICIPANTS:
            continue
        login = (review.get("user") or {}).get("login", "")
        add(login)

    return tuple(ordered)


def latest_human_comment_review(reviews: list[dict[str, Any]], author: str) -> dict[str, Any] | None:
    """Return the latest COMMENTED review from a human reviewer (not the PR author)."""
    author_key = normalize_login(author)
    comments: list[dict[str, Any]] = []
    for review in reviews:
        if review.get("state") != "COMMENTED":
            continue
        login = (review.get("user") or {}).get("login", "")
        if not login or normalize_login(login) == author_key:
            continue
        if is_bot_login(login):
            continue
        comments.append(review)
    if not comments:
        return None
    return max(comments, key=lambda r: r.get("submitted_at") or r.get("created_at") or "")


def determine_action(
    *,
    author: str,
    reviews: list[dict[str, Any]],
    requested_users: list[str],
    requested_teams: list[str],
) -> tuple[str, tuple[str, ...]]:
    """Describe who should act next on a pull request."""
    latest = latest_meaningful_review(reviews)
    if latest and latest.get("state") == "CHANGES_REQUESTED":
        return "author (address review feedback)", (author,)

    comment_review = latest_human_comment_review(reviews, author)
    if comment_review:
        reviewer = (comment_review.get("user") or {}).get("login", "reviewer")
        return f"author (review comment from {reviewer})", (author,)

    actors: list[str] = list(requested_users)
    if actors:
        return "reviewers", tuple(actors)

    if latest and latest.get("state") == "APPROVED":
        reviewer = (latest.get("user") or {}).get("login")
        if reviewer and reviewer != author:
            return "merge or follow-up", (reviewer,)

    if requested_teams:
        team_names = tuple(team.get("slug", team.get("name", "team")) for team in requested_teams)
        return "team reviewers", team_names

    return "request reviewers or review", (author,)


def needs_review(
    *,
    author: str,
    reviews: list[dict[str, Any]],
    requested_users: list[str],
    requested_teams: list[str],
) -> bool:
    """Return True when a pull request should be included in reminders."""
    action, _ = determine_action(
        author=author,
        reviews=reviews,
        requested_users=requested_users,
        requested_teams=requested_teams,
    )
    if action.startswith("author"):
        return True
    if action == "reviewers":
        return True
    if action == "team reviewers":
        return True
    latest = latest_meaningful_review(reviews)
    if latest and latest.get("state") == "APPROVED":
        reviewer = (latest.get("user") or {}).get("login")
        if reviewer and reviewer != author:
            return False
    return True


def collect_reminders(
    client: GitHubClient,
    repositories: list[str],
    *,
    idle_threshold_hours: float,
    ignore_labels: frozenset[str],
    config: ReminderConfig,
    now: datetime | None = None,
) -> list[PullRequestReminder]:
    """Collect pull requests that match reminder criteria across repositories."""
    now = now or datetime.now(tz=UTC)
    reminders: list[PullRequestReminder] = []

    for repo in repositories:
        logger.info("Scanning %s", repo)
        try:
            pulls = client.list_open_pulls(repo)
        except GitHubApiError:
            logger.exception("Failed to list pull requests for %s", repo)
            continue

        for pr in pulls:
            if pr.get("draft"):
                continue

            label_names = parse_label_names(pr.get("labels", []))
            if has_ignore_label(label_names, ignore_labels) or is_snoozed(label_names, now=now):
                continue

            hours = idle_hours(pr["updated_at"], now=now)
            if hours < idle_threshold_hours:
                continue

            number = int(pr["number"])
            author = (pr.get("user") or {}).get("login", "unknown")
            reviews = client.list_reviews(repo, number)
            requested = client.list_requested_reviewers(repo, number)
            requested_users = [u["login"] for u in requested.get("users", [])]
            requested_teams = requested.get("teams", [])

            if not needs_review(
                author=author,
                reviews=reviews,
                requested_users=requested_users,
                requested_teams=requested_teams,
            ):
                continue

            action, actors = determine_action(
                author=author,
                reviews=reviews,
                requested_users=requested_users,
                requested_teams=requested_teams,
            )
            if not should_include_pull_request(
                author=author,
                requested_users=requested_users,
                actors=actors,
                config=config,
            ):
                continue

            # Show everyone who should act (per issue). team_reviewers only scopes
            # which PRs are included above, not which names appear in the message.
            reminders.append(
                PullRequestReminder(
                    repository=repo,
                    number=number,
                    title=pr["title"],
                    url=pr["html_url"],
                    author=author,
                    idle_hours=hours,
                    action=action,
                    actors=actors,
                    reviewers=collect_pr_reviewers(
                        author=author,
                        requested_users=requested_users,
                        reviews=reviews,
                    ),
                    reviewer_teams=team_slugs(requested_teams),
                )
            )

    reminders.sort(key=lambda item: (item.repository, -item.idle_hours, item.number))
    return reminders


def format_actor(login: str, slack_map: dict[str, str]) -> str:
    """Format a GitHub login: ``<@id>`` only when listed in slack_mentions, else ``login``."""
    slack_id = slack_map.get(normalize_login(login))
    if slack_id:
        return f"<@{slack_id}>"
    return f"`{login}`"


def format_reviewer_list(
    reviewers: tuple[str, ...],
    reviewer_teams: tuple[str, ...],
    slack_map: dict[str, str],
) -> str:
    """Format requested reviewers (humans + teams); @mentions only from slack_map."""
    parts = [format_actor(login, slack_map) for login in reviewers]
    parts.extend(f"`{slug}`" for slug in reviewer_teams)
    return ", ".join(parts) if parts else "_none requested_"


def format_pr_message_text(item: PullRequestReminder, slack_map: dict[str, str], *, config: ReminderConfig) -> str:
    """Build mrkdwn body for a single pull request (one Slack message)."""
    lines = [
        f"{repo_icon_for(item.repository, config)} *{item.repository}*",
        f"<{item.url}|#{item.number} {item.title}> — idle {format_idle(item.idle_hours)}",
        f"Author: {format_actor(item.author, slack_map)}",
        f"Reviewer: {format_reviewer_list(item.reviewers, item.reviewer_teams, slack_map)}",
    ]
    if item.action != "reviewers":
        lines.append(f"Status: *{item.action}*")
    return "\n".join(lines)


def format_idle(hours: float) -> str:
    """Format idle duration for Slack messages."""
    if hours < 48:
        return f"{hours:.0f}h"
    days = hours / 24
    return f"{days:.1f}d"


def build_slack_payload_for_pr(
    item: PullRequestReminder,
    slack_map: dict[str, str],
    config: ReminderConfig,
) -> dict[str, Any]:
    """Build one Slack incoming-webhook payload for a single pull request."""
    body = format_pr_message_text(item, slack_map, config=config)
    return {
        "text": f"#{item.number} {item.title}",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": body[:3000]}}],
    }


def build_slack_payloads(
    reminders: list[PullRequestReminder],
    slack_map: dict[str, str],
    config: ReminderConfig,
) -> list[dict[str, Any]]:
    """Build one Slack payload per PR so each can have its own thread."""
    if not reminders:
        return [
            {
                "text": "No pull requests need review reminders right now.",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":white_check_mark: No open PRs need review reminders right now.",
                        },
                    }
                ],
            }
        ]
    return [build_slack_payload_for_pr(item, slack_map, config) for item in reminders]


def build_slack_payload(
    reminders: list[PullRequestReminder],
    slack_map: dict[str, str],
    config: ReminderConfig,
) -> dict[str, Any]:
    """Backward-compatible single payload (first message only). Prefer ``build_slack_payloads``."""
    return build_slack_payloads(reminders, slack_map, config)[0]


def post_slack_webhook(webhook_url: str, payload: dict[str, Any]) -> None:
    """Post a JSON payload to a Slack incoming webhook."""
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Slack webhook returned HTTP {response.status}")


def post_slack_payloads(webhook_url: str, payloads: list[dict[str, Any]]) -> None:
    """Post each payload as a separate channel message (one thread root per PR)."""
    for index, payload in enumerate(payloads):
        if index > 0:
            time.sleep(0.3)
        post_slack_webhook(webhook_url, payload)


def resolve_repositories(raw: str | None, default_repo: str) -> list[str]:
    """Parse configured repositories or fall back to the current repository."""
    if raw and raw.strip():
        return [repo.strip() for repo in raw.split(",") if repo.strip()]
    return [default_repo]


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repositories",
        default=os.environ.get("PR_REVIEW_REPOSITORIES", ""),
        help="Comma-separated owner/repo list (default: GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--idle-hours",
        type=float,
        default=float(os.environ.get("PR_REVIEW_IDLE_HOURS", "24")),
        help="Minimum idle time since last PR update before reminding",
    )
    parser.add_argument(
        "--ignore-labels",
        default=os.environ.get("PR_REVIEW_IGNORE_LABELS", "no-reminder,wip"),
        help="Comma-separated labels that suppress reminders",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Path to .toml config with exclude_users, team_reviewers, slack_mentions",
    )
    parser.add_argument(
        "--github-slack-map",
        default=os.environ.get("PR_REVIEW_GITHUB_SLACK_MAP", ""),
        help="Optional env override: github_login:SLACK_MEMBER_ID pairs for @mentions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("PR_REVIEW_DRY_RUN", "").lower() in {"1", "true", "yes"},
        help="Print the Slack payload without sending it",
    )
    parser.add_argument(
        "--skip-slack",
        action="store_true",
        help="Collect reminders only; do not post to Slack (useful in tests)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the reminder collector and optionally post to Slack."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    token = os.environ.get("PR_REVIEW_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.error("GITHUB_TOKEN or PR_REVIEW_GITHUB_TOKEN is required")
        return 1

    default_repo = os.environ.get("GITHUB_REPOSITORY", "")
    repositories = resolve_repositories(args.repositories, default_repo)
    if not repositories or repositories == [""]:
        logger.error("No repositories configured")
        return 1

    ignore_labels = frozenset(label.strip() for label in args.ignore_labels.split(",") if label.strip())

    config_path = resolve_config_path(args.config or None)
    config = ReminderConfig.from_path(config_path)
    logger.info(
        "Loaded config from %s (exclude=%d, team=%d, slack=%d)",
        config_path,
        len(config.exclude_users),
        len(config.team_reviewers),
        len(config.slack_mentions),
    )

    client = GitHubClient(token)
    reminders = collect_reminders(
        client,
        repositories,
        idle_threshold_hours=args.idle_hours,
        ignore_labels=ignore_labels,
        config=config,
    )

    slack_map = merge_slack_maps(config, parse_github_slack_map(args.github_slack_map))
    payloads = build_slack_payloads(reminders, slack_map, config)

    if args.dry_run or args.skip_slack:
        for index, payload in enumerate(payloads, start=1):
            print(json.dumps(payload, indent=2))
            preview = ""
            for block in payload.get("blocks", []):
                if block.get("type") == "section":
                    preview = (block.get("text") or {}).get("text", "")
                    break
            if preview:
                print(f"\n--- Slack preview ({index}/{len(payloads)}) ---\n")
                print(preview)
        if args.dry_run:
            logger.info("Dry run complete (%d PR(s), %d message(s))", len(reminders), len(payloads))
        return 0

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.error("SLACK_WEBHOOK_URL is required unless --dry-run or --skip-slack is set")
        return 1

    try:
        post_slack_payloads(webhook_url, payloads)
    except Exception:
        logger.exception("Failed to post Slack reminder")
        return 1

    logger.info("Posted %d Slack message(s) for %d PR(s)", len(payloads), len(reminders))
    return 0


if __name__ == "__main__":
    sys.exit(main())
