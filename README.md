# resume-tailor

**Generate a job-specific resume and cover letter from a single source of
truth, scored against a keyword corpus weighted by real job-market data.**

Part of a three-repo job-search pipeline:
[job-hunt] discovers postings via ATS JSON APIs → **resume-tailor** generates
the application materials → [keyword_finder] provides the shared keyword
corpus and matching engine. Built and used daily in my own data engineering
job search.

## The core idea

Stop editing resume files. All content lives in `master_resume.yaml` — a
**bullet bank** of every true, interview-defensible accomplishment, each
tagged with the skills it demonstrates. For each job description, the engine:

1. Extracts and weights the JD's keywords (word-boundary matching with fuzzy
   fallback, category weights, optional blending with empirical frequency
   data mined from real postings)
2. Ranks every bullet and project by relevance to *this* JD
3. Selects and reorders — projects compete for 3 slots, bullets re-rank
   within each job between a floor and ceiling — **never inventing content,
   only choosing emphasis**
4. Emits an ATS-clean docx/PDF plus a gap report: which JD keywords you
   cover, which you're missing, and how each project scored

The gap report drives a feedback loop: missing keywords you can *truthfully*
claim become YAML edits, and every future resume inherits them.

## Tools

| Script | Purpose |
|---|---|
| `resume_tailor.py` | JD (file or URL) → tailored resume docx/PDF + gap report |
| `cover_letter.py` | Same engine → one-page letter from narrative proof blocks, with a mandatory human-filled `[WHY THIS COMPANY]` slot |
| `bullet_harvest.py` | Mine a folder of old resumes → deduplicated bullet inventory (fuzzy clustering) |
| `jd_harvest.py` | Mine a folder of saved JDs → keyword frequency data + candidate new keywords |

Niceties: Greenhouse/Lever/Ashby URLs fetch clean JD text via their JSON
APIs; `--baseline old_resume.docx` prints an honest before/after match score;
`--pdf` exports via LibreOffice or MS Word; page layout uses keep-with-next
so headings and job blocks never split across pages.

## Quick start

```bash
git clone https://github.com/jeffboerger/resume-tailor && cd resume-tailor
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp master_resume.example.yaml master_resume.yaml
# edit master_resume.yaml: your contact info and your true accomplishments

python resume_tailor.py --jd path/to/jd.txt --company Acme --role "Data Engineer" --pdf
python cover_letter.py  --jd path/to/jd.txt --company Acme --role "Data Engineer"
```

The keyword corpus resolves automatically: a local `data/` folder if present,
else a sibling `../keyword_finder/data` checkout, else pass `--keywords-dir`.

If a `keyword_frequencies.csv` (from `jd_harvest.py`) sits in the working
directory, corpus weights are blended with real market frequency —
deliberately gently (0.7x–1.2x), because frequency data is only as current
as the postings it was mined from.

## Honest findings from building this

- **Reordering doesn't move ATS keyword scores** — the parser reads the same
  words in any order. Reordering is for the human 7-second skim; score gains
  come from the gap-report → YAML-edit loop. The `--baseline` flag exists to
  keep the tool honest about this.
- **The truth constraint is the architecture.** The engine can only select
  from pre-written content, so no generated resume can ever claim something
  the bank doesn't contain — and everything in the bank passes the "can I
  talk about this for five minutes in an interview" test.
- **Cover letters need a human slot.** Recruiters pattern-match fully
  generated letters instantly. The letter tool assembles true proof
  paragraphs but always leaves one bracketed sentence that must be written
  by hand, per application.

## License

MIT
