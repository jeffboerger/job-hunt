# job-hunt

**A personal job-search aggregator that skips the scraping.** Instead of
fighting bot-protected career pages, it hits the JSON APIs that power them:

| Source | Endpoint |
|---|---|
| Greenhouse | `boards-api.greenhouse.io` (public, no auth) |
| Lever | `api.lever.co/v0/postings` (public, no auth) |
| Ashby | `api.ashbyhq.com/posting-api` (public, no auth) |
| Workday | the hidden `/wday/cxs/` JSON endpoint behind every myworkdayjobs.com site |
| Remotive | remote-jobs API |
| Adzuna | aggregator with true city+radius geo filtering (free API key) |

One run sweeps a configurable watchlist of companies, filters by title and
location, flags what's new since the last run, and writes a sortable HTML
report.

Part of a three-repo pipeline: **job-hunt** discovers postings →
[resume-tailor](https://github.com/jeffboerger/resume-tailor) generates a
tailored resume and cover letter per posting →
[keyword_finder](https://github.com/jeffboerger/keyword_finder) provides the
shared keyword corpus. Built and used daily in my own data engineering job
search.

## Features

- **Phrase-aware title matching** — `data engineer` matches "Senior Data
  Engineer II" and "Data and Ontology Engineer" but not "Software Engineer,
  Big Data" (word order matters, up to 2 intervening words). `|` gives
  per-word alternatives: `data|analytics engineer`.
- **Include/exclude filters** for titles and locations (drop staff/principal
  roles, drop non-US remote) — all in YAML config.
- **Junior boost** — titles signaling entry-level (junior, associate, new
  grad, "Engineer I") float to the top with a JR-FRIENDLY badge.
- **Seen-tracking** — SQLite database marks every posting; `--new-only`
  shows just what appeared since the last run. NEW badges in the report.
- **Posting-age window** — `--since 3d` / `2w`, or set a default in config;
  Workday's fuzzy "Posted 30+ Days Ago" is normalized to real dates.
- **JD corpus building** — with `save_jds` enabled, the full description of
  every new matching posting is saved to a dated folder. Free for
  Lever/Ashby/Remotive (already in the API payload); one polite detail fetch
  for Greenhouse/Workday. Feeds downstream keyword-frequency analysis.
- **Outputs** — console summary, sortable `jobs.html`, optional CSV.
- **`--check-sources`** — verifies every configured company slug responds.
- **Playwright fallback** (`js_board.py`) for the rare board with no JSON
  API, driven by a 3-selector YAML config per board.

## Quick start

```bash
git clone https://github.com/jeffboerger/job-hunt && cd job-hunt
python3 -m venv venv && source venv/bin/activate
pip install requests pyyaml

# edit companies.yaml: your watchlist + search block
python jobhunt.py                          # everything matching, all time
python jobhunt.py --new-only --since 3d    # the daily driver
open jobs.html
```

### Config sketch (companies.yaml)

```yaml
greenhouse: [gitlab, datadog, stripe]
lever: [palantir, plaid]
ashby: [openai, ramp]
workday:
  - tenant: disney
    host: disney.wd5.myworkdayjobs.com
    site: disneycareer
search:
  since: 2w              # default posting-age window
  save_jds: jds          # save new postings' descriptions here
  titles: [data engineer, analytics engineer, data analyst]
  locations: [Orlando, Tampa, FL, Remote]
  exclude_titles: [staff, principal, director, senior]
  exclude_locations: [canada, india, emea]
```

Keep multiple config files for different search profiles and switch with
`--config` (e.g., a focused daily list and a weekly wide-net list).

### Finding a company's ATS slug

Open their careers page, click any posting, read the URL:
`boards.greenhouse.io/{slug}/...`, `jobs.lever.co/{slug}/...`,
`jobs.ashbyhq.com/{slug}/...`, or `{tenant}.wd5.myworkdayjobs.com/{site}/...`.
Verify the whole watchlist with `--check-sources`.

## Design notes

- **APIs over scraping**: sturdier, faster, and respectful — the tool
  identifies itself, sleeps between requests, and only reads public data.
- **Deliberately human-in-the-loop**: this discovers and triages; it does
  not auto-apply. Application quality beats application volume.

## License

MIT
