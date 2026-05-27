# PR review Slack reminder

Scheduled GitHub Action that posts open pull requests needing attention to a Slack channel (AutoX / `pipelines-components` and other configured repos). Each qualifying PR is sent as its own message so the team can discuss it in a thread.

**Schedule:** weekdays at **07:00 UTC** (09:00 CEST in summer) — see `cron` in [`.github/workflows/pr-review-reminder.yml`](../workflows/pr-review-reminder.yml).

**Configuration:** [`.github/pr-review-reminder.toml`](../pr-review-reminder.toml) — team filter, Slack @mentions, optional per-repo emoji (`[repo_icons]`).

**Secrets:** `SLACK_WEBHOOK_URL`, `PR_REVIEW_GITHUB_TOKEN`.

**Variable:** `PR_REVIEW_REPOSITORIES` — comma-separated `owner/repo` list (empty = only the repo that hosts the workflow).

**Local dry-run (no Slack post):** from `.github`, set `PYTHONPATH=.`, export tokens, then  
`python3 -m pr_review_reminder.pr_review_reminder --config pr-review-reminder.toml --dry-run`.
