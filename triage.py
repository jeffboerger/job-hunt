#!/usr/bin/env python3
"""
triage.py — triage for new job postings, with swappable engines.

For every saved JD in jds/ that hasn't been triaged yet, produces a
structured verdict and caches it in seen_jobs.sqlite3 (table: triage,
keyed by the same uid jobhunt.py uses). A posting is never triaged twice.

Two engines, same verdict schema:

  rules (DEFAULT — free, offline, deterministic)
      Regex/heuristic classifier: years-of-experience parsing, seniority
      and remote-language detection, hard-no screens (clearance, wrong
      stack), evergreen/ghost-posting signals. The one-liner is assembled
      from whichever rules fired, so every verdict is explainable.

  llm (OPT-IN — requires ANTHROPIC_API_KEY in .env, costs ~pennies/day)
      One Claude Haiku call per posting. Never runs unless you pass --llm.

Usage:
  python triage.py                 # rules engine, everything new
  python triage.py --dry-run       # list pending, classify nothing
  python triage.py --show          # print cached verdicts, newest first
  python triage.py --llm --limit 3 # LLM engine (explicit opt-in only)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Reuse the existing infrastructure instead of duplicating it.
from jobhunt import load_dotenv, DB_PATH, load_config

RULES_VERSION = "rules-v1"
JD_CHAR_LIMIT = 7000

# ---------------------------------------------------------------------------
# Candidate profile — the tuning surface for BOTH engines. The rules engine
# reads the structured fields; the LLM engine reads PROFILE_TEXT. When a
# verdict disagrees with your own read of a posting, this is what you edit.
# ---------------------------------------------------------------------------
STACK_STRONG = ["python", "sql", "dbt", "bigquery", "airflow", "streamlit",
                "etl", "elt", "data pipeline", "data warehouse", "gcp",
                "github actions", "tableau", "pandas"]
STACK_WRONG = ["embedded", "firmware", "kotlin", "swift", " ios ", "android",
               "verilog", "fpga", ".net only", "mainframe", "cobol"]
HARD_NO = ["security clearance", "ts/sci", "top secret", "secret clearance",
           "polygraph", "clearance required", "active clearance"]
GOOD_METROS = ["orlando", "lakeland", "tampa", "houston", "austin", "dallas",
               "celebration", "kissimmee", "sugar land", "the woodlands",
               "plano", "irving", "round rock", "st. petersburg", "brandon"]

PROFILE_TEXT = """\
Candidate: early-career Data Engineer (Dec 2023 CS grad, career-changer).
Strengths: Python, SQL, dbt, BigQuery, Streamlit; runs a live hourly ELT
pipeline (GitHub Actions -> BigQuery -> dbt); real business-operations
background; national-lab data internship. No professional DE title yet.
Targets: Data Engineer, then SWE (Python/data), then Data Analyst.
Locations: Orlando/Lakeland/Tampa, Houston, Austin, Dallas, or remote (US).
Not a fit: 5+ years required, Java/Scala-only stacks, clearances,
unrelated domains (embedded, mobile, hardware)."""


# ===========================================================================
# ENGINE 1 — rules (free, offline, explainable)
# ===========================================================================
RE_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:-|to|–)\s*\d{1,2})?\s*\+?\s*years?", re.I)
RE_SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|architect|director|manager|head of)\b", re.I)
RE_JUNIOR = re.compile(
    r"\b(junior|entry[- ]?level|new[- ]grads?|early[- ]career|recent grads?|"
    r"associate|interns?)\b|\b(?:engineer|analyst|developer)\s+i\b"
    r"|\b0\s*(?:-|to|–)\s*[123]\s*years?", re.I)
RE_NO_REMOTE = re.compile(
    r"remote (?:work )?is not|not (?:a )?remote|no remote|"
    r"relocation (?:is )?required|must (?:be able to )?work on[- ]?site", re.I)
RE_HYBRID = re.compile(
    r"\bhybrid\b|\d\s*days?\s*(?:per week|/week|a week)\s*(?:in|at)\s*(?:the\s*)?office", re.I)
RE_REMOTE = re.compile(
    r"fully remote|100%\s*remote|remote[- ]first|remote within|work from anywhere", re.I)
RE_ONSITE = re.compile(r"\bon[- ]?site\b|\bin[- ]office\b", re.I)
RE_GHOST = re.compile(
    r"always accepting applications|talent (?:community|pool|network|pipeline)|"
    r"future (?:opportunities|openings|roles)|evergreen|general (?:interest|application)|"
    r"this is not (?:a specific|an active)", re.I)
RE_SALARY = re.compile(r"\$\s?(\d{2,3})[,.]?\d{3}")


def _min_years(text: str) -> int | None:
    """Smallest years-of-experience ask in the text (caps at 20 to skip
    '401k' / 'founded 25 years ago' noise)."""
    yrs = [int(m.group(1)) for m in RE_YEARS.finditer(text) if int(m.group(1)) <= 20]
    return min(yrs) if yrs else None


def classify_rules(job: dict) -> dict:
    """Deterministic verdict. Same schema the LLM engine produces, plus the
    one_liner is assembled from the rules that fired — every verdict is
    explainable by grepping this function."""
    title, jd = job["title"], job["jd"]
    text = f"{title}\n{jd}"
    low = text.lower()
    reasons: list[str] = []

    # --- seniority ---------------------------------------------------------
    yrs = _min_years(jd)
    if RE_SENIOR_TITLE.search(title):
        seniority = "senior_only"
        reasons.append("senior-level title")
    elif RE_JUNIOR.search(text):
        seniority = "junior_ok"
        reasons.append("junior-friendly language")
    elif yrs is not None:
        if yrs <= 2:
            seniority, why = "junior_ok", f"asks {yrs}+ yrs"
        elif yrs <= 5:
            seniority, why = "stretch", f"asks {yrs}+ yrs (career-changer bridgeable)"
        else:
            seniority, why = "senior_only", f"asks {yrs}+ yrs"
        reasons.append(why)
    else:
        seniority = "stretch"          # unknowable -> human should look
        reasons.append("seniority unstated")

    # --- remote truth (JD text, not the location field) --------------------
    if RE_NO_REMOTE.search(jd):
        remote = "onsite"
    elif RE_HYBRID.search(jd):
        remote = "hybrid"
    elif RE_REMOTE.search(jd):
        remote = "remote"
    elif RE_ONSITE.search(jd):
        remote = "onsite"
    else:
        remote = "unclear"

    # --- ghost risk ---------------------------------------------------------
    ghost_pts = 0
    if RE_GHOST.search(jd):
        ghost_pts += 2
        reasons.append("evergreen/talent-pool language")
    if len(jd) < 600:
        ghost_pts += 1
        reasons.append("thin JD")
    sal = [int(m.group(1)) for m in RE_SALARY.finditer(jd)]
    if len(sal) >= 2 and min(sal) > 0 and max(sal) / min(sal) > 1.8:
        ghost_pts += 1
        reasons.append("suspiciously wide salary band")
    ghost = "high" if ghost_pts >= 2 else ("medium" if ghost_pts == 1 else "low")

    # --- stack + hard-no screens -------------------------------------------
    hard_no = next((t for t in HARD_NO if t in low), None)
    if hard_no:
        reasons.append(hard_no)
    wrong = [t.strip() for t in STACK_WRONG if t in low]
    hits = [t for t in STACK_STRONG if t in low]
    if hits:
        reasons.append(f"stack: {', '.join(hits[:4])}")
    elif wrong:
        reasons.append(f"wrong stack ({wrong[0]})")

    # --- location -----------------------------------------------------------
    loc_low = job.get("location", "").lower()
    loc_ok = (remote == "remote"
              or any(m in loc_low or m in low for m in GOOD_METROS))

    # --- overall fit ----------------------------------------------------------
    if hard_no or seniority == "senior_only" or (wrong and not hits):
        fit = "skip"
    elif seniority == "junior_ok" and hits and loc_ok and ghost != "high":
        fit = "strong"
    else:
        fit = "maybe"
    if not loc_ok and fit != "skip":
        reasons.append("location off-target")

    one_liner = f"{fit.capitalize()}: " + "; ".join(reasons[:4]) + "."
    return {"fit": fit, "seniority_fit": seniority, "remote_truth": remote,
            "ghost_risk": ghost, "one_liner": one_liner[:200],
            "_raw": json.dumps({"engine": RULES_VERSION, "reasons": reasons,
                                "min_years": yrs})}


# ===========================================================================
# ENGINE 2 — llm (opt-in, ~pennies; never runs without --llm)
# ===========================================================================
MODEL = "claude-haiku-4-5-20251001"
API_URL = "https://api.anthropic.com/v1/messages"

RUBRIC = """\
Assess the job description against the candidate profile. Respond with ONLY
a JSON object — no prose, no markdown fences — with exactly these keys:
  "fit": "strong" | "maybe" | "skip"
  "seniority_fit": "junior_ok" | "stretch" | "senior_only"
  "remote_truth": "remote" | "hybrid" | "onsite" | "unclear"
      (what the JD text supports, not what the location field claims)
  "ghost_risk": "low" | "medium" | "high"
      (high = evergreen/pipeline-posting signs: vague duties, "always
      accepting applications", no team specifics, huge salary range)
  "one_liner": one blunt sentence (max 25 words) naming the deciding factor."""

VALID = {
    "fit": {"strong", "maybe", "skip"},
    "seniority_fit": {"junior_ok", "stretch", "senior_only"},
    "remote_truth": {"remote", "hybrid", "onsite", "unclear"},
    "ghost_risk": {"low", "medium", "high"},
}


def classify_llm(job: dict) -> dict | None:
    """One Haiku call. Returns a verdict dict or None (caller skips so the
    posting retries next run). Treats model output as hostile input."""
    import requests
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("--llm requires ANTHROPIC_API_KEY in .env")
    prompt = (f"{RUBRIC}\n\n--- CANDIDATE PROFILE ---\n{PROFILE_TEXT}\n\n"
              f"--- JOB POSTING ---\nTitle: {job['title']}\n"
              f"Company: {job['company']}\nLocation: {job['location']}\n\n"
              f"{job['jd']}")
    r = requests.post(API_URL, timeout=60, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, json={"model": MODEL, "max_tokens": 300,
             "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    raw = "".join(b.get("text", "") for b in r.json().get("content", [])
                  if b.get("type") == "text")

    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        v = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    for key, allowed in VALID.items():
        val = str(v.get(key, "")).strip().lower()
        if val not in allowed:
            return None
        v[key] = val
    v["one_liner"] = str(v.get("one_liner", "")).strip()[:200]
    if not v["one_liner"]:
        return None
    v["_raw"] = raw.strip()
    return v


# ===========================================================================
# Shared plumbing: JD discovery, uid reconstruction, cache
# ===========================================================================
HEADER_KEYS = ("TITLE", "COMPANY", "LOCATION", "SOURCE", "POSTED", "URL")


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS triage (
        uid TEXT PRIMARY KEY, triaged_at TEXT, model TEXT, fit TEXT,
        seniority_fit TEXT, remote_truth TEXT, ghost_risk TEXT,
        one_liner TEXT, jd_file TEXT, raw_json TEXT)""")
    return con


def parse_jd_file(path: Path) -> dict | None:
    """Reconstruct the jobhunt uid ('{source}|{company}|{url}') from the
    header save_jds() writes, and return the JD body."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"  ! unreadable: {path.name}: {e}")
        return None
    meta, body_start = {}, 0
    for line in text.splitlines(keepends=True):
        m = re.match(r"([A-Z]+):\s?(.*)", line)
        if m and m.group(1) in HEADER_KEYS:
            meta[m.group(1)] = m.group(2).strip()
            body_start += len(line)
        elif line.strip() == "" and meta:
            body_start += len(line)
            break
        elif not meta:
            body_start += len(line)
        else:
            break
    if not all(k in meta for k in ("SOURCE", "COMPANY", "URL")):
        print(f"  ! no header, skipping: {path.name}")
        return None
    return {"uid": f"{meta['SOURCE']}|{meta['COMPANY']}|{meta['URL']}",
            "title": meta.get("TITLE", ""), "company": meta["COMPANY"],
            "location": meta.get("LOCATION", ""),
            "jd": text[body_start:].strip()[:JD_CHAR_LIMIT],
            "file": str(path)}


def find_pending(jd_dir: Path, done: set[str]) -> list[dict]:
    return [j for p in sorted(jd_dir.glob("*/*.txt"))
            if (j := parse_jd_file(p)) and j["uid"] not in done]


FIT_MARK = {"strong": "***", "maybe": " * ", "skip": "   "}


# ===========================================================================
# Programmatic API — used by jobhunt.py to enrich jobs.html. Rules engine
# only: the LLM path stays exclusively behind the explicit --llm CLI flag.
# ===========================================================================
def run_rules_quiet(jd_dir: Path | None = None) -> int:
    """Triage all pending JDs with the free rules engine. Returns the
    number of new verdicts written. Never raises for a missing folder."""
    con = db_connect()
    if jd_dir is None:
        search_cfg = (load_config().get("search") or {})
        jd_dir = Path(search_cfg.get("save_jds") or "jds")
    jd_dir = Path(jd_dir)
    if not jd_dir.exists():
        return 0
    done = {r[0] for r in con.execute("SELECT uid FROM triage")}
    n = 0
    for job in find_pending(jd_dir, done):
        v = classify_rules(job)
        con.execute("INSERT OR REPLACE INTO triage VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job["uid"], datetime.now(timezone.utc).isoformat(),
                     RULES_VERSION, v["fit"], v["seniority_fit"],
                     v["remote_truth"], v["ghost_risk"], v["one_liner"],
                     job["file"], v["_raw"]))
        n += 1
    con.commit()
    return n


def get_verdicts(uids: list[str]) -> dict[str, dict]:
    """Cached verdicts for the given uids, keyed by uid. Chunked to stay
    under SQLite's bound-parameter limit."""
    con = db_connect()
    out: dict[str, dict] = {}
    uids = list(uids)
    for i in range(0, len(uids), 500):
        chunk = uids[i:i + 500]
        rows = con.execute(
            f"SELECT uid, fit, seniority_fit, remote_truth, ghost_risk,"
            f" one_liner FROM triage WHERE uid IN"
            f" ({','.join('?' * len(chunk))})", chunk)
        for uid, fit, sen, rem, ghost, line in rows:
            out[uid] = {"fit": fit, "seniority_fit": sen, "remote_truth": rem,
                        "ghost_risk": ghost, "one_liner": line}
    return out


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Triage saved JDs")
    ap.add_argument("--jd-dir", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--llm", action="store_true",
                    help="use Claude Haiku instead of the free rules engine "
                         "(requires ANTHROPIC_API_KEY; costs ~pennies)")
    args = ap.parse_args()

    con = db_connect()

    if args.show:
        rows = con.execute(
            "SELECT triaged_at, model, fit, seniority_fit, remote_truth,"
            " ghost_risk, one_liner, uid FROM triage"
            " ORDER BY triaged_at DESC").fetchall()
        for t, mdl, fit, sen, rem, ghost, line, uid in rows:
            print(f"{FIT_MARK.get(fit, '   ')} [{fit:<6}] {sen:<11} {rem:<7} "
                  f"ghost:{ghost:<6} {mdl:<12} {t[:16]}\n"
                  f"      {line}\n      {uid}\n")
        print(f"{len(rows)} cached verdict(s)")
        return

    search_cfg = (load_config().get("search") or {})
    jd_dir = Path(args.jd_dir or search_cfg.get("save_jds") or "jds")
    if not jd_dir.exists():
        sys.exit(f"JD folder not found: {jd_dir} — run jobhunt.py with "
                 f"save_jds configured first.")

    pending = find_pending(jd_dir, {r[0] for r in con.execute("SELECT uid FROM triage")})
    if args.limit:
        pending = pending[:args.limit]
    if not pending:
        print("Nothing to triage — all saved JDs have cached verdicts.")
        return

    engine_name = MODEL if args.llm else RULES_VERSION
    print(f"{len(pending)} posting(s) to triage  [engine: {engine_name}]")
    if args.dry_run:
        for job in pending:
            print(f"  - {job['company']}: {job['title']}")
        return

    ok = failed = 0
    for job in pending:
        label = f"{job['company']}: {job['title'][:48]}"
        try:
            v = classify_llm(job) if args.llm else classify_rules(job)
        except Exception as e:                      # API/network errors
            print(f"  ! error for {label}: {e}")
            failed += 1
            continue
        if v is None:
            print(f"  ! malformed verdict for {label} — will retry next run")
            failed += 1
            continue
        con.execute("INSERT OR REPLACE INTO triage VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job["uid"], datetime.now(timezone.utc).isoformat(),
                     engine_name, v["fit"], v["seniority_fit"],
                     v["remote_truth"], v["ghost_risk"], v["one_liner"],
                     job["file"], v["_raw"]))
        con.commit()
        ok += 1
        print(f"  {FIT_MARK[v['fit']]} [{v['fit']:<6}] {label}\n"
              f"        {v['one_liner']}")
        if args.llm:
            time.sleep(0.3)

    print(f"\nDone: {ok} triaged, {failed} failed/skipped. "
          f"Verdicts cached in {DB_PATH.name} (table: triage).")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:      # e.g. `triage.py --show | head`
        sys.exit(0)
