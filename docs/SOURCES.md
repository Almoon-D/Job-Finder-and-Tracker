# Sources

A *source* is one place where jobs are published. Each source in `config.yaml` has a `name`,
a `group` (which notification it feeds) and either a `url` (auto-detected) or a `type` plus
parameters.

```yaml
- name: Example Bank            # shown in notifications (override with `company:`)
  url: https://examplebank.wd3.myworkdayjobs.com/External
  group: company_sites          # default
  favorite: true                # also polled by the 'favorites' group
  enabled: true
  queries: [product manager]    # extra keyword searches (sources that support search)
  role_families: [product]      # only these families for this source
  exclude_title_any: [retail]   # extra exclusions for this source
  skip_location_filter: false
  use_coverage_search: true     # run coverage.search_terms globally on this source
  fetch_details: true           # download full descriptions for candidates
  max_pages: 10
  require_keyword_match: true   # only titles with a role-family keyword reach the AI (saves quota)
  only_queries: true            # ATS sources: run only `queries`, not the full listing
  use_ai: true                  # false: never send this source's jobs to the AI (keyword matching only)
```

`require_keyword_match` is useful on noisy, high-volume sources (whole-country board listings,
generic search engines). `only_queries` suits large employers where only a few roles interest you:
with `queries: [investor relations]`, `only_queries: true`, `role_families: [ir]` and
`require_keyword_match: true` only those searches run and only matching titles are kept. It is
supported by `workday`, `oracle_hcm`, `successfactors` (RMK) and `eightfold`.

`role_families` also limits the family the **AI** may assign: a job it scores well but files under a
family not in the list (an investor-relations role at a source limited to banking families) is dropped
on purpose. It is counted as `source_family` (not as a low score) in `runs/last_run.json`, which lists
those jobs under `blocked_by_family`, and in the weekly summary. If you want a company's roles from more
than one family, list them all.

`use_ai: false` keeps a source out of the AI matcher: its jobs pass only if a role-family keyword
is in the title (restrict `role_families` to unambiguous families to avoid noise). Use it for
sites whose `robots.txt` opts out of AI use (`Content-Signal: ai-input=no`), or to save quota.

Sources whose optional credentials are missing (`infojobs`, `adzuna`, `email_alerts`) are
**skipped**: the log says `source #n: skipped (no credentials: set …)`, they do not count as
failures and trigger no health alert. They start working as soon as the secrets exist.

Use `jobfinder detect <url>` to see which adapter handles a URL, and
`jobfinder test-source <name|url>` to see what a source returns.

## Auto-detected ATS adapters

| Type | URL pattern | Notes |
|---|---|---|
| `workday` | `https://<tenant>.wdN.myworkdayjobs.com/<site>` | Picks the site's own location facets for your locations; relative dates ("Posted 3 Days Ago"). |
| `oracle_hcm` | `https://<host>.oraclecloud.<tld>/hcmUI/CandidateExperience/<lang>/sites/<site>` (`.com`, or a regional data centre such as `fa.ocs.oraclecloud.eu`) | Server-side country filter, sorted by posting date. |
| `eightfold` | `https://<company>.eightfold.ai/careers` | PCSX API, falls back to `/api/apply/v2`. Add `?domain=company.com` if needed. |
| `brassring` | `https://<host>/TGnewUI/Search/Home/Home?partnerid=<n>&siteid=<n>` | IBM/Infinite BrassRing Talent Gateway; newest first, stops at the age window. |
| `successfactors` | `https://careerN.successfactors.eu/career?company=<id>` | Legacy XML listing. For RMK sites on a custom domain set `type: successfactors` and `url:` the site root. |
| `greenhouse` | `boards.greenhouse.io/<board>` or `job-boards.greenhouse.io/<board>` | |
| `lever` | `jobs.lever.co/<company>` | |
| `smartrecruiters` | `jobs.smartrecruiters.com/<Company>` | Server-side country filter. |
| `ashby` | `jobs.ashbyhq.com/<board>` | |
| `workable` | `apply.workable.com/<account>` | |
| `recruitee` | `<company>.recruitee.com` | |
| `teamtailor` | `<company>.teamtailor.com` | Custom domain: set `type: teamtailor` and the site `url`. |
| `personio` | `<company>.jobs.personio.de` | |

## Job boards

| Type | Site | Needs | Notes |
|---|---|---|---|
| `linkedin` | LinkedIn public search | – | Rate-limited; keep queries few (use `OR`). |
| `efinancialcareers` | eFinancialCareers | – | Whole country, newest first, until the age window ends. |
| `jobcloud` | jobup.ch, jobs.ch | – | Public search API; searches are spaced out (CloudFront). |
| `infojobs` | InfoJobs (Spain) | `INFOJOBS_CLIENT_ID`, `INFOJOBS_CLIENT_SECRET` | Official API. |
| `adzuna` | Adzuna (ES, CH, MX, GB, FR…) | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Official API, free keys with a daily cap. |
| `email_alerts` | Alert e-mails over IMAP | `IMAP_USER`, `IMAP_PASSWORD` (`IMAP_HOST`) | LinkedIn, Indeed, InfoJobs, eFinancialCareers, jobup/jobs.ch, Michael Page; others with AI. |

### `linkedin`

Public guest search, with no login. LinkedIn rate-limits datacenter IPs, so keep the number
of queries low and never use it in the `favorites` group. Each query runs once per location,
so combine synonyms with `OR`: `'"private banker" OR "wealth manager"'`.

```yaml
- name: LinkedIn
  group: boards
  type: linkedin
  queries: [product manager, ux researcher]
  locations: ["Berlin, Germany", Lisbon]    # default: your configured cities and countries
  company_ids: [1234, 5678]                 # optional: LinkedIn company ids (f_C); one search covers them all
                                            # find an id with: jobfinder linkedin-id <company page URL>
  company_names: [Example Bank]             # optional: search by name and keep only that company
  experience_levels: [3, 4]                 # optional f_E filter
  max_pages: 2                              # 10 jobs per page
```

### `efinancialcareers`

Reads the JSON API behind the public search. For each country it takes every job, newest first,
until the jobs are older than the group's window (the search only sorts by date without a
keyword), then runs `queries` inside the country and the coverage terms worldwide. Listings
carry the full description.

```yaml
- name: eFinancialCareers
  group: boards
  type: efinancialcareers
  countries: [ES, CH]           # default: the countries of your locations
  max_pages: 4                  # 100 jobs per page
  require_keyword_match: true
```

### `jobcloud` (jobup.ch / jobs.ch)

```yaml
- name: jobup.ch
  group: boards
  type: jobcloud
  site: jobup.ch                # or jobs.ch
  queries: [private banker, gestionnaire de fortune]
  locations: [Geneva, Lausanne] # default: your configured Swiss cities
  max_pages: 2                  # 20 jobs per page, newest first
```

The API sits behind CloudFront, which blocks bursts and caches the 403 for a while: searches are
spaced out, a blocked search is skipped and the source only fails if every search is blocked.

### `infojobs` and `adzuna`

```yaml
- name: InfoJobs
  group: boards
  type: infojobs
  queries: [banca privada, relación con inversores]
  provinces: [madrid]           # optional; default all of Spain
- name: Adzuna
  group: boards
  type: adzuna
  queries: [private banker]
  countries: [ES, CH, MX]       # default: the countries of your locations
  where: {ES: [Madrid]}         # optional; default your cities (or the whole country)
```

Credentials: [developer.infojobs.net](https://developer.infojobs.net) (application → client id
and secret) and [developer.adzuna.com](https://developer.adzuna.com) (app id and key).

### `email_alerts`

Many boards only offer e-mail alerts (Indeed), or let you tune them far better than any search.
Send those alerts to a dedicated mailbox and read it over IMAP:

```yaml
- name: Alerts (boards)
  group: boards
  type: email_alerts
  parsers: [linkedin, indeed, infojobs, efinancialcareers, jobup]
  unknown: ai                    # other senders: extract jobs with the AI (without AI: ignored)
  exclude_senders: [michaelpage] # handled by another source
- name: Alerts (recruiters)
  group: recruiters
  type: email_alerts
  parsers: [michaelpage]
  ai_senders: [hays, robertwalters]   # these unknown senders are always read with the AI
```

- **Read-only**: the mailbox is opened with `EXAMINE` and messages are fetched with
  `BODY.PEEK[]`, so nothing is marked read, moved or deleted. The last UID seen is kept in
  `state/runs.json` (`monitors`), so each e-mail is processed once. Only e-mails of the last
  `max_age_days + 1` days are considered.
- **Parsers** turn every tracking link (`…?url=https%3A…`) into the job's canonical URL
  (`linkedin.com/jobs/view/<id>/`, `indeed.com/viewjob?jk=<id>`, `infojobs.net/…/of-i<id>`,
  `efinancialcareers.com/…id<n>`, `jobup.ch/…/detail/<uuid>/`, `michaelpage.*/job-detail/…/ref/…`).
  The link text is the title; the next lines of the same block are the company and the place
  (a place is only kept if it names a known country or city). Opaque trackers are followed with
  a GET (at most `resolve_redirects: 20` per run).
- **Unknown senders** with `unknown: ai`: the AI gets the subject, the text and the numbered list
  of links, and must answer JSON; jobs whose URL is not one of the e-mail's links are dropped.
- The e-mail date is used as an approximate posting date (`~26/09`).
- `senders: {indeed: [alerts@custom.example]}` overrides the sender patterns of a parser.

### Not supported

| Site | Why | Alternative |
|---|---|---|
| Indeed | No public API; the only programmatic access (used by python-jobspy) impersonates Indeed's mobile app with its private key, and the web pages are protected against datacenter IPs. | Indeed e-mail alerts (`email_alerts`), Adzuna. |
| Welcome to the Jungle | Search results are rendered in the browser from a third-party search service; `/jobs?query=` redirects to the home page. | Its e-mail alerts, read with `unknown: ai`. |

## Declarative recipes (any other site)

These keep all site-specific knowledge in your private config. Placeholders available in
URLs, bodies, params and headers:

- `{page}`, `{offset}`, `{limit}`
- `{query}`: `queries` plus the coverage search terms
- `{location}`: your cities, or `location_values`
- `{country}` (ISO2) and `{country_name}`

A string that is exactly one placeholder keeps its type, so `"{limit}"` becomes the number
`20`. `{query_slug}` is the query as a URL slug (`banca privada` → `banca-privada`), for sites
whose search URLs look like `/trabajo-de-banca-privada`.

### `json_api`

```yaml
- name: Example
  type: json_api
  url: https://api.example.test/graphql
  method: POST
  headers: {Origin: "https://careers.example.test"}
  body:
    query: "query Jobs($p: Int, $q: String) { jobs(page: $p, q: $q) { id title city country postedAt } }"
    variables: {p: "{page}", q: "{query}"}
  items: data.jobs                          # JMESPath to the list
  fields:
    id: id
    title: title
    url: applyUrl                           # or url_template: "https://careers.example.test/jobs/{id}"
    location: "join(', ', [city || '', country || ''])"  # JMESPath; a list gives several
    posted_at: postedAt                     # ISO, epoch, relative or dd/mm/yyyy
    description: summary
  pagination: {type: page, start: 0, limit: 50, max_pages: 5}   # or type: offset / none
  detail:                                   # optional: full description of candidates
    url_template: "https://api.example.test/jobs/{id}"
    description: job.descriptionHtml
```

### `html_list`

```yaml
- name: Example recruiter
  type: html_list
  url: https://recruiter.example.test/jobs?page={page}
  item: "article.job"
  title: "h2"
  link: "a"                 # or "self" if the item itself is the <a>
  location: ".location"
  date: ".date"
  company_selector: ".company"  # optional: company name (default: the source name)
  pagination: {start: 1, max_pages: 3}
  fetch: http               # or browser (Playwright; installed automatically when used)
  wait_for: "article.job"   # browser mode: wait for this selector
  detail: {description: ".job-description"}
```

Cards without a link (the page opens the job with JavaScript) can build the URL from an
attribute: `id_attr: data-id` and `url_template: "https://recruiter.example.test/job/{id}"`.
Search pages work with `{query}` or `{query_slug}` in `url` and the source's `queries`.

More options:

```yaml
  urls:                     # several listing pages in one source (instead of url)
    - "https://recruiter.example.test/jobs/finance?page={page}"
    - "https://recruiter.example.test/jobs/{query_slug}?page={page}"
  date_regex: "Online since:\\s*(.+)"    # the date part of the date text (groups are joined)
  location_regex: "\\d{4}\\s+(.+)$"      # the place part of the location text (no match: whole text)
  delay_seconds: 10         # pause between listing requests (honour a robots.txt Crawl-delay)
  detail: {jsonld: true}    # description, date and missing location from the job page's JSON-LD
```

A 404 on a later page, or on a keyword search, is read as "no more results" (many sites answer
404 to an empty search). A 404 on the first page of a plain `url` is still an error.

### `jsonld_sitemap` / `jsonld_pages`

For sites that embed schema.org `JobPosting` JSON-LD, as most recruiters do so that Google
Jobs can index them:

```yaml
- name: Example recruiter
  type: jsonld_sitemap
  url: https://recruiter.example.test/sitemap.xml   # sitemap or sitemap index
  url_pattern: "/jobs?/"
  sitemap_pattern: "job"                            # optional: which child sitemaps to follow
  max_urls: 60
```

`jsonld_pages` reads JobPosting objects directly from one or more listing pages (`urls:`).

Sitemaps whose values are wrapped in `<![CDATA[…]]>` are read, job pages are fetched concurrently
(within the per-host limit), a page that fails is skipped (the source only fails if all of them
do), and JSON-LD with raw line breaks inside strings is accepted. If a site refreshes `lastmod`
(and `datePosted`) every day, raise `max_urls` to cover all its jobs: the notification date is
then the refresh date.

### `rss`

```yaml
- name: Example feed
  type: rss
  url: https://careers.example.test/jobs.rss
  location_tag: category          # take locations from <category> elements
  location_regex: "Location:\\s*([^<\\n]+)"
```

### `page_monitor`

For small firms without a job list:

```yaml
- name: Example family office
  type: page_monitor
  url: https://example-fo.test/careers
  selector: main                   # optional
  link_pattern: "/careers/.+"      # new matching links become jobs; without it: "page changed" alerts
```

## Recruiters and headhunters

Recruiter sites rarely run a mainstream ATS. What usually works, by platform:

| Site looks like | Technique |
|---|---|
| Job pages with schema.org `JobPosting` and a job sitemap | `jsonld_sitemap` (dates from `datePosted`) |
| Server-rendered listing (Drupal, Liferay, Laravel, custom) | `html_list`; several sector or keyword pages with `urls`; `detail: {jsonld: true}` when job pages carry JSON-LD |
| WordPress with a job post type | its feed (`/<jobs-path>/feed/`) with `rss`; `pubDate` may be a sync time |
| Manatal career pages (`<slug>.careers-page.com`) | `html_list` on `?page={page}` (`article.job-card`); no dates |
| Single-page app over a JSON API (Sitecore/Next.js search APIs, Supabase REST…) | `json_api` with the same request the page makes (a public `apikey` header if the page sends one) |
| Bot protection (PerimeterX/HUMAN, Vercel checkpoint, Cloudflare challenge) | not reachable from GitHub Actions: create job alerts to your alerts mailbox and read them with `email_alerts` (`ai_senders`); if the firm posts on LinkedIn, add a `linkedin` source in the recruiters group with its `company_ids` and no `queries` (one keyword-less search per location) |
| Executive search firms that publish no mandates | nothing to watch: register your CV in their database |

Check `robots.txt` first: do not read disallowed paths, honour `Crawl-delay` with `delay_seconds`,
and use `use_ai: false` when it says `Content-Signal: ai-input=no`. Job sites answer datacenter
IPs differently from home connections, so confirm a new source from Actions with
`mode: test-source` (source number) or a `dry-run` of its group.

## Adding a new adapter (developers)

1. Create `src/jobfinder/sources/<kind>/<name>.py` with a class that subclasses `Adapter`,
   sets `type_name`, implements `async fetch() -> list[Job]` and optionally `enrich(job)`
   and `detect(url)`. Decorate it with `@register`.
2. Import the module in `src/jobfinder/sources/__init__.py` and, if it auto-detects URLs,
   add it to `DETECT_ORDER` in `detect.py`.
3. Add a synthetic fixture under `tests/fixtures/` and a test in `tests/test_adapters.py`.
   Use fictional companies only.
4. Adapters must be **vendor- or platform-generic**. Company-specific details belong in the
   user's private config, as declarative recipes.
