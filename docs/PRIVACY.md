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
| `runs/stats.json`: daily counts per group and source (for the weekly summary) | Private data repo | You |
| `tracker/applications.csv`: jobs you marked, status, dates, notes, history | Private data repo | You |
| `state/tracker.json`: Telegram update offset, ids of messages with buttons or Discord reactions, layout of grouped digests | Private data repo | You |
| `reports/weekly/`: weekly summaries | Private data repo | You |
| `feeds/`: JSON Feed, RSS, summary and tracker JSON for dashboards | Private data repo | You (and dashboards with a read-only token) |
| Tokens and API keys | GitHub Secrets of the public repo | Only the workflow (masked in logs) |
| Job-alert e-mails | Your dedicated mailbox (read-only over IMAP) | You; state keeps only the last UID |

## Public logs

Actions logs of public repositories are public. The code follows one rule: **log messages
never contain values from the config or from job postings.**

- `log.info()` prints only counts, adapter types and source numbers (`source #7 (workday)`).
- `log.detail()` prints private details, and only when running locally with `--verbose`.
  In CI it is always silent.
- Config validation errors show only the field path and error type in CI. The full message
  goes to `runs/config_error.txt` in the private repo.
- Notification errors show the channel and HTTP status only.
- `test-ai` prints the provider's `name` from `llm.providers` (e.g. `gemini-flash`) and a
  status. Use generic names there.
- E-mail alerts: only counts are logged (`email: 12 new messages … 30 jobs`), never senders,
  subjects or links.
- `test-source` in Actions takes the source **number** (`#7`), because workflow inputs are
  shown in the public log.
- Tracker sync logs only counts (`tracker: 3 updates (2 buttons, 1 commands, 0 ignored), 1 rows
  changed`). Button data, commands and message text never appear. The bot only obeys the chat in
  `TELEGRAM_CHAT_ID`: presses and commands from any other chat are ignored.
- The weekly summary logs only its number of new jobs.
- A test (`tests/test_pipeline.py::test_ci_logs_are_redacted`) runs the pipeline, a tracker
  sync and a weekly summary in CI mode with `--verbose`. It fails if a company name, title,
  location, URL, group name, bot token or Telegram message appears in the output.

The GitHub UI still shows the workflow file, the cron times and the name of the private
repository in the checkout step. Neither says anything about your job search.

## Third parties

- **AI providers (optional)**: they receive public job postings and your `profile` text, and
  with `unknown: ai` the text of alert e-mails from senders without a built-in parser (they may
  include the greeting of your dedicated mailbox). Free tiers may use the data for training, so
  keep the profile anonymous and use a dedicated mailbox.
- **Mailbox (optional)**: the tool logs in with an app password, opens the mailbox read-only
  (`EXAMINE`, `BODY.PEEK`) and never changes, moves or deletes messages. Opaque tracking links
  of known alert senders may be followed (a GET, at most 20 per run) to find the job URL.
- **Telegram**: the bot sends your alerts and, for the tracker, reads the button presses and
  commands of your chat (`getUpdates`, kept by Telegram for at most 24 hours).
- **ntfy.sh**: a topic is readable by anyone who knows its name. Use a long random name or a
  token-protected or self-hosted server.
- **cron-job.org (optional)**: stores a token that can only trigger workflows of the public
  repository (Actions: read & write). It cannot read your private repository.
- **Job sites**: they see requests coming from GitHub's IP addresses, never your identity.
  LinkedIn is queried through its public guest endpoints, without logging in.

## Forks and pull requests

Workflows triggered by pull requests from forks do not receive secrets, and the repository
never uses `pull_request_target`. A fork cannot read your private data repository.
