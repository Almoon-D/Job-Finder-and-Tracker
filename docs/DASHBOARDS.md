# Dashboards

Every run writes these files to the **private** data repository. A dashboard reads them through the
[GitHub contents API](https://docs.github.com/en/rest/repos/contents#get-repository-content) with a
read-only token, so nothing is ever public.

| File | Format | Content |
|---|---|---|
| `feeds/summary.json` | JSON | Counts (24 h / 7 d / 30 d, per group and family), source health, tracker counts, latest 10 jobs |
| `feeds/tracker.json` | JSON | Tracker funnel, pending counts and every tracked job (status, date, notes) |
| `feeds/jobs.json` | [JSON Feed 1.1](https://www.jsonfeed.org/version/1.1/) | Jobs notified in the last 30 days |
| `feeds/jobs.xml` | RSS 2.0 | The same jobs, for any feed reader |

Checked against the current documentation of each tool (September 2026): Glance v0.8.6
([configuration](https://github.com/glanceapp/glance/blob/main/docs/configuration.md),
[custom-api](https://github.com/glanceapp/glance/blob/main/docs/custom-api.md)), Homepage
([customapi](https://gethomepage.dev/widgets/services/customapi/)) and ntfy
([publish](https://docs.ntfy.sh/publish/), [subscribe API](https://docs.ntfy.sh/subscribe/api/)).

| Tool | Can it send an `Authorization` header? | How it is used here |
|---|---|---|
| Glance `custom-api` | Yes: `headers` (also in `subrequests`) | `summary.json`, `tracker.json`, ntfy history |
| Glance `rss` | Yes: `headers` on each feed | `jobs.xml` |
| Homepage `customapi` | Yes: `headers` | `summary.json`, `tracker.json` |
| ntfy | Only in `http` action buttons, sent from your phone. It never fetches URLs itself, and attachments by URL carry no headers | Push only: alerts and the weekly summary. Glance can show its history (below) |

## 1. Read-only token (PAT #3)

GitHub → **Settings → Developer settings → Fine-grained tokens → Generate new token**:

- **Repository access**: *Only select repositories* → your private data repository.
- **Permissions → Contents**: *Read-only*. Nothing else.

Every request uses the same URL pattern and two headers:

```
GET https://api.github.com/repos/<you>/<data-repo>/contents/feeds/summary.json
Authorization: Bearer <PAT #3>
Accept: application/vnd.github.raw+json
```

GitHub also rejects requests without a `User-Agent` header. Glance sends one; Homepage does
not, so its examples set it.

`Accept: application/vnd.github.raw+json` returns the file itself. Without it, GitHub wraps the
file in JSON with base64 content, and the widgets below would show nothing. The token allows
5,000 requests per hour, so keep `cache` / `refreshInterval` at several minutes (the files change at most every 10 minutes).

## 2. Glance

Set the environment variables `JOBFINDER_REPO` (`you/job-finder-data`) and `JOBFINDER_TOKEN`
(PAT #3) in the Glance container. Glance replaces `${NAME}` anywhere in its config, or use
`${secret:name}` with Docker secrets. `NTFY_TOPIC` / `NTFY_TOKEN` are only needed for the last
widget.

<!-- dashboards-test: glance -->
```yaml
pages:
  - name: Jobs
    columns:
      - size: full
        widgets:
          # Counters, tracker funnel and the latest jobs (summary.json).
          - type: custom-api
            title: Job finder
            cache: 15m
            url: https://api.github.com/repos/${JOBFINDER_REPO}/contents/feeds/summary.json
            headers:
              Authorization: Bearer ${JOBFINDER_TOKEN}
              Accept: application/vnd.github.raw+json
              X-GitHub-Api-Version: "2022-11-28"
            template: |
              {{ if ne .Response.StatusCode 200 }}<p class="color-negative">HTTP {{ .Response.StatusCode }}: check JOBFINDER_TOKEN and JOBFINDER_REPO</p>{{ end }}
              <div class="flex justify-between text-center">
                <div><div class="color-highlight size-h3">{{ .JSON.Int "jobs_last_24h" }}</div><div class="size-h6">24 H</div></div>
                <div><div class="color-highlight size-h3">{{ .JSON.Int "jobs_last_7d" }}</div><div class="size-h6">7 DAYS</div></div>
                <div><div class="color-highlight size-h3">{{ .JSON.Int "tracker.counts.applied" }}</div><div class="size-h6">APPLIED</div></div>
                <div><div class="color-highlight size-h3">{{ .JSON.Int "tracker.counts.interview" }}</div><div class="size-h6">INTERVIEWS</div></div>
              </div>
              <ul class="list list-gap-10 collapsible-container margin-top-15" data-collapse-after="5">
                {{ range .JSON.Array "latest" }}
                <li>
                  <a class="size-h4 color-highlight block text-truncate" href="{{ .String "url" }}" target="_blank" rel="noreferrer">{{ .String "job_title" }}</a>
                  <ul class="list-horizontal-text">
                    <li>{{ .String "company" }}</li>
                    <li {{ .String "notified_at" | parseRelativeTime "rfc3339" }}></li>
                    {{ if .String "tracker_label" }}<li class="color-positive">{{ .String "tracker_label" }}</li>{{ end }}
                  </ul>
                </li>
                {{ end }}
              </ul>
              {{ if gt (.JSON.Int "sources.failing") 0 }}
              <p class="color-negative margin-top-10">⚠ {{ .JSON.Int "sources.failing" }} / {{ .JSON.Int "sources.checked" }} sources failing</p>
              {{ end }}

          # Application tracker (tracker.json): funnel and pending, then every tracked job.
          - type: custom-api
            title: Tracker
            cache: 15m
            url: https://api.github.com/repos/${JOBFINDER_REPO}/contents/feeds/tracker.json
            headers:
              Authorization: Bearer ${JOBFINDER_TOKEN}
              Accept: application/vnd.github.raw+json
            template: |
              {{ if ne .Response.StatusCode 200 }}<p class="color-negative">HTTP {{ .Response.StatusCode }}: check JOBFINDER_TOKEN and JOBFINDER_REPO</p>{{ end }}
              <ul class="list-horizontal-text">
                {{ range .JSON.Array "funnel" }}{{ if gt (.Int "count") 0 }}<li>{{ .String "label" }} {{ .Int "count" }}</li>{{ end }}{{ end }}
              </ul>
              <p class="size-h6 margin-top-5">To apply: {{ .JSON.Int "pending.to_apply" }} · Awaiting reply: {{ .JSON.Int "pending.follow_up" }}</p>
              <ul class="list list-gap-10 collapsible-container margin-top-10" data-collapse-after="6">
                {{ range .JSON.Array "items" }}
                <li>
                  <a class="color-highlight block text-truncate" href="{{ .String "url" }}" target="_blank" rel="noreferrer">{{ .String "company" }} — {{ .String "title" }}</a>
                  <ul class="list-horizontal-text">
                    <li>{{ .String "status_label" }}</li>
                    <li>{{ .String "date" }}</li>
                    {{ if .String "notes" }}<li class="text-truncate">{{ .String "notes" }}</li>{{ end }}
                  </ul>
                </li>
                {{ end }}
              </ul>

          # The same jobs as an RSS feed (jobs.xml): categories show group, family and tracker status.
          - type: rss
            title: Job feed
            style: detailed-list
            limit: 20
            cache: 15m
            feeds:
              - url: https://api.github.com/repos/${JOBFINDER_REPO}/contents/feeds/jobs.xml
                title: Job finder
                headers:
                  Authorization: Bearer ${JOBFINDER_TOKEN}
                  Accept: application/vnd.github.raw+json

          # Latest ntfy notifications of a protected topic (ntfy returns JSON Lines).
          - type: custom-api
            title: ntfy
            cache: 5m
            url: https://ntfy.sh/${NTFY_TOPIC}/json
            parameters:
              poll: 1
              since: 7d
            headers:
              Authorization: Bearer ${NTFY_TOKEN}
            skip-json-validation: true
            template: |
              <ul class="list list-gap-10 collapsible-container" data-collapse-after="5">
                {{ range .JSONLines }}{{ if eq (.String "event") "message" }}
                <li>
                  {{ if .String "click" }}<a class="color-highlight block text-truncate" href="{{ .String "click" }}" target="_blank" rel="noreferrer">{{ .String "title" }}</a>
                  {{ else }}<div class="color-highlight text-truncate">{{ .String "title" }}</div>{{ end }}
                  <div class="size-h6" {{ .String "time" | parseRelativeTime "unix" }}></div>
                </li>
                {{ end }}{{ end }}
              </ul>
```

Notes:

- To add a widget to an existing page, copy only its item from `widgets:`.
- The tracker widget lists the `items` of `tracker.json` (newest change first). Each item has
  `status` (`interested`, `applied`, `interview`, `offer`, `rejected`, `discarded` or `other`),
  `status_label`, `date` (last change), `days` (since then), `name` (company — title), `company`,
  `title`, `location`, `url` and `notes`.
- The ntfy widget needs a topic protected with an access token (self-hosted ntfy, or a reserved
  topic on ntfy.sh). On a public topic, drop `headers`. The topic name then works as a password,
  so it must not appear in shared configs.

## 3. Homepage

In `services.yaml`. Homepage replaces `{{HOMEPAGE_VAR_*}}` with environment variables:
set `HOMEPAGE_VAR_JOBFINDER_REPO` (`you/job-finder-data`) and `HOMEPAGE_VAR_JOBFINDER_TOKEN`
(PAT #3), or use `HOMEPAGE_FILE_*` for a token stored in a file.

<!-- dashboards-test: homepage -->
```yaml
- Job finder:
    - Summary:
        icon: mdi-briefcase-search
        widget:
          type: customapi
          url: https://api.github.com/repos/{{HOMEPAGE_VAR_JOBFINDER_REPO}}/contents/feeds/summary.json
          refreshInterval: 900000 # 15 min
          headers:
            Authorization: Bearer {{HOMEPAGE_VAR_JOBFINDER_TOKEN}}
            Accept: application/vnd.github.raw+json
            User-Agent: homepage # GitHub rejects requests without one, and Homepage sends none
            X-GitHub-Api-Version: "2022-11-28"
          mappings:
            - field: jobs_last_24h
              label: 24 h
              format: number
            - field: jobs_last_7d
              label: 7 days
              format: number
            - field: tracker.counts.applied
              label: Applied
              format: number
            - field: tracker.counts.interview
              label: Interviews
              format: number
    - Latest jobs:
        icon: mdi-new-box
        widget:
          type: customapi
          url: https://api.github.com/repos/{{HOMEPAGE_VAR_JOBFINDER_REPO}}/contents/feeds/summary.json
          refreshInterval: 900000
          headers:
            Authorization: Bearer {{HOMEPAGE_VAR_JOBFINDER_TOKEN}}
            Accept: application/vnd.github.raw+json
            User-Agent: homepage # GitHub rejects requests without one, and Homepage sends none
          display: dynamic-list
          mappings:
            items: latest
            name: title
            label: notified_at
            format: relativeDate
            limit: 10
            target: "{url}"
    - Tracker:
        icon: mdi-clipboard-check-outline
        widget:
          type: customapi
          url: https://api.github.com/repos/{{HOMEPAGE_VAR_JOBFINDER_REPO}}/contents/feeds/tracker.json
          refreshInterval: 900000
          headers:
            Authorization: Bearer {{HOMEPAGE_VAR_JOBFINDER_TOKEN}}
            Accept: application/vnd.github.raw+json
            User-Agent: homepage # GitHub rejects requests without one, and Homepage sends none
          display: dynamic-list
          mappings:
            items: items
            name: name
            label: status_label
            limit: 10
            target: "{url}"
```

The block view shows at most 4 fields. Other useful fields: `jobs_last_30d`,
`tracker.counts.interested`, `tracker.counts.offer`, `tracker.pending.to_apply`,
`tracker.pending.follow_up`, `sources.ok`, `sources.failing`. `target` inserts the item's `url`
as is, so each row opens the job offer.

## 4. ntfy

ntfy only receives notifications: it cannot fetch the feeds, with or without headers.

What the tool sends to ntfy:

- Job alerts: one notification per job (`per_job`, up to 10), or a Markdown digest. Tapping it
  opens the offer.
- The weekly summary, as Markdown, cut at ntfy's 4 KB limit. It starts with the path of the full
  report (`reports/weekly/YYYY-Www.md`).

The tracker buttons are not sent to ntfy. ntfy `http` action buttons can send headers, but every
message would then carry a GitHub token through the ntfy server, where it stays in the message
cache and anyone who knows the topic can read it. Instead, use Telegram's buttons or edit
`tracker/applications.csv` on the GitHub website or app. To see past ntfy messages on a
dashboard, use the Glance widget above.

## 5. Discord

Discord webhooks cannot show tracker buttons. A webhook created in the channel settings (the
`DISCORD_WEBHOOK_URL` this tool uses) cannot send interactive components. With
`?with_components=true` it can only send link buttons. Button clicks are delivered to a Discord
*application* through an interactions endpoint or the gateway, and both need a server that is
always running. GitHub Actions cannot provide one. Discord keeps receiving the alerts and the
weekly summary. Track jobs with Telegram, or by editing the CSV on the web.

## 6. How these examples were tested

- A test (`tests/test_feeds.py`) checks that every field used by the blocks above exists in the
  files the tool writes.
- The Glance (v0.8.6) and Homepage (v2.4.0) blocks were run unchanged against a local copy of the
  GitHub contents API. Like GitHub, it rejects requests without the token or a `User-Agent`, and
  without the raw `Accept` header it returns base64. The rendered pages were then checked in a browser.
  The ntfy widget was tested against a local endpoint that answers like ntfy's poll API (JSON Lines,
  token required). With a wrong token, the Glance widgets show `HTTP 401` instead of empty numbers.
