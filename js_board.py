#!/usr/bin/env python3
"""
js_board.py — Playwright fallback for job boards that are truly JS-rendered
and expose no JSON API.

Try the JSON approach FIRST (see jobhunt.py) — open the board, hit F12 →
Network → XHR/Fetch, reload, and look for a request returning job JSON.
90% of "JavaScript job boards" have one, and it's faster and sturdier than
driving a browser. Use this module only for the stubborn 10%.

Setup (one time):
    pip install playwright
    playwright install chromium

Usage:
    python js_board.py --config board_example.yaml --title "data engineer" --location Orlando

The YAML config describes any board with three CSS selectors:

    url: "https://example.com/careers?q={title}&loc={location}"
    wait_for: ".job-card"          # selector that appears when results load
    card: ".job-card"              # one element per job
    fields:
      title: ".job-card__title"    # selectors relative to each card
      location: ".job-card__loc"
      link: "a"                    # href is taken from this element
    next_button: "button.load-more"   # optional: clicked until it disappears
    max_pages: 5
"""
from __future__ import annotations


import argparse
import sys
from urllib.parse import quote

import yaml

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright not installed. Run:\n  pip install playwright && playwright install chromium")


def scrape(config: dict, title: str, location: str) -> list[dict]:
    url = config["url"].format(title=quote(title), location=quote(location))
    jobs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        try:
            page.wait_for_selector(config["wait_for"], timeout=20000)
        except PWTimeout:
            browser.close()
            sys.exit(f"Timed out waiting for '{config['wait_for']}' — "
                     "selector wrong, or the site is blocking headless browsers.")

        for _ in range(config.get("max_pages", 3)):
            for card in page.query_selector_all(config["card"]):
                item = {}
                for field, sel in config["fields"].items():
                    el = card.query_selector(sel)
                    if el is None:
                        item[field] = ""
                    elif field == "link":
                        href = el.get_attribute("href") or ""
                        if href.startswith("/"):
                            from urllib.parse import urljoin
                            href = urljoin(url, href)
                        item[field] = href
                    else:
                        item[field] = (el.inner_text() or "").strip()
                if item.get("title"):
                    jobs.append(item)

            nxt = config.get("next_button")
            if not nxt:
                break
            btn = page.query_selector(nxt)
            if btn is None or not btn.is_enabled():
                break
            btn.click()
            page.wait_for_timeout(1800)  # let new cards render

        browser.close()

    # dedupe on link
    seen, out = set(), []
    for j in jobs:
        if j.get("link") not in seen:
            seen.add(j.get("link"))
            out.append(j)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--title", default="data engineer")
    ap.add_argument("--location", default="")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    jobs = scrape(cfg, args.title, args.location)

    print(f"\n{len(jobs)} jobs scraped:\n")
    for j in jobs:
        print(f"  {j.get('title','')[:60]:<60}  {j.get('location','')[:30]}")
        print(f"    {j.get('link','')}")


if __name__ == "__main__":
    main()
