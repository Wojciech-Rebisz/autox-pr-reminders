# PR review Slack reminder

Scheduled GitHub Action that posts open pull requests needing attention to a Slack channel (AutoX / `pipelines-components` and other configured repos). It reduces forgotten reviews without manual chasing.

**Schedule:** weekdays at **07:00 UTC** (09:00 CET) — see `cron` in [`.github/workflows/pr-review-reminder.yml`](../workflows/pr-review-reminder.yml).

**Configuration:** [`.github/pr-review-reminder.toml`](../pr-review-reminder.toml) (team filter, Slack @mentions). Secrets: `SLACK_WEBHOOK_URL`, `PR_REVIEW_GITHUB_TOKEN`. Optional variable: `PR_REVIEW_REPOSITORIES`.

## Slack message format

```
:pr-open-1: PR review reminder — N PR(s) need attention

*owner/repo*
• #89 PR title — idle 16h
Author: <@slack-id or `github-login`>
Reviewer: `VaniHaripriya`, `hbelmiro`
Status: *author (review comment from hbelmiro)*
```

- **Author** — PR author. **Reviewer** — requested reviewers plus humans who left a review (bots omitted).
- `<@…>` only for GitHub logins listed under `[slack_mentions]` in the TOML; others use `` `login` ``.
- **Status** — shown when the author should respond (e.g. review comment); omitted when the PR only awaits reviewers.

**Local preview (no Slack post):** `python3 -m pr_review_reminder.pr_review_reminder --config pr-review-reminder.toml --dry-run` from `.github` with `PYTHONPATH=.` and tokens in the environment.
