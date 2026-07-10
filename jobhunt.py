#!/usr/bin/env python3
"""
jobhunt.py — a personal job-search aggregator.

Instead of scraping HTML from bot-protected job boards, this hits the JSON APIs
that power modern career sites directly:

  * Greenhouse  — boards-api.greenhouse.io (public, no auth)
  * Lever       — api.lever.co/v0/postings (public, no auth)
  * Ashby       — api.ashbyhq.com/posting-api (public, no auth)
  * Workday     — the hidden /wday/cxs/ JSON endpoint behind every
                  myworkdayjobs.com site (Disney, NVIDIA, Salesforce, ...)
  * Remotive    — remotive.com/api (public remote-job API)
  * Adzuna      — aggregator API with true city+radius filtering (free key)

Usage:
  python jobhunt.py --title "data engineer" --locations "Orlando,Tampa,Lakeland,Remote"
  python jobhunt.py --title "analytics engineer" --locations Orlando --new-only --csv jobs.csv
  python jobhunt.py --check-sources          # verify every configured company slug
"""
from __future__ import annotations


import argparse
import csv
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

DEFAULT_CONFIG = Path(__file__).parent / "companies.yaml"
DB_PATH = Path(__file__).parent / "seen_jobs.sqlite3"
UA = {"User-Agent": "Mozilla/5.0 (personal job-search tool; polite; 1 req/sec)"}
TIMEOUT = 20


# --------------------------------------------------------------------------- #
#  Data model
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    source: str
    company: str
    title: str
    location: str
    url: str
    posted: str = ""          # ISO date string when available
    remote: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def uid(self) -> str:
        return f"{self.source}|{self.company}|{self.url}"


# --------------------------------------------------------------------------- #
#  Sources — each returns list[Job]
# --------------------------------------------------------------------------- #
def fetch_greenhouse(slug: str) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append(Job(
            source="greenhouse", company=slug,
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            posted=(j.get("updated_at") or "")[:10],
            raw=j,
        ))
    return jobs


def fetch_lever(slug: str) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cats = j.get("categories") or {}
        ts = j.get("createdAt")
        posted = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
        jobs.append(Job(
            source="lever", company=slug,
            title=j.get("text", ""),
            location=cats.get("location", "") or "",
            url=j.get("hostedUrl", ""),
            posted=posted,
            remote="remote" in (cats.get("location", "") or "").lower(),
            raw=j,
        ))
    return jobs


def fetch_ashby(slug: str) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append(Job(
            source="ashby", company=slug,
            title=j.get("title", ""),
            location=j.get("location", "") or "",
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            posted=(j.get("publishedAt") or "")[:10],
            remote=bool(j.get("isRemote")),
            raw=j,
        ))
    return jobs


def fetch_workday(entry: dict, search_text: str) -> list[Job]:
    """
    Every myworkdayjobs.com career site (Disney, NVIDIA, ...) is a JS app fed by
    a JSON endpoint:  POST {host}/wday/cxs/{tenant}/{site}/jobs
    entry = {tenant: 'disney', host: 'disney.wd5.myworkdayjobs.com', site: 'disneycareer'}
    """
    tenant, host, site = entry["tenant"], entry["host"], entry["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs, offset, page = [], 0, 20
    for _ in range(5):  # cap at 100 postings per company per run
        payload = {"appliedFacets": {}, "limit": page, "offset": offset,
                   "searchText": search_text}
        r = requests.post(api, json=payload, headers={**UA, "Accept": "application/json"},
                          timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath", "")
            jobs.append(Job(
                source="workday", company=tenant,
                title=j.get("title", ""),
                location=j.get("locationsText", "") or "",
                url=f"https://{host}/en-US/{site}{path}" if path else f"https://{host}",
                posted=workday_posted_to_iso(j.get("postedOn", "")),
                raw=j,
            ))
        offset += page
        if offset >= data.get("total", 0):
            break
        time.sleep(0.6)  # be polite
    return jobs


def fetch_remotive(search_text: str) -> list[Job]:
    url = f"https://remotive.com/api/remote-jobs?search={quote(search_text)}"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append(Job(
            source="remotive", company=j.get("company_name", ""),
            title=j.get("title", ""),
            location=j.get("candidate_required_location", "Remote"),
            url=j.get("url", ""),
            posted=(j.get("publication_date") or "")[:10],
            remote=True, raw=j,
        ))
    return jobs



def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (existing
    env vars win). Keeps secrets out of committed config files."""
    import os
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if k and k not in os.environ:
            os.environ[k] = v


def fetch_adzuna(search_text: str, where: str, cfg: dict) -> list[Job]:
    """Aggregator with real geo-filtering. Free key: developer.adzuna.com"""
    import os
    app_id = cfg.get("app_id") or os.environ.get("ADZUNA_APP_ID", "")
    app_key = cfg.get("app_key") or os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        print("  ! adzuna: needs app_id + app_key (config or ADZUNA_APP_ID/"
              "ADZUNA_APP_KEY in .env) — free at developer.adzuna.com")
        return []
    jobs = []
    for page in (1, 2):
        url = (f"https://api.adzuna.com/v1/api/jobs/us/search/{page}"
               f"?app_id={app_id}&app_key={app_key}"
               f"&what={quote(search_text)}&where={quote(where)}"
               f"&distance={cfg.get('distance_km', 40)}&results_per_page=50"
               f"&max_days_old={cfg.get('max_days_old', 21)}"
               f"&content-type=application/json")
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("results", []):
            jobs.append(Job(
                source="adzuna", company=(j.get("company") or {}).get("display_name", ""),
                title=j.get("title", ""),
                location=(j.get("location") or {}).get("display_name", ""),
                url=j.get("redirect_url", ""),
                posted=(j.get("created") or "")[:10],
                raw=j,
            ))
        time.sleep(0.6)
    return jobs


# --------------------------------------------------------------------------- #
#  Dates
# --------------------------------------------------------------------------- #
from datetime import timedelta

def workday_posted_to_iso(text: str) -> str:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> ISO date."""
    t = (text or "").lower()
    today = datetime.now().date()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return ""


def parse_since(s: str) -> datetime | None:
    """'24h', '1d', '7d', '2w' -> cutoff datetime."""
    m = re.fullmatch(r"(\d+)([hdw])", s.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    hours = n * {"h": 1, "d": 24, "w": 168}[unit]
    return datetime.now() - timedelta(hours=hours)


# --------------------------------------------------------------------------- #
#  Filtering
# --------------------------------------------------------------------------- #
def _word_hit(term: str, text: str) -> bool:
    """Whole-word match so 'bi' hits 'BI Engineer' but not 'relia-BI-lity'."""
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def title_matches(title: str, query: str) -> bool:
    """Query matches as an adjacent PHRASE (case-insensitive), so
       'data engineer' hits 'Senior Data Engineer II' but not
       'Software Engineer, Big Data'. '|' gives alternatives per word:
       'data|analytics engineer' -> (data or analytics) engineer."""
    parts = [f"(?:{'|'.join(re.escape(o) for o in w.split('|'))})"
             for w in query.lower().split()]
    # allow up to 2 intervening words: 'data engineer' also hits
    # 'Data and Ontology Engineer' / 'Data Movement Platform' — but word
    # ORDER still matters, so 'Engineer, Big Data' stays rejected.
    pattern = r"\b" + r"\W+(?:\w+\W+){0,2}".join(parts) + r"\b"
    return re.search(pattern, title.lower()) is not None


def title_excluded(title: str, exclude: list) -> bool:
    t = title.lower()
    return any(_word_hit(x.lower(), t) for x in exclude or [])


def location_excluded(loc: str, exclude: list) -> bool:
    l = loc.lower()
    return any(x.lower() in l for x in exclude or [])


def location_matches(loc: str, wanted: list[str], remote_flag: bool) -> bool:
    if not wanted:
        return True
    l = loc.lower()
    for w in wanted:
        w = w.strip().lower()
        if not w:
            continue
        if w == "remote" and (remote_flag or "remote" in l):
            return True
        if w in l:
            return True
    return False



# --------------------------------------------------------------------------- #
#  Seniority boost — flag junior-friendly titles and float them to the top
# --------------------------------------------------------------------------- #
DEFAULT_BOOST = ["junior", "jr", "entry level", "entry-level", "associate",
                 "new grad", "early career", "graduate"]

def is_boosted(title: str, boost_terms: list) -> bool:
    t = title.lower()
    # senior/lead/staff prefixes veto the boost ("Senior Data Engineer I")
    if re.search(r"\b(senior|sr\.?|staff|principal|lead)\b", t):
        return False
    if any(_word_hit(b.lower(), t) for b in boost_terms or []):
        return True
    # 'Data Engineer I' / 'Data Engineer 1' (level-one roles)
    return re.search(r"\b(i|1)\s*$", t) is not None


# --------------------------------------------------------------------------- #
#  Seen-tracking (SQLite)
# --------------------------------------------------------------------------- #
def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS seen
                   (uid TEXT PRIMARY KEY, first_seen TEXT, title TEXT,
                    company TEXT, location TEXT, url TEXT)""")
    return con


def mark_and_flag_new(jobs: list[Job]) -> set[str]:
    con = db_connect()
    new_uids = set()
    now = datetime.now().isoformat(timespec="seconds")
    for j in jobs:
        cur = con.execute("SELECT 1 FROM seen WHERE uid=?", (j.uid,))
        if cur.fetchone() is None:
            new_uids.add(j.uid)
            con.execute("INSERT INTO seen VALUES (?,?,?,?,?,?)",
                        (j.uid, now, j.title, j.company, j.location, j.url))
    con.commit()
    con.close()
    return new_uids





def fetch_usajobs(cfg: dict) -> list:
    """USAJobs.gov official REST API (free key: developer.usajobs.gov).
    Full JD text arrives in the search payload — no detail fetches needed.
    Config:
      usajobs:
        email: you@example.com          # used as User-Agent per their API rules
        api_key: XXXX                   # or set env var USAJOBS_API_KEY
        keywords: [data engineer, data analyst]
        locations: [Orlando Florida, Houston Texas, Austin Texas]
        days: 30                        # only postings from the last N days
    """
    import os
    key = cfg.get("api_key") or os.environ.get("USAJOBS_API_KEY", "")
    email = cfg.get("email") or os.environ.get("USAJOBS_EMAIL", "")
    if not key or not email:
        print("  ! usajobs: needs email + api_key (or USAJOBS_API_KEY env var) — "
              "free signup at developer.usajobs.gov")
        return []
    headers = {"Host": "data.usajobs.gov", "User-Agent": email,
               "Authorization-Key": key}
    jobs, seen_ids = [], set()
    loc_param = ";".join(cfg.get("locations", []))
    for kw in cfg.get("keywords", []):
        params = {"Keyword": kw, "ResultsPerPage": 250,
                  # only postings you're actually eligible for — internal-only
                  # "area of consideration" announcements never reach the report
                  "HiringPath": cfg.get("hiring_path", "public")}
        if loc_param:
            params["LocationName"] = loc_param
        if cfg.get("days"):
            params["DatePosted"] = cfg["days"]
        # GS-grade band: federal seniority lives in pay grade, not title words
        if cfg.get("pay_grade_low"):
            params["PayGradeLow"] = str(cfg["pay_grade_low"]).zfill(2)
        if cfg.get("pay_grade_high"):
            params["PayGradeHigh"] = str(cfg["pay_grade_high"]).zfill(2)
        try:
            r = requests.get("https://data.usajobs.gov/api/search",
                             headers=headers, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            items = r.json().get("SearchResult", {}).get("SearchResultItems", [])
        except Exception as e:
            print(f"  ! usajobs '{kw}': {e}")
            continue
        for item in items:
            jid = item.get("MatchedObjectId", "")
            if not jid or jid in seen_ids:
                continue
            d = item.get("MatchedObjectDescriptor", {})
            seen_ids.add(jid)
            jobs.append(Job(
                source="usajobs",
                company=d.get("OrganizationName", "usajobs"),
                title=d.get("PositionTitle", ""),
                location=d.get("PositionLocationDisplay", ""),
                url=d.get("PositionURI", ""),
                posted=(d.get("PublicationStartDate", "") or "")[:10],
                raw=d,
            ))
        time.sleep(0.5)
    print(f"[usajobs] {len(jobs)} postings across {len(cfg.get('keywords', []))} keyword(s)")
    return jobs


def fetch_careercircle(cfg: dict) -> list:
    """CareerCircle (Allegis staffing board). Server-rendered HTML search:
    /jobs?keyword=X&location=state~FL~Florida State~lat~lng&page=N
    Requires beautifulsoup4. Config:
      careercircle:
        locations:
          - "state~FL~Florida State~27.543598~-81.82069"
          - "MetroArea~Greater Orlando~Greater Orlando~28.538336~-81.379234"
        keywords: [data engineer, data analyst]
        pages: 2
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  ! careercircle source needs: pip install beautifulsoup4")
        return []
    jobs, seen_ids = [], set()
    # accept a list of location tuples (repeated params, UI caps at 10)
    # or a single string for backward compat
    locs = cfg.get("locations") or cfg.get("location", "")
    if isinstance(locs, str):
        locs = [locs] if locs else []
    for kw in cfg.get("keywords", []):
        for page in range(1, cfg.get("pages", 2) + 1):
            params = {"keyword": kw, "location": locs}
            if page > 1:
                params["page"] = page
            try:
                r = requests.get("https://www.careercircle.com/jobs",
                                 params=params, headers=UA, timeout=TIMEOUT)
                r.raise_for_status()
            except Exception as e:
                print(f"  ! careercircle '{kw}' p{page}: {e}")
                break
            soup = BeautifulSoup(r.text, "html.parser")
            anchors = soup.select('a[href*="/jobs/all/"]')
            found_this_page = 0
            for a in anchors:
                m = re.search(r"/jobs/all/all/usa/([a-z-]+)/([a-z-]+)/([0-9a-f-]{36})",
                              a.get("href", ""))
                if not m or m.group(3) in seen_ids:
                    continue
                title = a.get_text(" ", strip=True)
                if not title:
                    continue
                # walk up to the card container (the ancestor mentioning Posted)
                card = a
                for _ in range(6):
                    if card.parent is None:
                        break
                    card = card.parent
                    if "Posted" in card.get_text():
                        break
                text = card.get_text("\n", strip=True)
                comp = ""
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                if title in lines:
                    i = lines.index(title)
                    if i + 1 < len(lines):
                        comp = lines[i + 1].split("\u2022")[0].strip()
                locm = re.search(r"([A-Za-z .'-]+,\s*[A-Z]{2})", text)
                posted = ""
                pm = re.search(r"Posted\s+(\d+)\s+days?\s+ago", text)
                if pm:
                    posted = (datetime.now() - timedelta(days=int(pm.group(1)))).strftime("%Y-%m-%d")
                elif re.search(r"Posted\s+(yesterday)", text):
                    posted = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                elif re.search(r"Posted\s+today", text):
                    posted = datetime.now().strftime("%Y-%m-%d")
                seen_ids.add(m.group(3))
                found_this_page += 1
                jobs.append(Job(
                    source="careercircle",
                    company=comp or "careercircle",
                    title=title,
                    location=locm.group(1) if locm else m.group(2).replace("-", " ").title(),
                    url=f"https://www.careercircle.com/jobs/all/all/usa/{m.group(1)}/{m.group(2)}/{m.group(3)}",
                    posted=posted,
                    raw={"id": m.group(3)},
                ))
            if found_this_page == 0:
                if "jobs found" in r.text and page == 1:
                    Path("careercircle_debug.html").write_text(r.text)
                    print("  ! careercircle: results present but parser found 0 cards — "
                          "markup may have changed; saved careercircle_debug.html")
                break   # no results or last page
            time.sleep(0.8)
    print(f"[careercircle] {len(jobs)} postings across {len(cfg.get('keywords', []))} keyword(s)")
    return jobs


# --------------------------------------------------------------------------- #
#  JD saving — write descriptions of NEW matching postings to a folder,
#  building a dated corpus of the current target market (feeds jd_harvest.py)
# --------------------------------------------------------------------------- #
import hashlib
import html as _html


def strip_html(raw: str) -> str:
    """HTML -> plain text, no extra deps. Good enough for keyword mining."""
    text = _html.unescape(_html.unescape(raw or ""))  # Greenhouse double-escapes
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def fetch_description(job: Job) -> str:
    """Get the JD text for one job. Lever/Ashby/Remotive: already in raw.
    Greenhouse/Workday: one polite detail fetch. Adzuna: truncated snippet."""
    try:
        if job.source == "lever":
            parts = [job.raw.get("descriptionPlain") or strip_html(job.raw.get("description", ""))]
            for lst in job.raw.get("lists", []) or []:
                parts.append(lst.get("text", ""))
                parts.append(strip_html(lst.get("content", "")))
            return "\n".join(p for p in parts if p)
        if job.source == "ashby":
            return (job.raw.get("descriptionPlain")
                    or strip_html(job.raw.get("descriptionHtml", "")))
        if job.source == "remotive":
            return strip_html(job.raw.get("description", ""))
        if job.source == "usajobs":
            det = (job.raw.get("UserArea", {}) or {}).get("Details", {}) or {}
            duties = det.get("MajorDuties", [])
            if isinstance(duties, str):
                duties = [duties]
            parts = [det.get("JobSummary", ""), "\n".join(duties),
                     det.get("QualificationSummary", "")]
            return "\n\n".join(p for p in parts if p)
        if job.source == "careercircle":
            r = requests.get(job.url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return strip_html(r.text)
        if job.source == "adzuna":
            # Snippet-only by design: adzuna.com details pages bot-block
            # automated fetches, so chasing the full JD mostly fails and
            # occasionally saves challenge-page junk. The truncated snippet
            # still carries real keyword signal for the corpus.
            return strip_html(job.raw.get("description", ""))
        if job.source == "greenhouse":
            jid = job.raw.get("id")
            if not jid:
                return ""
            r = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{job.company}/jobs/{jid}",
                headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return strip_html(r.json().get("content", ""))
        if job.source == "workday":
            path = job.raw.get("externalPath", "")
            if not path:
                return ""
            m = re.match(r"https://([^/]+)/en-US/([^/]+)", job.url)
            if not m:
                return ""
            host, site = m.group(1), m.group(2)
            r = requests.get(f"https://{host}/wday/cxs/{job.company}/{site}{path}",
                             headers={**UA, "Accept": "application/json"}, timeout=TIMEOUT)
            r.raise_for_status()
            info = r.json().get("jobPostingInfo", {}) or {}
            return strip_html(info.get("jobDescription", ""))
    except Exception as e:
        print(f"    ! JD fetch failed for {job.company}/{job.title[:30]}: {e}")
    return ""


def save_jds(jobs: list, new_uids: set, outdir: Path) -> int:
    """Save descriptions of new matching postings to outdir/YYYY-MM/*.txt"""
    new_jobs = [j for j in jobs if j.uid in new_uids]
    if not new_jobs:
        return 0
    month_dir = outdir / datetime.now().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for j in new_jobs:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{j.company}-{j.title}").strip("-")[:80]
        tag = hashlib.md5(j.uid.encode()).hexdigest()[:6]
        path = month_dir / f"{slug}-{tag}.txt"
        if path.exists():
            continue                 # already saved — skip BEFORE fetching
        text = fetch_description(j)
        if len(text) < 200:          # too short to be a usable JD
            continue
        header = (f"TITLE: {j.title}\nCOMPANY: {j.company}\nLOCATION: {j.location}\n"
                  f"SOURCE: {j.source}\nPOSTED: {j.posted}\nURL: {j.url}\n"
                  f"SAVED: {datetime.now().strftime('%Y-%m-%d')}\n\n")
        path.write_text(header + text, encoding="utf-8")
        saved += 1
        if j.source in ("greenhouse", "workday", "adzuna", "careercircle"):
            time.sleep(0.5)          # be polite on detail fetches
    return saved


# --------------------------------------------------------------------------- #
#  Runner
# --------------------------------------------------------------------------- #
def load_config(path: Path = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG
    if path.exists():
        return yaml.safe_load(path.read_text()) or {}
    print(f"!! config not found: {path} — create one (see README)", file=sys.stderr)
    return {}


def collect(args, cfg) -> list[Job]:
    wanted_sources = set(args.sources.split(",")) if args.sources else None
    def on(name): return wanted_sources is None or name in wanted_sources

    all_jobs: list[Job] = []
    def grab(label, fn, *a):
        try:
            got = fn(*a)
            all_jobs.extend(got)
            print(f"  [{label:<28}] {len(got):>4} postings")
        except requests.HTTPError as e:
            print(f"  [{label:<28}]  auth or slug problem (401/403 = credentials, 404 = bad slug)")
        except Exception as e:
            print(f"  [{label:<28}]  error: {e}")
        time.sleep(0.4)

    print("Fetching…")
    if on("greenhouse"):
        for slug in cfg.get("greenhouse", []):
            grab(f"greenhouse/{slug}", fetch_greenhouse, slug)
    if on("lever"):
        for slug in cfg.get("lever", []):
            grab(f"lever/{slug}", fetch_lever, slug)
    if on("ashby"):
        for slug in cfg.get("ashby", []):
            grab(f"ashby/{slug}", fetch_ashby, slug)
    if on("workday"):
        for entry in cfg.get("workday", []):
            for t in getattr(args, "title_list", [args.title]):
                grab(f"workday/{entry['tenant']}:{t[:18]}", fetch_workday, entry, t)
    if on("remotive"):
        grab("remotive", fetch_remotive, args.title)
    if on("usajobs") and cfg.get("usajobs"):
        grab("usajobs", fetch_usajobs, cfg["usajobs"])
    if on("careercircle") and cfg.get("careercircle"):
        grab("careercircle", fetch_careercircle, cfg["careercircle"])
    if on("adzuna") and cfg.get("adzuna"):
        az = cfg["adzuna"]
        queries = az.get("queries") or ([args.title] if args.title else [])
        wheres = az.get("wheres") or ([args.adzuna_where] if args.adzuna_where
                  else (args.locations.split(",")[:1] if args.locations else []))
        for q in queries:
            for w in wheres:
                grab(f"adzuna/{w}", fetch_adzuna, q, w, az)
    return all_jobs


HTML_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>jobhunt — {count} matches</title><style>
body{{font-family:-apple-system,Segoe UI,sans-serif;margin:2rem;background:#f7f8fa;color:#1c2333}}
h1{{font-size:1.25rem}} .meta{{color:#68707f;font-size:.85rem;margin-bottom:1rem}}
table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
th,td{{padding:.55rem .8rem;text-align:left;border-bottom:1px solid #e6e8ee;font-size:.9rem}}
th{{background:#1c2333;color:#fff;cursor:pointer;user-select:none;position:sticky;top:0}}
tr:hover{{background:#f0f4ff}} a{{color:#1a56db;text-decoration:none;font-weight:600}}
a:hover{{text-decoration:underline}}
.new{{background:#e8f7ee;color:#137a3d;font-size:.72rem;font-weight:700;
     padding:.15rem .45rem;border-radius:99px;margin-left:.4rem}}
.src{{color:#68707f;font-size:.78rem}}
.fit{{font-size:.72rem;font-weight:700;padding:.15rem .5rem;border-radius:99px;cursor:help}}
.fit-strong{{background:#e8f7ee;color:#137a3d}}
.fit-maybe{{background:#fff3d6;color:#8a5a00}}
.fit-skip{{background:#f1f2f5;color:#8a90a0}}
.bar{{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;margin:.8rem 0}}
.bar input[type=text],.bar select{{padding:.4rem .6rem;border:1px solid #ccd2dd;
  border-radius:6px;font-size:.85rem;background:#fff}}
.bar label{{font-size:.82rem;color:#454d5e;display:flex;gap:.3rem;align-items:center}}
#shown{{margin-left:auto;font-size:.82rem;color:#68707f}}</style></head><body>
<h1>jobhunt report</h1>
<div class="meta">{count} matches · titles: {titles} · locations: {locs} · generated {ts}
 · click a header to sort</div>
<div class="bar">
<input type="text" id="fSearch" placeholder="search anything…">
<select id="fCompany"><option value="">All companies</option></select>
<select id="fLoc"><option value="">All locations</option></select>
<select id="fSrc"><option value="">All sources</option></select>
<select id="fFit"><option value="">All fits</option><option>strong</option><option>maybe</option><option>skip</option><option value="—">unrated</option></select>
<label><input type="checkbox" id="fJr"> JR-friendly only</label>
<label><input type="checkbox" id="fNew"> NEW only</label>
<span id="shown"></span>
</div>
<table id="t"><thead><tr>
<th>Posted</th><th>Title</th><th>Company</th><th>Location</th><th>Source</th><th>Fit</th>
</tr></thead><tbody>
{rows}
</tbody></table>
<script>
document.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{{
 const tb=document.querySelector('#t tbody');
 const rows=[...tb.rows].sort((a,b)=>a.cells[i].innerText.localeCompare(b.cells[i].innerText));
 if(th.dataset.asc==='1'){{rows.reverse();th.dataset.asc='0'}}else{{th.dataset.asc='1'}}
 rows.forEach(r=>tb.appendChild(r));}});

const tb=document.querySelector('#t tbody');
const allRows=[...tb.rows];
function fill(sel,col){{
 const vals=[...new Set(allRows.map(r=>r.cells[col].innerText.trim()))].sort();
 vals.forEach(v=>{{const o=document.createElement('option');o.value=v;o.textContent=v;
  document.querySelector(sel).appendChild(o);}});
}}
fill('#fCompany',2); fill('#fLoc',3); fill('#fSrc',4);
function apply(){{
 const q=document.querySelector('#fSearch').value.toLowerCase();
 const co=document.querySelector('#fCompany').value;
 const lo=document.querySelector('#fLoc').value;
 const sr=document.querySelector('#fSrc').value;
 const ft=document.querySelector('#fFit').value;
 const jr=document.querySelector('#fJr').checked;
 const nw=document.querySelector('#fNew').checked;
 let n=0;
 allRows.forEach(r=>{{
  let ok=true;
  if(q && !r.innerText.toLowerCase().includes(q)) ok=false;
  if(co && r.cells[2].innerText.trim()!==co) ok=false;
  if(lo && r.cells[3].innerText.trim()!==lo) ok=false;
  if(sr && r.cells[4].innerText.trim()!==sr) ok=false;
  if(ft && r.cells[5].innerText.trim()!==ft) ok=false;
  if(jr && !r.cells[1].innerHTML.includes('JR-FRIENDLY')) ok=false;
  if(nw && !r.cells[1].innerHTML.includes('>NEW<')) ok=false;
  r.style.display=ok?'':'none';
  if(ok) n++;
 }});
 document.querySelector('#shown').textContent=n+' shown';
}}
['fSearch','fCompany','fLoc','fSrc','fFit','fJr','fNew'].forEach(id=>{{
 const el=document.getElementById(id);
 el.addEventListener(el.tagName==='INPUT'&&el.type==='text'?'input':'change',apply);
}});
apply();
</script></body></html>"""


def write_html(path, jobs, new_uids, titles, locations, verdicts=None):
    import html as _h
    verdicts = verdicts or {}
    rows = []
    for j in jobs:
        badge = '<span class="new">NEW</span>' if j.uid in new_uids else ""
        if getattr(j, "boosted", False):
            badge = '<span class="new" style="background:#fff3d6;color:#8a5a00">JR-FRIENDLY</span>' + badge
        v = verdicts.get(j.uid)
        if v:
            tip = _h.escape(f"{v['one_liner']}  [{v['seniority_fit']} · "
                            f"{v['remote_truth']} · ghost:{v['ghost_risk']}]", quote=True)
            fit_cell = (f'<td><span class="fit fit-{v["fit"]}" '
                        f'title="{tip}">{v["fit"]}</span></td>')
        else:
            fit_cell = '<td class="src">—</td>'
        rows.append(
            f"<tr><td>{_h.escape(j.posted or '—')}</td>"
            f'<td><a href="{_h.escape(j.url)}" target="_blank">{_h.escape(j.title)}</a>{badge}</td>'
            f"<td>{_h.escape(j.company)}</td><td>{_h.escape(j.location)}</td>"
            f'<td class="src">{_h.escape(j.source)}</td>{fit_cell}</tr>')
    doc = HTML_TMPL.format(count=len(jobs), titles=_h.escape(", ".join(titles)),
                           locs=_h.escape(locations or "any"),
                           ts=datetime.now().strftime("%Y-%m-%d %H:%M"),
                           rows="\n".join(rows))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="Personal job-search aggregator")
    p.add_argument("--title", required=False, default="",
                   help='Single title query. Terms ANDed; OR with |, e.g. "data|analytics engineer"')
    p.add_argument("--titles", default="",
                   help='Comma list of title queries, matched as ANY. e.g. "data engineer,analytics engineer,etl developer"')
    p.add_argument("--locations", default="",
                   help='Comma list matched against posting location, e.g. "Orlando,Tampa,Lakeland,FL,Remote"')
    p.add_argument("--sources", default="",
                   help="Comma list to limit sources: greenhouse,lever,ashby,workday,remotive,adzuna")
    p.add_argument("--adzuna-where", default="", help="City for Adzuna geo search (defaults to first --locations entry)")
    p.add_argument("--new-only", action="store_true", help="Only show jobs not seen in previous runs")
    p.add_argument("--since", default="", help="Only jobs posted within a window: 24h, 1d, 3d, 7d, 2w. Jobs with no date are kept (marked '??').")
    p.add_argument("--csv", default="", help="Also write results to this CSV file (url column included)")
    p.add_argument("--html", default="jobs.html",
                   help="Clickable HTML report path (written EVERY run; default jobs.html). Use --html '' to skip.")
    p.add_argument("--config", default="", help="Path to a config YAML (default: companies.yaml). Keep several for different search profiles.")
    p.add_argument("--check-sources", action="store_true", help="Verify every configured slug responds, then exit")
    p.add_argument("--save-jds", default="",
                   help="Save descriptions of NEW matching postings as .txt into this folder "
                        "(dated subfolders; feeds jd_harvest.py). Also settable in config as search: save_jds: <dir>")
    args = p.parse_args()

    cfg = load_config(args.config or None)

    # Resolve the title list: --titles > --title > companies.yaml search block > default
    search_cfg = cfg.get("search", {}) or {}
    if args.titles:
        titles = [t.strip() for t in args.titles.split(",") if t.strip()]
    elif args.title:
        titles = [args.title]
    else:
        titles = search_cfg.get("titles") or ["data engineer"]
    args.title_list = titles
    args.title = titles[0]  # used as server-side searchText hint
    if not args.locations and search_cfg.get("locations"):
        args.locations = ",".join(search_cfg["locations"])
    # config-driven default posting-age window (CLI --since overrides;
    # --since all disables even the config default)
    if not args.since and search_cfg.get("since"):
        args.since = str(search_cfg["since"])
    if args.since.lower() == "all":
        args.since = ""

    if args.check_sources:
        args.locations = ""
        jobs = collect(args, cfg)
        print(f"\nTotal postings reachable: {len(jobs)}")
        return

    jobs = collect(args, cfg)

    wanted_locs = [w for w in args.locations.split(",") if w.strip()]
    ex_titles = search_cfg.get("exclude_titles", [])
    ex_locs = search_cfg.get("exclude_locations", [])
    filtered = [j for j in jobs
                if any(title_matches(j.title, t) for t in args.title_list)
                and not title_excluded(j.title, ex_titles)
                and location_matches(j.location, wanted_locs, j.remote)
                and not location_excluded(j.location, ex_locs)]

    # posted-within window
    if args.since:
        cutoff = parse_since(args.since)
        if cutoff is None:
            sys.exit(f"--since format not recognized: '{args.since}' (use 24h, 1d, 7d, 2w)")
        kept = []
        for j in filtered:
            if not j.posted:
                kept.append(j)          # unknown date: keep, flagged in output
                continue
            try:
                if datetime.fromisoformat(j.posted[:10]) >= cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
                    kept.append(j)
            except ValueError:
                kept.append(j)
        filtered = kept

    # dedupe by (company,title,location)
    seen_keys, deduped = set(), []
    for j in filtered:
        k = j.url or (j.company.lower(), j.title.lower(), j.location.lower())
        if k not in seen_keys:
            seen_keys.add(k)
            deduped.append(j)

    new_uids = mark_and_flag_new(deduped)

    jd_dir = args.save_jds or search_cfg.get("save_jds", "")
    if jd_dir:
        n_saved = save_jds(deduped, new_uids, Path(jd_dir))
        if n_saved:
            print(f"\nSaved {n_saved} new JD(s) -> {jd_dir}/{datetime.now().strftime('%Y-%m')}/")

    # Triage: free rules-engine verdicts for the report's Fit column.
    # Fail-safe — a triage problem must never kill the report.
    verdicts = {}
    try:
        import triage
        n_triaged = triage.run_rules_quiet(Path(jd_dir) if jd_dir else None)
        if n_triaged:
            print(f"Triaged {n_triaged} new posting(s)  [engine: rules]")
        verdicts = triage.get_verdicts([j.uid for j in deduped])
    except Exception as e:
        print(f"  ! triage skipped: {e}")

    if args.new_only:
        deduped = [j for j in deduped if j.uid in new_uids]

    boost_terms = search_cfg.get("boost_titles", DEFAULT_BOOST)
    for j in deduped:
        j.boosted = is_boosted(j.title, boost_terms)
    deduped.sort(key=lambda j: (j.posted or ""), reverse=True)
    deduped.sort(key=lambda j: not j.boosted)   # stable: boosted rise, dates keep order

    print(f"\n{'='*100}")
    print(f"  {len(deduped)} match(es) for titles={args.title_list} locations='{args.locations or 'any'}'"
          f"{'  (new only)' if args.new_only else ''}{f'  (posted ≤{args.since})' if args.since else ''}")
    print(f"{'='*100}")
    for j in deduped:
        star = "★ NEW " if j.uid in new_uids else "      "
        star = ("▲JR " if getattr(j, "boosted", False) else "    ") + star
        print(f"{star}{(j.posted or '  ??')[:10]:>10}  {j.title[:52]:<52}  "
              f"{j.company[:16]:<16}  {j.location[:34]:<34}")
        print(f"{'':16}{j.url}")

    if args.csv and deduped:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["new", "posted", "title", "company", "location", "source", "url"])
            for j in deduped:
                w.writerow(["NEW" if j.uid in new_uids else "", j.posted, j.title,
                            j.company, j.location, j.source, j.url])
        print(f"\nWrote {len(deduped)} rows → {args.csv}")

    if args.html and deduped:
        write_html(args.html, deduped, new_uids, args.title_list, args.locations, verdicts)
        from pathlib import Path as _P
        print(f"\n>>> Clickable report: {_P(args.html).resolve()}")
        print(">>> Open it with:  open jobs.html")


if __name__ == "__main__":
    main()
