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
```

Use `jobfinder detect <url>` to see which adapter handles a URL, and
`jobfinder test-source <name|url>` to see what a source returns.

## Auto-detected ATS adapters

| Type | URL pattern | Notes |
|---|---|---|
| `workday` | `https://<tenant>.wdN.myworkdayjobs.com/<site>` | Picks the site's own location facets for your locations; relative dates ("Posted 3 Days Ago"). |
| `oracle_hcm` | `https://<host>.oraclecloud.com/hcmUI/CandidateExperience/<lang>/sites/<site>` | Server-side country filter, sorted by posting date. |
| `eightfold` | `https://<company>.eightfold.ai/careers` | PCSX API, falls back to `/api/apply/v2`. Add `?domain=company.com` if needed. |
| `brassring` | `https://<host>/TGnewUI/Search/Home/Home?partnerid=<n>&siteid=<n>` | IBM/Infinite BrassRing Talent Gateway; newest first, stops at the age window. |
| `successfactors` | `https://careerN.successfactors.eu/career?company=<id>` | Legacy XML listing. For RMK sites on a custom domain set `type: successfactors` and `url:` the site root. |
| `greenhouse` | `boards.greenhouse.io/<board>` or `job-boards.greenhouse.io/<board>` | |
| `lever` | `jobs.lever.co/<company>` | |
| `smartrecruiters` | `jobs.smartrecruiters.com/<Company>` | Server-side country filter. |
| `ashby` | `jobs.ashbyhq.com/<board>` | |
| `workable` | `apply.workable.com/<account>` | |
| `recruitee` | `<company>.recruitee.com` | |
| `teamtailor` | `<company>.teamtailor.com` | |
| `personio` | `<company>.jobs.personio.de` | |

## Job boards

### `linkedin`

Public guest search, with no login. LinkedIn rate-limits datacenter IPs, so keep the number
of queries low and never use it in the `favorites` group.

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

## Declarative recipes (any other site)

These keep all site-specific knowledge in your private config. Placeholders available in
URLs, bodies, params and headers:

- `{page}`, `{offset}`, `{limit}`
- `{query}`: `queries` plus the coverage search terms
- `{location}`: your cities, or `location_values`
- `{country}` (ISO2) and `{country_name}`

A string that is exactly one placeholder keeps its type, so `"{limit}"` becomes the number
`20`.

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
  pagination: {start: 1, max_pages: 3}
  fetch: http               # or browser (Playwright; installed automatically when used)
  wait_for: "article.job"   # browser mode: wait for this selector
  detail: {description: ".job-description"}
```

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
