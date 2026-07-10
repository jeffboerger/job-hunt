# job-hunt

**A personal job-search aggregator that skips the scraping.** Instead of
fighting bot-protected career pages, it hits the JSON APIs that power them:

| Source | Endpoint | Auth |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io` | none |
| Lever | `api.lever.co/v0/postings` | none |
| Ashby | `api.ashbyhq.com/posting-api` | none |
| Workday | the hidden `/wday/cxs/` JSON endpoint behind every myworkdayjobs.com site | none |
| USAJobs | `data.usajobs.gov/api/search` — federal postings with GS-grade band and public-hiring-path filtering; full JD in the payload | free key |
| CareerCircle | server-rendered search pages, HTML-parsed (staffing/recruiter-backed postings) | none |
| Adzuna | aggregator with true city+radius geo filtering, multi-query × multi-metro | free app id+key |
| Remotive | remote-jobs API | none |

One run sweeps a configurable watchlist of companies, filters by title and
location, flags what's new since the last run, and writes a sortable HTML
report.

Part of a three-repo pipeline: **job-hunt** discovers postings and
auto-triages every new JD (rule-based fit / seniority / ghost-risk verdicts
rendered in the report) →
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
- **Built-in triage** — every new JD gets a rule-based verdict (fit,
  seniority reality-check, remote truth from the JD text, ghost-posting
  risk) rendered as a color-coded Fit column in the report, with the
  reasoning on hover. Free and offline; an optional LLM backend sits
  behind an explicit flag. See [Triage engine](#triage-engine-triagepy).
- **Posting-age window** — `--since 3d` / `2w`, or set a default in config;
  Workday's fuzzy "Posted 30+ Days Ago" is normalized to real dates.
- **JD corpus building** — with `save_jds` enabled, the full description of
  every new matching posting is saved to a dated folder. Free for
  Lever/Ashby/Remotive/USAJobs (already in the API payload); one polite
  detail fetch for Greenhouse/Workday/CareerCircle. Feeds downstream
  keyword-frequency analysis.
- **Federal-aware** — USAJobs source filters to the public hiring path (no
  internal-only "area of consideration" postings) and a configurable GS
  grade band; the title filter understands federal conventions ("IT
  Specialist", "Operations Research Analyst").
- **Secrets stay out of git** — API credentials load from a gitignored
  `.env` (built-in loader, no dependency); config files carry no keys.
- **Outputs** — console summary, sortable `jobs.html` with Fit
  verdicts, optional CSV.
- **`--check-sources`** — verifies every configured company slug responds.
- **Playwright fallback** (`js_board.py`) for the rare board with no JSON
  API, driven by a 3-selector YAML config per board.

## Quick start

```bash
git clone https://github.com/jeffboerger/job-hunt && cd job-hunt
python3 -m venv venv && source venv/bin/activate
pip install requests pyyaml beautifulsoup4

# credentialed sources (both optional, both free):
cat > .env << 'DONE'
USAJOBS_API_KEY="key-from-developer.usajobs.gov"
ADZUNA_APP_ID=id-from-developer.adzuna.com
ADZUNA_APP_KEY=key-from-developer.adzuna.com
DONE
echo ".env" >> .gitignore

# edit companies.yaml: your watchlist + search block
python jobhunt.py                          # everything matching
python jobhunt.py --new-only               # the daily driver
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
usajobs:
  email: you@example.com          # your API registration email
  keywords: [data engineer, data analyst]
  pay_grade_low: 7                # GS band = federal seniority filter
  pay_grade_high: 12
  locations: [Orlando, Florida, Houston, Texas]
  days: 30
adzuna:
  distance_km: 40                 # credentials come from .env
  queries: [data engineer, data analyst]
  wheres: [Orlando, FL, Houston, TX]
careercircle:
  locations: ["state~FL~Florida State~27.54~-81.82"]   # tuples from their site's URL
  keywords: [data engineer]
  pages: 2
search:
  since: 2w              # default posting-age window
  save_jds: jds          # save new postings' descriptions here
  titles: [data engineer, analytics engineer, data analyst, it specialist]
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

## Triage engine (`triage.py`)

**Every new posting gets a structured verdict so 7am-you reads one-liners,
not twenty job descriptions.** After each discovery run, a rule-based
classifier scores every newly saved JD — apply, look closer, or skip —
and caches the verdict forever in the same SQLite database the
seen-tracker uses. Free, offline, deterministic. An optional LLM backend
(Claude Haiku) sits behind an explicit `--llm` flag for when nuance is
worth pennies.

### How it works

```
jds/YYYY-MM/*.txt ──► parse header, rebuild uid ──► already triaged? ──no──►
                                                                        │
   seen_jobs.sqlite3 (triage table) ◄── verdict ◄── rules engine (or --llm)
```

1. **Find pending.** Scans the `jds/` folder that `save_jds` populates.
   Each saved JD's header (`TITLE:`, `COMPANY:`, `SOURCE:`, `URL:`) is
   enough to rebuild the exact uid `jobhunt.py` uses
   (`source|company|url`) — discovery and triage share one identity per
   posting, no second bookkeeping system.
2. **Classify.** The default engine is pure regex/heuristics — no network,
   no key, no cost:
   - **Seniority**: years-of-experience parsing ("5+ years" → senior_only,
     "0-2" → junior_ok, 3–5 → stretch), senior/junior title and body
     language. Unstated seniority degrades safely to `stretch` → a human
     look, never an auto-skip on thin evidence.
   - **Remote truth**: what the JD *text* supports ("remote-first" vs
     "3 days per week in office" vs "remote work is not available"), not
     what the location field claims.
   - **Ghost risk**: evergreen/talent-pool language, thin JDs,
     suspiciously wide salary bands.
   - **Hard-no screens**: clearances, wrong-stack signals (mobile,
     embedded), weighed against stack hits (python, sql, dbt, bigquery…).
   - **Location**: target metros or true-remote.
3. **Cache forever.** Verdicts land in a `triage` table keyed by uid. A
   posting is never triaged twice; a daily run only touches what's new.

### The verdict

| Field | Values | What it answers |
|---|---|---|
| `fit` | `strong` / `maybe` / `skip` | Apply today, human look, or move on |
| `seniority_fit` | `junior_ok` / `stretch` / `senior_only` | Can 0–3 yrs actually get this |
| `remote_truth` | `remote` / `hybrid` / `onsite` / `unclear` | What the JD text supports |
| `ghost_risk` | `low` / `medium` / `high` | Evergreen/pipeline-posting smell |
| `one_liner` | text | The deciding factors, assembled from the rules that fired |

**Every verdict is explainable.** The one-liner names the exact rules that
fired ("Skip: senior-level title; ts/sci") — there is never a "why did it
say that" mystery, and the `raw_json` column stores the full reason list
plus the parsed years-ask for auditing.

### Usage

```bash
python triage.py             # triage everything new (free rules engine)
python triage.py --dry-run   # list pending, classify nothing
python triage.py --show      # browse all cached verdicts, newest first
python triage.py --show | grep strong    # this morning's shortlist
```

Daily flow: `python jobhunt.py --new-only` — that's it. The discovery run
auto-triages every new JD and renders verdicts as a **Fit column** in
jobs.html (hover for the why; filter by fit in the bar). The commands
above are for browsing, auditing, and calibrating outside the report. The
first-ever run triages the whole existing `jds/` backlog — still free, and
it doubles as the calibration dataset.

#### Optional LLM backend

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env    # gitignored, like all creds
python triage.py --llm --limit 3
```

One Claude Haiku call per posting (~fractions of a cent each; a full
backlog costs pennies). The LLM path never runs without the explicit
`--llm` flag and hard-exits without a key — it cannot spend money by
accident. Its output is treated as hostile input: fences stripped, JSON
extracted, every field validated against a fixed vocabulary; malformed
responses are rejected *without caching* so they retry next run. The
`model` column records which engine produced each verdict, so the two are
directly comparable.

### Calibration

The tuning surface is the block of constants at the top of `triage.py`:
`STACK_STRONG`, `STACK_WRONG`, `HARD_NO`, `GOOD_METROS`, and the regexes
(plus `PROFILE_TEXT` for the LLM path). When a verdict disagrees with your
own read of a posting, that disagreement is signal: the miss becomes a new
pattern or list entry. Rules read words, not meaning — "we'd consider
exceptional early-career candidates" after a 5-year ask will still file as
senior — so expect to tune, and treat every tune as a logged decision.

### Design decisions

- **Reuses, doesn't duplicate.** Imports the `.env` loader, config reader,
  and database path from `jobhunt.py`; verdicts share the seen-tracking
  database. One repo, one identity scheme, one SQLite file.
- **Free by default, smart by choice.** The zero-cost engine is the
  default; intelligence is an opt-in flag, never a dependency.
- **Ambiguity degrades safely.** Unknowns route to `maybe`, not `skip` —
  the failure mode is reading one extra one-liner, not missing a job.
- **Fail-open on errors, fail-closed on garbage.** Errors and malformed
  LLM output skip the posting (retried next run) rather than caching a
  bad verdict or crashing the batch.
- **The engine advises, the human applies.** Triage orders the queue; it
  doesn't submit applications. Judgment stays with the person whose name
  is on the resume.

## Design notes

- **APIs over scraping**: sturdier, faster, and respectful — the tool
  identifies itself, sleeps between requests, and only reads public data.
- **Deliberately human-in-the-loop**: this discovers and triages; it does
  not auto-apply. Application quality beats application volume.

## License

MIT
