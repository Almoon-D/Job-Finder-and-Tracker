# Privacy model

The tool is designed so that a **public** repository (free GitHub Actions minutes) reveals
nothing about what its owner is looking for.

## What lives where

| Data | Location | Visible to |
|---|---|---|
| Code, workflows, docs, example config | Public repo | Everyone |
| `config.yaml`: companies, roles, locations, schedules, profile | Private data repo | You |
| `state/`: every job seen, notifications, AI verdicts, source health | Private data repo | You |
| `runs/last_run.json`, `runs/config_error.txt`: run details and errors | Private data repo | You |
| `feeds/`: JSON Feed, RSS, summary for dashboards | Private data repo | You (and dashboards with a read-only token) |
| Tokens and API keys | GitHub Secrets of the public repo | Only the workflow (masked in logs) |

## Public logs

Actions logs of public repositories are public. The code follows one rule: **log messages
never contain values from the config or from job postings.**

- `log.info()` prints only counts, adapter types and source numbers (`source #7 (workday)`).
- `log.detail()` prints private details, and only when running locally with `--verbose`.
  In CI it is always silent.
- Config validation errors show only the field path and error type in CI. The full message
  goes to `runs/config_error.txt` in the private repo.
- Notification errors show the channel and HTTP status only.
- A test (`tests/test_pipeline.py::test_ci_logs_are_redacted`) runs the pipeline in CI mode
  with `--verbose` and fails if a company name, title, location, URL or group name appears
  in the output.

The GitHub UI still shows the workflow file, the cron times and the name of the private
repository in the checkout step. Neither says anything about your job search.

## Third parties

- **AI providers (optional)**: they receive public job postings and your `profile` text.
  Free tiers may use the data for training, so keep the profile anonymous.
- **ntfy.sh**: a topic is readable by anyone who knows its name. Use a long random name or a
  token-protected or self-hosted server.
- **cron-job.org (optional)**: stores a token that can only trigger workflows of the public
  repository (Actions: read & write). It cannot read your private repository.
- **Job sites**: they see requests coming from GitHub's IP addresses, never your identity.
  LinkedIn is queried through its public guest endpoints, without logging in.

## Forks and pull requests

Workflows triggered by pull requests from forks do not receive secrets, and the repository
never uses `pull_request_target`. A fork cannot read your private data repository.
