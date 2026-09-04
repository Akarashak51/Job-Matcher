"""
companies_source.py
====================
Loads the master company list from companies.csv (company_name, country,
careers_url, platform_hint) instead of a hardcoded Python list, so adding
or updating companies is just an edit to a spreadsheet - no code change.

The CSV can have messy platform-hint text ("Own Portal", "Workday",
typos, blanks); normalize_platform() maps it onto the small set of
platforms hulk_job_search.py actually knows how to query:
  greenhouse | lever | smartrecruiters | ashby | workday | custom

"custom" means: no public JSON API is known for this platform (Taleo,
in-house "Own Portal" sites, unrecognized text, or a blank cell) - these
get handled by the generic HTML career-page scraper instead.

Each row becomes a dict shaped exactly like the entries that used to be
hand-written in hulk_job_search.COMPANIES, e.g.:
    {"name": "Razorpay", "platform": "greenhouse", "token": None,
     "confirmed": False, "careers_url": "https://razorpay.com/careers",
     "country": "India", "platform_hint_raw": "Lever"}

`token` / `tenant` / `wd_host` / `site` start out as None - the CSV only
gives us a careers URL and a platform *hint*, not the actual API token or
Workday tenant. Those get filled in at runtime by
hulk_job_search.discover_platform_from_url() (and cached to disk by
platform_cache.py) the first time each company is actually searched, so
we don't pay the cost of resolving 300+ companies on every app startup.
"""

import csv
import os
import re

CSV_PATH = os.environ.get(
    "COMPANIES_CSV",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "companies.csv"),
)

# Longest/most-specific keys first so e.g. "smartrecruiters" doesn't get
# accidentally matched by a shorter substring.
_PLATFORM_PATTERNS = [
    (re.compile(r"greenhouse", re.I), "greenhouse"),
    (re.compile(r"smartrecruit", re.I), "smartrecruiters"),
    (re.compile(r"\blever\b", re.I), "lever"),
    (re.compile(r"ashby", re.I), "ashby"),
    (re.compile(r"workday|^work$|^wd\d?$|^wday$", re.I), "workday"),
]


def normalize_platform(raw_hint):
    """Map free-text platform-hint column onto a known platform key, or
    'custom' if it doesn't clearly match one (Taleo, "Own Portal", typos,
    blank cells, anything else with no public JSON job-board API)."""
    text = (raw_hint or "").strip()
    for pattern, key in _PLATFORM_PATTERNS:
        if pattern.search(text):
            return key
    return "custom"


def _clean_url(url):
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def load_companies_from_csv(path=None):
    """Read the companies CSV and return a list of company dicts in the
    same shape hulk_job_search.COMPANIES has always used. Returns an
    empty list (never raises) if the file is missing or empty, so callers
    can cleanly fall back to a built-in list."""
    path = path or CSV_PATH
    if not os.path.exists(path):
        return []

    seen = set()
    companies = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Tolerate slightly different header spellings.
        fieldmap = {(name or "").strip().lower(): name for name in (reader.fieldnames or [])}

        def col(row, *candidates):
            for c in candidates:
                key = fieldmap.get(c)
                if key is not None:
                    return (row.get(key) or "").strip()
            return ""

        for row in reader:
            name = col(row, "company_name", "company", "name")
            careers_url = _clean_url(col(row, "careers_url", "career_website", "career website", "url"))
            raw_hint = col(row, "platform_hint", "application platform", "platform")
            country = col(row, "country")

            if not name or not careers_url:
                continue

            key = (name.lower(), careers_url.lower())
            if key in seen:
                continue
            seen.add(key)

            companies.append({
                "name": name,
                "platform": normalize_platform(raw_hint),
                "token": None,
                "tenant": None,
                "wd_host": None,
                "site": None,
                "confirmed": False,
                "careers_url": careers_url,
                "country": country or None,
                "platform_hint_raw": raw_hint or None,
            })

    companies.sort(key=lambda c: c["name"].lower())
    return companies
