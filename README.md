# job-finder-and-tracker

A personal job radar that runs **only on GitHub Actions** (free for public repositories).
It watches company career sites, job boards and recruiter pages, keeps the offers that
match **your private preferences**, and notifies you by **Telegram, e-mail, Discord, ntfy**
(or any [Apprise](https://github.com/caronc/apprise) service). Each notification includes a
direct link and the approximate date and time the offer was posted.

Nothing personal lives in this repository. Your companies, roles, locations and schedules
sit in a **private data repository** that the workflow reads with a scoped token. The
public Actions logs are redacted, so anyone can fork the tool without seeing what you are
looking for.

> 🇪🇸 Step-by-step setup guide in Spanish: [docs/SETUP.es.md](docs/SETUP.es.md)

## Features

- **Four alert types**, each with its own schedule, set in your private config:
  - **Favourite companies**: polled every ~10 minutes, instant high-priority alert.
  - **Company career sites**: e.g. 09:00 and 20:30, offers posted in the last 3 days.
  - **Job boards**: digest every N days, grouped by role family and location.
  - **Recruiters / headhunters**: digest every N days.
- **Auto-detected ATS adapters**: paste a careers URL and the adapter is chosen for you.
  Supported: Workday, Oracle HCM (Recruiting Cloud), Eightfold, SAP SuccessFactors (legacy
  and RMK), Greenhouse, Lever, SmartRecruiters, Ashby, Workable, Recruitee, Teamtailor,
  Personio.
- **Declarative recipes** for any other site, with no code:
  - `json_api` for any JSON or GraphQL API
  - `html_list` for CSS selectors, with an optional headless browser
  - `jsonld_sitemap` / `jsonld_pages` for schema.org `JobPosting`
  - `rss`
  - `page_monitor` for small firms without an ATS
- **Job boards**: LinkedIn public search (keywords, company id or name), eFinancialCareers,
  jobup.ch / jobs.ch, InfoJobs and Adzuna (official APIs, optional keys), plus recipes for
  others (e.g. OCC, Computrabajo).
- **Job-alert e-mails** read over IMAP, read-only: LinkedIn, Indeed, InfoJobs,
  eFinancialCareers, jobup/jobs.ch and Michael Page are parsed directly (tracking links become
  canonical job URLs); other senders can be read by the AI.
- **Cross-source de-duplication**: the same offer from a company site, a board and an e-mail is
  sent once (fuzzy company + title, canonical URL), with "also on …".
- **Locations**: any number of countries and cities, with multilingual aliases from GeoNames
  (München/Munich, Lisboa/Lisbon, Wien/Vienna…). **Coverage** matching keeps roles based
  elsewhere that mention your market ("DACH coverage, based in London").
- **Optional free AI matcher**: any OpenAI-compatible API, such as Gemini Flash, NVIDIA NIM,
  Groq or OpenRouter, tried in order (a spent quota moves on to the next one). It scores fit,
  estimates the years of experience required and explains why. Cheap keyword filters run
  first; without an API key everything still works on keywords only.
- **Idempotent scheduling**: a run can be triggered by GitHub's cron, by an external
  scheduler such as cron-job.org, or manually, and a slot is never notified twice. This
  matters because GitHub's cron can be hours late or skip runs: a missed slot still runs up to
  `grace_hours` (12 by default) late, and a safety tick checks every hour.
- **Health alerts** when a source fails repeatedly or suddenly returns nothing.
- **Application tracker in Telegram**: every per-job alert has ⭐ Interested · ✅ Applied ·
  🗣 Interview · ❌ Discard buttons. Presses are saved in `tracker/applications.csv` in the private
  repo, a file you can also edit on the GitHub website. `/status` and `/pending` commands. There is
  no server: the bot is polled at every run and by a `tracker-sync` run every 1–3 h.
- **Weekly summary** to every channel, and saved as `reports/weekly/YYYY-Www.md`. It covers new jobs
  per group, company and role family, matches against discards, the tracker funnel, and source health.
- **Private feeds** (JSON Feed 1.1, RSS, summary and tracker JSON) for dashboards. Tested examples for
  Glance and Homepage are in [docs/DASHBOARDS.md](docs/DASHBOARDS.md).

## How it works

```
cron / cron-job.org / manual ─► GitHub Actions (this public repo)
                                   │ checks out your PRIVATE data repo into ./data
                                   ▼
          sources ─► normalise ─► location ─► freshness ─► not seen ─► exclusions
                  ─► AI score (optional) ─► de-duplicate ─► notify ─► state + feeds
                                   ▼
                     commit & push to the private data repo
```

| Where | What |
|---|---|
| Public repo (this one) | Code, workflows, docs. No personal data. |
| Private data repo | `config.yaml`, `state/`, `runs/` (last run, daily stats), `feeds/`, `tracker/applications.csv`, `reports/weekly/` |
| GitHub Secrets (public repo) | Tokens: data repo, Telegram, SMTP, Discord, ntfy, AI keys |

See [docs/PRIVACY.md](docs/PRIVACY.md) for the full privacy model.

## Quick start

1. Fork this repository and enable Actions in your fork.
2. Create a **private** repository (e.g. `job-finder-data`) and add a `config.yaml` based on
   [`config.example.yaml`](config.example.yaml).
3. Create a fine-grained token with **Contents: read & write** on that private repo only.
4. In your fork: add the variable `DATA_REPO` (`you/job-finder-data`) and the secret
   `DATA_REPO_TOKEN`, plus the secrets of the channels you want (`TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, `SMTP_*`, `EMAIL_TO`, `DISCORD_WEBHOOK_URL`, `NTFY_TOPIC`…) and,
   optionally, AI keys (`GEMINI_API_KEY`, `NVIDIA_API_KEY`, `GROQ_API_KEY`), `IMAP_*` for
   alert e-mails and `INFOJOBS_*` / `ADZUNA_*`. The workflows pass secrets one by one: a new
   secret name must also be added to their `env:` block.
5. Run **Actions → jobfinder → Run workflow** with `mode: validate`, then `test-notify` (its
   Telegram message shows the tracker buttons) and `test-ai`.
6. Optional but recommended: trigger the workflows from [cron-job.org](https://cron-job.org)
   for punctual alerts, and `mode: tracker-sync` every 1–3 h for the tracker (see the setup guide).

## Command line

```bash
uv sync
uv run jobfinder validate-config --config my-config.yaml
uv run jobfinder detect https://acme.wd3.myworkdayjobs.com/External
uv run jobfinder test-source "Example Bank" --config my-config.yaml --details
uv run jobfinder test-ai --config my-config.yaml                         # checks every AI provider
uv run jobfinder dry-run --config my-config.yaml --data-dir /tmp/jf -v   # prints matches locally
uv run jobfinder run --data-dir data                                     # what the workflow does
uv run jobfinder tracker-sync --data-dir data                            # read tracker buttons/commands
```

Outside CI, `-v` prints private details to your terminal. In CI, logs only ever show counts
and source numbers.

## Development

```bash
uv sync
uv run ruff check .
uv run pytest
```

Adapters are tested against synthetic fixtures (`tests/fixtures`), with no network needed.
[docs/SOURCES.md](docs/SOURCES.md) explains how to add a new source type.

## License

[GNU AGPL-3.0-or-later](LICENSE).
