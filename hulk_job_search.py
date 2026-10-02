"""
Hulk - Job Search v6: fresher-tuned (0-1 YOE), Greenhouse + Lever +
SmartRecruiters + Workday + Ashby + custom career-site fallback, for
your "Top 30 Target Companies India 2026" list.
python hulk_job_search_v6.py --scan-all
WHAT CHANGED FROM v5
---------------------
1. ROOT-CAUSE FIX - date filtering was silently killing almost every
   result. Greenhouse's `updated_at` field means "last edited", not
   "posted". Rolling/evergreen fresher postings (which is most of what
   these companies run) don't get touched daily, so a strict "posted
   within N days" filter was discarding live, open roles. Date
   filtering is now ADVISORY by default: nothing is dropped for being
   old or for having no date at all - results are sorted freshest-known
   first, and each row is tagged "recent (<=N days)", "older", or
   "date unknown" so you can see the situation instead of losing rows
   silently. Pass --strict-days to restore the old hard-cutoff v5
   behavior if you ever want it.

2. WORKDAY FIXED for the two companies that were failing:
     - PayPal (India): tenant=paypal, host=wd1, site=jobs (confirmed
       live against paypal.wd1.myworkdayjobs.com/jobs)
     - Salesforce (India): tenant=salesforce, host=wd12,
       site=External_Career_Site (confirmed live against
       salesforce.wd12.myworkdayjobs.com/External_Career_Site)

3. CONFIG CORRECTED using what your own v5 run already discovered:
     - MongoDB (India) -> actually Greenhouse (token "mongodb"), not
       Workday. No more guessing needed for this one.
     - ServiceNow (India) -> actually SmartRecruiters (token
       "servicenow"), not Workday.

4. ASHBY ADDED as a 4th REST platform (alongside Greenhouse/Lever/
   SmartRecruiters) in both the confirmed-hint path and the
   auto-detect fallback. Ashby has a clean public, unauthenticated
   JSON API (api.ashbyhq.com/posting-api/job-board/{name}) and several
   Indian SaaS companies on this list plausibly sit on it now instead
   of Greenhouse - previously this script had literally no way to see
   those jobs.

5. Everything else (role keyword matching, experience filter, custom
   career-site scraper, CSV output) is unchanged from v5.

Setup:
  pip install requests beautifulsoup4

Run:
  python hulk_job_search_v6.py --scan-all
  python hulk_job_search_v6.py --scan-all --days 5 --max-experience 1
  python hulk_job_search_v6.py --scan-all --strict-days
  python hulk_job_search_v6.py --guess-workday "Company Name"
"""

import re
import csv
import time
import argparse
import threading
import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except ImportError:
    HAVE_BS4 = False

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
REQUEST_TIMEOUT = 10
MAX_PLATFORM_JOBS = 500  # Prevent one unusually large board from dominating a scan.

# ---------------------------------------------------------------------
# Probe-failure tracking.
#
# BUG THIS FIXES: every try_* probe used to return (None, None) for BOTH
# "this company is definitely not on this platform" (a clean 404) AND
# "I couldn't tell" (timeout, 403 bot-block, 429 rate limit, 5xx, an HTML
# challenge page served with a 200, DNS/network down). resolve_company_
# platform() then treated "found nothing" as "this is a custom site" and
# wrote that to platform_cache.json PERMANENTLY. One bad moment on the
# network therefore demoted a real Greenhouse/Lever/Workday company to the
# HTML scraper forever - which finds nothing on JS-rendered career pages,
# so Job Search and Job Watcher both returned zero results while the jobs
# were plainly visible in a browser.
#
# Now the probes record every *inconclusive* outcome here (thread-local:
# each search / watcher worker thread runs its own probes sequentially),
# and callers only treat a miss as a real "not found" when nothing
# inconclusive happened along the way.
# ---------------------------------------------------------------------
_CLEAN_MISS_STATUSES = {400, 404, 410, 422}  # "no such board" - a real, cacheable answer
_probe_state = threading.local()


def _reset_probe_errors():
    _probe_state.inconclusive = 0


def _note_inconclusive():
    _probe_state.inconclusive = getattr(_probe_state, "inconclusive", 0) + 1


def _probe_was_inconclusive():
    return getattr(_probe_state, "inconclusive", 0) > 0


def _status_ok(resp):
    """True for HTTP 200. Any other status that is not a clean 'no such
    board' answer (403 bot-block, 429, 5xx, ...) is recorded as inconclusive."""
    if resp.status_code == 200:
        return True
    if resp.status_code not in _CLEAN_MISS_STATUSES:
        _note_inconclusive()
    return False


# =====================================================================
# 1. COMPANIES - corrected config
# =====================================================================

# This 30-company list is the original hand-curated set. It's kept as a
# fallback only - if companies.csv (below) is present, COMPANIES is built
# from that instead, since it covers 300+ companies with confirmed
# careers URLs. See companies_source.py / platform_cache.py.
_BUILTIN_COMPANIES = [
    {"name": "Razorpay", "platform": "greenhouse", "token": "razorpaysoftwareprivatelimited",
     "confirmed": True, "careers_url": "https://razorpay.com/jobs/jobs-all/"},

    {"name": "Cashfree Payments", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.cashfree.com/careers/"},

    {"name": "Juspay", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://juspay.io/careers"},

    {"name": "PayU India", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://corporate.payu.in/careers"},

    {"name": "CRED", "platform": "lever", "token": "cred",
     "confirmed": True, "careers_url": "https://careers.cred.club/"},

    {"name": "slice", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://slice.bank.in/careers"},

    {"name": "PhonePe", "platform": "greenhouse", "token": "phonepe",
     "confirmed": True, "careers_url": "https://www.phonepe.com/careers/"},

    {"name": "Groww", "platform": "greenhouse", "token": "groww", "gh_region": "eu",
     "confirmed": True, "careers_url": "https://groww.in/careers"},

    {"name": "Zoho", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.zoho.com/careers/"},

    {"name": "Freshworks", "platform": "smartrecruiters", "token": "freshworks",
     "confirmed": True, "careers_url": "https://www.freshworks.com/company/careers/"},

    {"name": "Postman", "platform": "greenhouse", "token": "postman",
     "confirmed": True, "careers_url": "https://www.postman.com/company/careers/"},

    {"name": "Chargebee", "platform": "greenhouse", "token": "chargebee",
     "confirmed": False, "careers_url": "https://www.chargebee.com/careers/",
     "ashby_token": "chargebee"},

    {"name": "BrowserStack", "platform": "workday", "tenant": "browserstack",
     "wd_host": "wd3", "site": "External", "confirmed": True,
     "careers_url": "https://www.browserstack.com/careers"},

    {"name": "CleverTap", "platform": "greenhouse", "token": "clevertap",
     "confirmed": False, "careers_url": "https://clevertap.com/careers/",
     "ashby_token": "clevertap"},

    {"name": "Innovaccer", "platform": "greenhouse", "token": "innovaccer",
     "confirmed": False, "careers_url": "https://innovaccer.com/careers",
     "ashby_token": "innovaccer"},

    {"name": "Whatfix", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://whatfix.com/careers/"},

    {"name": "Darwinbox", "platform": "greenhouse", "token": "darwinbox",
     "confirmed": False, "careers_url": "https://darwinbox.com/en-us/careers",
     "ashby_token": "darwinbox"},

    {"name": "PayPal (India)", "platform": "workday", "tenant": "paypal",
     "wd_host": "wd1", "site": "jobs", "confirmed": True,
     "careers_url": "https://careers.pypl.com/home/"},

    {"name": "Cloudflare (India)", "platform": "greenhouse", "token": "cloudflare",
     "confirmed": True, "careers_url": "https://www.cloudflare.com/careers/"},

    {"name": "Flipkart", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.flipkartcareers.com/"},

    {"name": "Myntra", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://careers.myntra.com/"},

    {"name": "Nykaa", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://careers.nykaa.com/"},

    {"name": "Meesho", "platform": "lever", "token": "meesho",
     "confirmed": True, "careers_url": "https://careers.meesho.com/"},

    {"name": "Zepto", "platform": "lever", "token": "zepto",
     "confirmed": False, "careers_url": "https://www.zepto.com/s/careers",
     "ashby_token": "zepto"},

    {"name": "Udaan", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://careers.udaan.com/"},

    {"name": "Urban Company", "platform": "lever", "token": "urbancompany",
     "confirmed": False, "careers_url": "https://www.urbancompany.com/careers",
     "ashby_token": "urbancompany"},

    {"name": "InMobi", "platform": "greenhouse", "token": "inmobi",
     "confirmed": True, "careers_url": "https://www.inmobi.com/company/careers"},

    {"name": "MongoDB (India)", "platform": "greenhouse", "token": "mongodb",
     "confirmed": True,
     "careers_url": "https://www.mongodb.com/company/careers/students-and-graduates"},

    {"name": "ServiceNow (India)", "platform": "smartrecruiters", "token": "servicenow",
     "confirmed": True,
     "careers_url": "https://careers.servicenow.com/locations/apj/india/"},

    {"name": "Salesforce (India)", "platform": "workday", "tenant": "salesforce",
     "wd_host": "wd12", "site": "External_Career_Site", "confirmed": True,
     "careers_url": "https://www.salesforce.com/company/careers/locations/india/"},

    # ---- Extended list (added on request) ----
    {"name": "Zomato", "platform": "custom", "token": None,
     "confirmed": True, "careers_url": "https://www.zomato.com/careers/all"},

    {"name": "Swiggy", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://careers.swiggy.com/"},

    {"name": "Paytm", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://careers.paytm.com/"},

    {"name": "Ola", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.olacabs.com/careers"},

    {"name": "Dream11", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://about.dream11.com/careers/"},

    {"name": "PolicyBazaar", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.policybazaar.com/careers/"},

    {"name": "upGrad", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.upgrad.com/careers/"},

    {"name": "Unacademy", "platform": "greenhouse", "token": "unacademy",
     "confirmed": False, "careers_url": "https://unacademy.com/careers"},

    {"name": "Zerodha", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://zerodha.com/careers/"},

    {"name": "Rapido", "platform": "lever", "token": "rapido",
     "confirmed": False, "careers_url": "https://rapido.bike/careers"},

    {"name": "Delhivery", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.delhivery.com/careers/"},

    {"name": "Licious", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.licious.in/careers"},

    {"name": "Blinkit", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.blinkit.com/careers"},

    {"name": "Practo", "platform": "greenhouse", "token": "practo",
     "confirmed": False, "careers_url": "https://www.practo.com/company/careers"},

    {"name": "Games24x7", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://games24x7.com/careers/"},

    {"name": "Atlassian", "platform": "greenhouse", "token": "atlassian",
     "confirmed": False, "careers_url": "https://www.atlassian.com/company/careers"},

    {"name": "Stripe", "platform": "greenhouse", "token": "stripe",
     "confirmed": False, "careers_url": "https://stripe.com/jobs"},

    {"name": "Notion", "platform": "greenhouse", "token": "notion",
     "confirmed": False, "careers_url": "https://www.notion.so/careers"},

    {"name": "Figma", "platform": "greenhouse", "token": "figma",
     "confirmed": False, "careers_url": "https://www.figma.com/careers/"},

    {"name": "Airbnb", "platform": "greenhouse", "token": "airbnb",
     "confirmed": False, "careers_url": "https://careers.airbnb.com/"},

    {"name": "Uber", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.uber.com/careers/"},

    {"name": "Netflix", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://explore.jobs.netflix.net/careers"},

    {"name": "Shopify", "platform": "greenhouse", "token": "shopify",
     "confirmed": False, "careers_url": "https://www.shopify.com/careers"},

    {"name": "Coinbase", "platform": "greenhouse", "token": "coinbase",
     "confirmed": False, "careers_url": "https://www.coinbase.com/careers"},

    {"name": "Databricks", "platform": "greenhouse", "token": "databricks",
     "confirmed": False, "careers_url": "https://www.databricks.com/company/careers"},

    {"name": "Canva", "platform": "custom", "token": None,
     "confirmed": False, "careers_url": "https://www.lifeatcanva.com/en/jobs/"},
]


# =====================================================================
# 1a. Load the real company list from companies.csv, merging in any
# platform credentials (token/tenant/wd_host/site) already discovered
# and cached from a previous run. Falls back to the 30-company built-in
# list if companies.csv isn't present (e.g. a fresh checkout without it).
# =====================================================================

from companies_source import load_companies_from_csv, normalize_platform  # noqa: E402
from platform_cache import (  # noqa: E402
    get_cached_platform, NEGATIVE_TTL_SECONDS, negative_entry_timestamp,
)


def _merge_cached_credentials(companies):
    for c in companies:
        cached = get_cached_platform(c["name"])
        if not cached:
            continue
        if cached.get("unresolved"):
            # We already ran the full discovery chain once (URL sniffing,
            # a direct slug guess against every ATS with a public JSON
            # API, and - only when the hint was Workday - the brute-force
            # tenant guess) and found nothing. Treat it as custom from now
            # on so repeat searches/scans skip straight to the direct
            # career-page scraper instead of re-running that whole chain
            # every time.
            #
            # Bug fixed here: this used to only demote to "custom" when
            # the CSV's *current* platform hint was still "workday". Any
            # cache row written with a different/older hint (or a bad
            # write) was silently ignored, which could leave a company
            # stuck on a token-less "confirmed" platform that never
            # resolves and never gets scraped as a fallback either. Now
            # any "unresolved" cache row always wins and always demotes
            # to custom, regardless of what platform it or the CSV names.
            c["_workday_guess_failed"] = True
            c["platform"] = "custom"
            c["_custom_confirmed"] = True
            c["_negative_at"] = negative_entry_timestamp(cached)
            continue
        c["platform"] = cached.get("platform", c["platform"])
        c["token"] = cached.get("token", c.get("token"))
        c["tenant"] = cached.get("tenant", c.get("tenant"))
        c["wd_host"] = cached.get("wd_host", c.get("wd_host"))
        c["site"] = cached.get("site", c.get("site"))
        c["gh_region"] = cached.get("gh_region", c.get("gh_region"))
        c["confirmed"] = True
        if cached.get("platform") == "custom":
            c["_custom_confirmed"] = True
            c["_negative_at"] = negative_entry_timestamp(cached)
    return companies


def load_companies():
    csv_companies = load_companies_from_csv()
    if csv_companies:
        return _merge_cached_credentials(csv_companies)
    return _BUILTIN_COMPANIES


COMPANIES = load_companies()


# =====================================================================
# 1b. Role suggestions - shown as a datalist in the UI so people can see
# common searchable role titles without having to guess phrasing.
# =====================================================================

ROLE_SUGGESTIONS = [
    "Backend Developer",
    "Frontend Developer",
    "Full Stack Developer",
    "Software Development Engineer",
    "Mobile Developer (Android)",
    "Mobile Developer (iOS)",
    "DevOps Engineer",
    "Site Reliability Engineer",
    "Data Scientist",
    "Data Analyst",
    "Data Engineer",
    "Machine Learning Engineer",
    "QA Engineer",
    "Product Manager",
    "Project Manager",
    "Business Analyst",
    "UI/UX Designer",
    "Product Designer",
    "Technical Writer",
    "Customer Success Manager",
    "Sales Executive",
    "Marketing Manager",
    "Content Writer",
    "HR Executive",
    "Recruiter",
    "Finance Analyst",
    "Operations Manager",
    "Security Engineer",
    "Solutions Architect",
    "Engineering Manager",
]


# =====================================================================
# 1c. Company "identity" helpers - careers link + a best-guess logo, so
# the UI can show who you're looking at even before/instead of a match.
# =====================================================================

def company_domain(company):
    """Best-guess root domain for a company, used for a logo lookup.
    Strips common subdomain prefixes (careers., jobs., corporate., www.)
    so 'https://careers.cred.club/' -> 'cred.club', not 'careers.cred.club'."""
    url = company.get("careers_url") or ""
    m = re.search(r"https?://([^/]+)", url)
    if not m:
        return None
    host = m.group(1)
    for prefix in ("careers.", "jobs.", "corporate.", "about.", "explore.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host or None


def company_identity(company):
    return {
        "name": company.get("name"),
        "careers_url": company.get("careers_url"),
        "logo_domain": company_domain(company),
    }


# =====================================================================
# 2. ROLE MATCHING (unchanged from v5)
# =====================================================================

ROLE_KEYWORD_PATTERNS = {
    "frontend": r"front[\s-]?end",
    "backend": r"back[\s-]?end",
    "fullstack": r"full[\s-]?stack",
    "software_developer": r"software\s+develop(er|ment)",
    "software_engineer": r"software\s+engineer",
    "sde": r"\bsde\b",
    "devops": r"dev\s?ops",
    "graduate_trainee": r"graduate\s+trainee|graduate\s+engineer\s+trainee|\bget\b",
    "intern": r"\bintern(ship)?\b",
    "associate_software": r"associate\s+software",
    "trainee_engineer": r"trainee\s+engineer",
    "forward": r"for[\s-]?ward",
}
ROLE_PATTERN = re.compile("|".join(ROLE_KEYWORD_PATTERNS.values()), re.IGNORECASE)

EXCLUDE_KEYWORDS = [
    "senior", "sr.", "sr ", "staff", "principal", "lead", "manager",
    "architect", "director", "head of", "vp ", "vice president",
    "iii", " iv", "l4", "l5", "l6",
]
EXCLUDE_PATTERN = re.compile("|".join(re.escape(k) for k in EXCLUDE_KEYWORDS), re.IGNORECASE)


def matched_role_keyword(title):
    for label, pattern in ROLE_KEYWORD_PATTERNS.items():
        if re.search(pattern, title, re.IGNORECASE):
            return label
    return None


def title_is_target_role(title, include_senior=False):
    if not include_senior and EXCLUDE_PATTERN.search(title):
        return False
    return matched_role_keyword(title) is not None


# =====================================================================
# 3. Experience filtering (unchanged from v5)
# =====================================================================

def extract_experience_years(text):
    """
    Find a stated MINIMUM years-of-experience requirement in a job
    description. Deliberately conservative: a bare "X years" mention
    is NOT enough on its own (company copy is full of unrelated ones -
    "founded 8 years ago", "grown 10x in 2 years", "8 years in
    business" - none of which are experience requirements). We only
    treat a number as a requirement when the word "experience" (or
    "exp"/"yoe") appears close to it, so real requirements like
    "3-5 years of experience" or "minimum 2 years experience" are
    still caught, but unrelated company-history numbers are not.
    """
    if not text:
        return None
    text = re.sub("<[^<]+?>", " ", text)

    years = []
    for m in re.finditer(r"(\d{1,2})\s*\+?\s*(?:-\s*(\d{1,2})\s*)?\s*years?", text, re.IGNORECASE):
        window = text[max(0, m.start() - 40): m.end() + 40]
        if re.search(r"experien|\byoe\b|\bexp\.?\b", window, re.IGNORECASE):
            years.append(int(m.group(1)))
            if m.group(2):
                years.append(int(m.group(2)))
    return min(years) if years else None


def passes_experience_filter(required_years, max_experience):
    if required_years is None:
        return True, "unspecified"
    if required_years <= max_experience:
        return True, f"{required_years}+ yrs stated (within range)"
    return False, f"{required_years}+ yrs stated (exceeds range)"


# =====================================================================
# 4. Date handling - ADVISORY, not a hard filter, unless --strict-days
# =====================================================================

def date_bucket(dt, days):
    """Returns (bucket_label, sort_key). Lower sort_key = shown first."""
    if dt is None:
        return "date unknown", 2
    age_days = (datetime.now(timezone.utc) - dt).days
    if age_days <= days:
        return f"recent (<= {days}d)", 0
    return f"older ({age_days}d ago)", 1


def relative_days_from_workday_string(posted_str):
    """Workday gives text like 'Posted 3 Days Ago' / 'Posted Today'."""
    if not posted_str:
        return None
    if "today" in posted_str.lower():
        return 0
    m = re.search(r"(\d+)\s*Day", posted_str)
    return int(m.group(1)) if m else None


# =====================================================================
# 5. Slug guessing
# =====================================================================

def slug_variants(company_name):
    base = re.sub(r"\s*\(.*?\)\s*", "", company_name).strip().lower()
    base = re.sub(r"[^a-z0-9 ]", "", base)
    return list(dict.fromkeys([
        base.replace(" ", ""),
        base.replace(" ", "-"),
        base.replace(" ", "_"),
    ]))


# =====================================================================
# 6. Per-platform raw fetchers (return RAW jobs, unfiltered)
# =====================================================================

def _json_payload(response):
    """Return a decoded JSON payload, or None for an invalid response.

    Career sites sometimes return an HTML challenge page with a 200 status
    instead of their advertised JSON response. Treat that as a failed adapter
    call so the normal ATS/custom fallback chain can continue.
    """
    try:
        return response.json()
    except (ValueError, TypeError):
        return None

def try_greenhouse(token, region=None):
    host = "boards-api.eu.greenhouse.io" if region == "eu" else "boards-api.greenhouse.io"
    url = f"https://{host}/v1/boards/{token}/jobs?content=true"
    try:
        resp = _bounded_request(requests.get, url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if _status_ok(resp):
            payload = _json_payload(resp)
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if isinstance(jobs, list):
                return jobs, ("greenhouse" if region is None else "greenhouse-eu")
            _note_inconclusive()  # 200 but not the JSON we expect (e.g. bot-challenge page)
    except requests.RequestException:
        _note_inconclusive()
    return None, None


def try_lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        resp = _bounded_request(requests.get, url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if _status_ok(resp):
            data = _json_payload(resp)
            if isinstance(data, list):
                return data, "lever"
            _note_inconclusive()
    except requests.RequestException:
        _note_inconclusive()
    return None, None


def try_smartrecruiters(token):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    postings = []
    offset = 0
    # The API defaults to a small page. Searching only that first page caused
    # legitimate matches on larger boards to be silently missed.
    try:
        while len(postings) < MAX_PLATFORM_JOBS:
            limit = min(100, MAX_PLATFORM_JOBS - len(postings))
            resp = _bounded_request(
                requests.get, url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                params={"limit": limit, "offset": offset},
            )
            if not _status_ok(resp):
                return (None, None) if not postings else (postings, "smartrecruiters")
            payload = _json_payload(resp)
            page = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(page, list):
                _note_inconclusive()
                return None, None
            postings.extend(page)
            if len(page) < limit:
                break
            offset += len(page)
        return postings, "smartrecruiters"
    except requests.RequestException:
        if postings:
            return postings, "smartrecruiters"
        _note_inconclusive()
    return None, None


def try_ashby(job_board_name):
    """Ashby's public, unauthenticated Job Postings API."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{job_board_name}"
    try:
        resp = _bounded_request(requests.get, url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if _status_ok(resp):
            payload = _json_payload(resp)
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if isinstance(jobs, list):
                return jobs, "ashby"
            _note_inconclusive()
    except requests.RequestException:
        _note_inconclusive()
    return None, None


import concurrent.futures

# requests' own `timeout` parameter only bounds the gap between individual
# bytes received - it does NOT cap total wall-clock time. A server that
# trickles data slowly (very common with corporate bot-detection systems -
# exactly the kind of site big companies like Amazon/Apple put in front of
# their careers pages) can make a single request hang for minutes even
# with timeout=10. This wraps calls in a real wall-clock deadline instead.
_FETCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="fetch")


def _bounded_request(method, url, max_wall_seconds=12, **kwargs):
    future = _FETCH_EXECUTOR.submit(method, url, **kwargs)
    try:
        return future.result(timeout=max_wall_seconds)
    except concurrent.futures.TimeoutError:
        # Can't forcibly kill the underlying thread/socket, but we stop
        # waiting on it here and move on - the abandoned thread will die
        # on its own once the OS-level socket eventually gives up.
        raise requests.exceptions.Timeout(
            f"exceeded {max_wall_seconds}s wall-clock limit fetching {url}"
        )


def try_workday(tenant, wd_host, site):
    if not (tenant and wd_host and site):
        return None, None
    url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    postings = []
    offset = 0
    try:
        while len(postings) < MAX_PLATFORM_JOBS:
            limit = min(100, MAX_PLATFORM_JOBS - len(postings))
            resp = _bounded_request(
                requests.post, url,
                headers={**HEADERS, "Content-Type": "application/json"},
                json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
                timeout=REQUEST_TIMEOUT,
            )
            if not _status_ok(resp):
                return (None, None) if not postings else (postings, "workday")
            payload = _json_payload(resp)
            page = payload.get("jobPostings") if isinstance(payload, dict) else None
            if not isinstance(page, list):
                _note_inconclusive()
                return None, None
            postings.extend(page)
            if len(page) < limit:
                break
            offset += len(page)
        return postings, "workday"
    except requests.RequestException as e:
        if postings:
            return postings, "workday"
        # A wrong tenant/host guess fails DNS resolution (ConnectionError) -
        # that is a normal "not here" answer while guessing. Timeouts are not.
        is_timeout = isinstance(e, requests.exceptions.Timeout)
        if is_timeout or not isinstance(e, requests.exceptions.ConnectionError):
            _note_inconclusive()
    return None, None


WORKDAY_HOSTS = ["wd1", "wd2", "wd3", "wd5", "wd12"]
WORKDAY_SITE_GUESSES = [
    "External", "Careers", "ExternalCareerSite", "External_Career_Site",
    "Global_Careers", "Career", "GlobalCareers", "jobs",
]


def guess_workday(company_name, tenant_hint=None, verbose=True, max_seconds=45):
    """Brute-force tenant/host/site combos against the real Workday API.
    Bounded by max_seconds total (not just per-request) so a company that
    genuinely isn't on Workday can't eat many minutes trying every
    combination one at a time."""
    tenants = [tenant_hint] if tenant_hint else slug_variants(company_name)
    tried = 0
    start = time.monotonic()
    for tenant in tenants:
        for host in WORKDAY_HOSTS:
            for site in WORKDAY_SITE_GUESSES:
                if time.monotonic() - start > max_seconds:
                    _note_inconclusive()  # ran out of time before trying every combo
                    if verbose:
                        print(f"  Gave up after {tried} attempts ({max_seconds}s budget used).")
                    return None
                tried += 1
                jobs, platform = try_workday(tenant, host, site)
                if jobs:
                    if verbose:
                        print(f"  MATCH after {tried} attempts: "
                              f"tenant={tenant} host={host} site={site} ({len(jobs)} jobs)")
                    return tenant, host, site, jobs
    if verbose:
        print(f"  No match after {tried} attempts. Open their careers page, click a job, "
              f"and read the URL: <tenant>.<wdN>.myworkdayjobs.com/<site>/job/... "
              f"then paste tenant/host/site into COMPANIES by hand.")
    return None


# =====================================================================
# 5b. Discovery from a careers URL - for the CSV-driven company list,
# where we only have a careers page link and a platform *hint*, not the
# actual API token/tenant. Most "own portal" and branded careers pages
# (careers.company.com, company.com/careers) either redirect straight to
# the underlying ATS, or embed a link/iframe to it somewhere in the page
# HTML - so one GET request is usually enough to lift the token/tenant
# straight out of the URL, without brute-force guessing.
#
# This is intentionally cheap (1 request, short timeout) and read-only -
# it never posts anything or submits forms, it just looks at where the
# page and its links point.
# =====================================================================

_WORKDAY_URL_RE = re.compile(
    r"([a-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com"
    r"(?:/wday/cxs/[a-z0-9_-]+)?"
    r"(?:/[a-z]{2}(?:-[a-z]{2,4})?(?=/|$))?"   # optional locale like /en-US
    r"(?:/([A-Za-z0-9_-]+))?",                  # site is now optional
    re.IGNORECASE,

)
_GREENHOUSE_URL_RE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.IGNORECASE
)
_LEVER_URL_RE = re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.IGNORECASE)
_SMARTRECRUITERS_URL_RE = re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)")
_WORKDAY_COMMON_SITES = [
    "external_experienced", "external_university", "External", "external",
    "Careers", "careers", "ExternalCareerSite", "External_Career_Site",
    "Global_Careers", "GlobalCareers", "Career", "jobs",
]
_WORKDAY_NOT_SITES = {"wday", "assets", "static", "favicon.ico", "en", "us"}


def _workday_from_careers_url(url):
    """Pull (tenant, host, site-or-None) straight from a *.myworkdayjobs.com
    URL with no network call."""
    m = _WORKDAY_URL_RE.search(url or "")
    if not m:
        return None
    site = m.group(3)
    if site and site.lower() in _WORKDAY_NOT_SITES:
        site = None
    return m.group(1), m.group(2).lower(), site


def _find_workday_site(tenant, wd_host, max_seconds=30):
    """Tenant+host are known but the site name isn't. Look at where the root
    URL redirects first, then try a short list of likely names on THIS
    tenant+host only. Returns a working site name or None."""
    start = time.monotonic()
    tried = set()

    def works(site):
        if site in tried:
            return False
        tried.add(site)
        jobs, _ = try_workday(tenant, wd_host, site)
        return jobs is not None  # [] is still a valid board

    root = f"https://{tenant}.{wd_host}.myworkdayjobs.com/"
    try:
        r = _bounded_request(requests.get, root, headers=HEADERS,
                             timeout=REQUEST_TIMEOUT, allow_redirects=True)
        haystack = r.url + "\n" + (r.text or "")[:200000]
        for m in _WORKDAY_URL_RE.finditer(haystack):
            site = m.group(3)
            if (m.group(1).lower() == tenant.lower() and m.group(2).lower() == wd_host
                    and site and site.lower() not in _WORKDAY_NOT_SITES and len(tried) < 5):
                if works(site):
                    return site
    except requests.RequestException:
        _note_inconclusive()

    t = tenant
    guesses = _WORKDAY_COMMON_SITES + [t, t.capitalize(), f"{t}_External",
                                       f"{t}External", f"{t}_Careers", f"{t}Careers"]
    for site in dict.fromkeys(guesses):
        if time.monotonic() - start > max_seconds:
            _note_inconclusive()
            return None
        if works(site):
            return site
    return None


def discover_platform_from_url(url):
    """Fetch a company's careers URL once and try to lift API
    credentials (Greenhouse token, Lever token, Workday tenant/host/site,
    etc.) straight out of the final redirected URL and the page HTML.

    Returns a dict like {"platform": "greenhouse", "token": "acme"} or
    {"platform": "workday", "tenant": "acme", "wd_host": "wd1", "site": "Careers"}
    on success, or None if nothing recognizable was found (e.g. the page
    is JS-rendered and the real board link isn't in the raw HTML, or the
    site is unreachable). Never raises.
    """
    if not url:
        return None
    try:
        resp = _bounded_request(
            requests.get, url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
        )
    except requests.RequestException:
        _note_inconclusive()
        return None
    if resp.status_code not in _CLEAN_MISS_STATUSES and resp.status_code >= 400:
        _note_inconclusive()  # 403 / 429 / 5xx: we never saw the real page

    haystack = resp.url + "\n" + (resp.text if resp.text else "")

    parts = _workday_from_careers_url(haystack)
    if parts:
        tenant, wd_host, site = parts
        if site:
            jobs, _ = try_workday(tenant, wd_host, site)
            if jobs is None:
                site = None
        if not site:
            site = _find_workday_site(tenant, wd_host)
        if site:
            return {"platform": "workday", "tenant": tenant, "wd_host": wd_host, "site": site}

    m = _GREENHOUSE_URL_RE.search(haystack)
    if m:
        token = m.group(1)
        jobs, _ = try_greenhouse(token)
        if jobs is not None:
            return {"platform": "greenhouse", "token": token}

    m = _LEVER_URL_RE.search(haystack)
    if m:
        token = m.group(1)
        jobs, _ = try_lever(token)
        if jobs is not None:
            return {"platform": "lever", "token": token}

    m = _SMARTRECRUITERS_URL_RE.search(haystack)
    if m:
        token = m.group(1)
        jobs, _ = try_smartrecruiters(token)
        if jobs is not None:
            return {"platform": "smartrecruiters", "token": token}

    m = _ASHBY_URL_RE.search(haystack)
    if m:
        token = m.group(1)
        jobs, _ = try_ashby(token)
        if jobs is not None:
            return {"platform": "ashby", "token": token}

    return None


def _guess_platform_via_public_api(company):
    """Try the company's own name as a board slug directly against every
    ATS that has a public, unauthenticated JSON API (Greenhouse, Lever,
    SmartRecruiters, Ashby) - the same trick search_unknown_company()
    already uses for companies we've never heard of at all.

    This exists because discover_platform_from_url() can only see what's
    in the raw HTML of the careers page. Two very common real-world cases
    slip past it entirely:
      - The careers page is a JS-rendered SPA (React/Next/etc.) whose
        initial HTML never contains a literal boards.greenhouse.io /
        jobs.lever.co link, even though the company genuinely runs on
        that ATS (Airbnb is exactly this: careers.airbnb.com is a SPA,
        but boards-api.greenhouse.io/v1/boards/airbnb/jobs works fine).
      - The CSV's platform_hint column is simply wrong or stale for that
        row, so even a successful HTML sniff would be checked against
        the wrong host pattern.

    The company's own CSV-hinted platform (if it's one of these four) is
    tried first since it's the most likely to be right; the rest are
    tried after as a safety net. Returns a found-platform dict shaped
    like discover_platform_from_url()'s return value, or None.
    """
    hinted = company.get("platform")
    checks = [
        ("greenhouse", lambda s: try_greenhouse(s, region=None)),
        ("greenhouse-eu", lambda s: try_greenhouse(s, region="eu")),
        ("lever", try_lever),
        ("smartrecruiters", try_smartrecruiters),
        ("ashby", try_ashby),
    ]
    if hinted in ("greenhouse", "lever", "smartrecruiters", "ashby"):
        checks.sort(key=lambda pair: 0 if pair[0].startswith(hinted) else 1)

    for slug in slug_variants(company["name"]):
        for _, fn in checks:
            jobs, used = fn(slug)
            if not jobs:
                continue
            if used == "greenhouse-eu":
                return {"platform": "greenhouse", "token": slug, "gh_region": "eu"}
            if used == "greenhouse":
                return {"platform": "greenhouse", "token": slug}
            return {"platform": used, "token": slug}
    return None


def _negative_expired(company):
    """True when a remembered 'no known ATS' verdict is older than the TTL."""
    ts = company.get("_negative_at")
    return ts is not None and (time.time() - ts) > NEGATIVE_TTL_SECONDS


def _forget_negative(company):
    """Drop an expired 'this is a custom site' verdict and go back to the
    CSV's original platform hint so discovery can run again."""
    company.pop("_custom_confirmed", None)
    company.pop("_workday_guess_failed", None)
    company.pop("_negative_at", None)
    raw_hint = company.get("platform_hint_raw")
    if raw_hint is not None:
        company["platform"] = normalize_platform(raw_hint)


def resolve_company_platform(company, try_workday_guess=False):
    """Make sure `company` has real credentials (token / tenant+host+site)
    for its platform, discovering and caching them if missing. Mutates
    and returns `company`. Safe to call repeatedly.

    CACHING RULES (this is the fix for "search/watcher suddenly returns
    nothing"):
      * A POSITIVE result (we found the real token/tenant) is cached
        permanently.
      * A NEGATIVE result ("no known ATS, treat as custom site") is cached
        ONLY when every probe gave a definitive answer (a clean 404 etc).
        If any probe was inconclusive - timeout, 403 bot-block, 429, 5xx,
        HTML challenge page, network down, Workday guess ran out of time -
        NOTHING is cached, the company keeps its CSV hint, and it is simply
        retried on the next search / scan. company["_lookup_unreliable"] is
        set so the search layer can say "couldn't check" instead of a
        misleading "no match".
      * Even a definitive negative expires after NEGATIVE_TTL_SECONDS
        (default 24h, env PLATFORM_CACHE_NEGATIVE_TTL_HOURS) so a company
        that later moves onto Greenhouse/Lever/etc. is picked up again.

    Set try_workday_guess=True to fall back to the slower brute-force
    guess_workday() when direct discovery fails and the hint says
    Workday - only worth it for a single on-demand search, not a bulk
    scan of hundreds of companies.
    """
    company["_lookup_unreliable"] = False
    platform = company.get("platform")
    has_creds = (
        (platform == "greenhouse" and company.get("token"))
        or (platform == "lever" and company.get("token"))
        or (platform == "smartrecruiters" and company.get("token"))
        or (platform == "ashby" and company.get("token"))
        or (platform == "workday" and company.get("wd_host"))
    )
    if has_creds:
        return company

    negative_verdict = (
        (platform == "custom" and company.get("_custom_confirmed"))
        or (platform == "workday" and company.get("_workday_guess_failed"))
    )
    if negative_verdict:
        if not _negative_expired(company):
            return company
        _forget_negative(company)          # stale verdict - probe again
        platform = company.get("platform")

    _reset_probe_errors()

    # Even when the CSV hint says "custom" (Own Portal / Taleo / unknown),
    # it's cheap to check once whether the page actually embeds a known
    # ATS (common: an in-house careers page with a Greenhouse/Lever widget)
    # - a much better result than the generic HTML scraper if so.
    found = None
    parts = _workday_from_careers_url(company.get("careers_url"))
    if parts:  # the CSV link itself is a Workday URL - no guessing needed
        tenant, wd_host, site = parts
        site = site or _find_workday_site(tenant, wd_host)
        if site:
            found = {"platform": "workday", "tenant": tenant, "wd_host": wd_host, "site": site}
    if found is None:
        found = discover_platform_from_url(company.get("careers_url"))

    # HTML sniffing found nothing - try the cheap direct-slug guess against
    # every ATS with a public JSON API. Handles JS-rendered career pages
    # and wrong/stale CSV hints.
    if found is None and platform != "workday":
        found = _guess_platform_via_public_api(company)

    attempted_full_guess = False
    if found is None and platform == "workday" and try_workday_guess:
        attempted_full_guess = True
        guessed = guess_workday(company["name"], verbose=False)
        if guessed:
            tenant, wd_host, site, _jobs = guessed
            found = {"platform": "workday", "tenant": tenant, "wd_host": wd_host, "site": site}

    from platform_cache import set_cached_platform

    if found:
        company.update(found)
        company["confirmed"] = True
        set_cached_platform(company["name"], found)
        return company

    # ---- nothing found. Was that a real "no", or did we just fail to look? ----
    if _probe_was_inconclusive():
        # Do NOT cache and do NOT mutate the company: leave it exactly as
        # loaded so the next search/scan re-probes from scratch.
        company["_lookup_unreliable"] = True
        return company

    now = time.time()
    if platform == "custom":
        company["_custom_confirmed"] = True
        company["_negative_at"] = now
        set_cached_platform(company["name"], {"platform": "custom"})
    elif platform == "workday" and attempted_full_guess:
        company["_workday_guess_failed"] = True
        company["_negative_at"] = now
        set_cached_platform(company["name"], {"platform": "workday", "unresolved": True})
    elif platform != "workday":
        company["platform"] = "custom"
        company["_custom_confirmed"] = True
        company["_negative_at"] = now
        set_cached_platform(company["name"], {"platform": "custom"})

    return company


# =====================================================================
# 7. Generic fallback scraper for fully custom career sites
# =====================================================================

JOB_LINK_HINTS = re.compile(r"job|career|opening|position|role|vacan", re.IGNORECASE)
JOB_DETAIL_LINK = re.compile(r"/jobs/[^/]+/[^/?#]+/?(?:[?#].*)?$", re.IGNORECASE)


def fetch_custom_site_jobs(url, keyword=None, location=None, max_pages=5):
    """
    Fetch job listings from a company's plain careers page and scrape
    job-like links, then let the caller filter them locally (see
    role_search._build_kept_custom).

    Deliberately does NOT send `keyword`/`location` as query parameters
    to the target site. Most "Own Portal"/custom career pages have no
    such search feature; passing an unrecognized query param can make
    some sites render an empty "no results" state instead of their
    normal default listing - silently hiding jobs that are genuinely
    there when a person opens the same URL in a browser with no query
    string at all. `keyword`/`location` are still accepted here (and
    ignored) purely so any existing caller passing them doesn't break.
    """
    if not HAVE_BS4:
        return []
    try:
        resp = _bounded_request(requests.get, url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status not in _CLEAN_MISS_STATUSES:
            _note_inconclusive()
        return []

    responses = [(resp.text, resp.url or url)]
    # Best-effort pagination of the site's DEFAULT listing (never a
    # search results page - we never send search params, see above).
    # If the site doesn't support ?page=, this just re-fetches the same
    # page (detected below and stopped) or 404s (also stopped) - it can
    # only add results, never remove ones page 1 already found.
    for page in range(2, max_pages + 1):
        try:
            page_resp = _bounded_request(
                requests.get, url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                params={"page": page},
            )
            if not page_resp.ok or page_resp.text == responses[-1][0]:
                break
            responses.append((page_resp.text, page_resp.url or url))
        except requests.RequestException:
            break

    results = []
    seen = set()
    for page_html, page_url in responses:
        soup = BeautifulSoup(page_html, "html.parser")
        for a in soup.find_all("a", href=True):
            # Some accessible career sites put the role name in aria-label
            # or title and leave the visual anchor empty (icon/card-only
            # links).
            text = a.get_text(" ", strip=True) or a.get("aria-label", "") or a.get("title", "")
            href = a["href"].strip()
            if not text or len(text) < 4:
                continue
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            is_job_link = JOB_DETAIL_LINK.search(href) is not None
            href_path = urlsplit(href).path.lower().rstrip("/")
            is_job_index = href_path.endswith("/jobs") or href_path.endswith("/saved-jobs")
            if (is_job_link or JOB_LINK_HINTS.search(href) or JOB_LINK_HINTS.search(text)) and not is_job_index and text.lower().strip() not in {
                "careers", "career", "jobs", "join us", "apply now", "openings",
                "search jobs", "view all jobs", "view all openings", "browse jobs",
                "see all jobs", "see open roles", "open roles", "our teams", "our jobs",
                "explore opportunities", "explore careers", "life at work", "learn more",
                "sign in", "create account", "saved jobs", "job alerts", "all locations",
                "all departments", "all categories", "filter results", "clear filters",
            }:
                full_link = href if href.startswith("http") else requests.compat.urljoin(page_url, href)
                if not full_link.startswith(("https://", "http://")):
                    continue
                if full_link not in seen:
                    seen.add(full_link)
                    results.append({"title": text, "link": full_link})

    # Branded portals often put the technology and role wording in the detail
    # page rather than the short result title. Fetch a bounded number of job
    # details so local matching can use that information too.
    for listing in results[:50]:
        if not JOB_DETAIL_LINK.search(listing["link"]):
            continue
        try:
            detail = _bounded_request(
                requests.get, listing["link"], headers=HEADERS, timeout=REQUEST_TIMEOUT
            )
            if detail.ok:
                detail_soup = BeautifulSoup(detail.text, "html.parser")
                listing["description"] = detail_soup.get_text(" ", strip=True)
        except requests.RequestException:
            continue
    return results


# =====================================================================
# 8. Normalizers - role + experience filters apply; date is advisory
# =====================================================================

def normalize_greenhouse(jobs, days, max_experience, include_senior, strict_days):
    kept = []
    for job in jobs:
        title = job.get("title", "")
        if not title_is_target_role(title, include_senior):
            continue
        updated_at = job.get("updated_at", "")
        dt = None
        if updated_at:
            try:
                dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        bucket, sort_key = date_bucket(dt, days)
        if strict_days and dt is not None and sort_key == 1:
            continue
        content = job.get("content", "")
        req_years = extract_experience_years(content)
        ok, exp_note = passes_experience_filter(req_years, max_experience)
        if not ok:
            continue
        location = (job.get("location") or {}).get("name", "")
        kept.append({
            "title": title, "location": location, "posted": updated_at or "date unknown",
            "date_bucket": bucket, "sort_key": sort_key,
            "experience_note": exp_note, "matched_keyword": matched_role_keyword(title),
            "link": job.get("absolute_url", ""),
        })
    return kept


def normalize_lever(jobs, days, max_experience, include_senior, strict_days):
    kept = []
    for job in jobs:
        title = job.get("text", "")
        if not title_is_target_role(title, include_senior):
            continue
        created_ms = job.get("createdAt")
        dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if created_ms else None
        bucket, sort_key = date_bucket(dt, days)
        if strict_days and dt is not None and sort_key == 1:
            continue
        description = job.get("description", "") + " " + str(job.get("lists", ""))
        req_years = extract_experience_years(description)
        ok, exp_note = passes_experience_filter(req_years, max_experience)
        if not ok:
            continue
        location = (job.get("categories") or {}).get("location", "")
        posted = dt.isoformat() if dt else "date unknown"
        kept.append({
            "title": title, "location": location, "posted": posted,
            "date_bucket": bucket, "sort_key": sort_key,
            "experience_note": exp_note, "matched_keyword": matched_role_keyword(title),
            "link": job.get("hostedUrl", ""),
        })
    return kept


def normalize_smartrecruiters(jobs, days, max_experience, include_senior, strict_days):
    kept = []
    for job in jobs:
        title = job.get("name", "")
        if not title_is_target_role(title, include_senior):
            continue
        released = job.get("releasedDate", "")
        dt = None
        if released:
            try:
                dt = datetime.fromisoformat(released.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        bucket, sort_key = date_bucket(dt, days)
        if strict_days and dt is not None and sort_key == 1:
            continue
        # SmartRecruiters needs a 2nd call for description; experience unspecified.
        ok, exp_note = passes_experience_filter(None, max_experience)
        location = (job.get("location") or {}).get("city", "")
        kept.append({
            "title": title, "location": location, "posted": released or "date unknown",
            "date_bucket": bucket, "sort_key": sort_key,
            "experience_note": exp_note, "matched_keyword": matched_role_keyword(title),
            "link": f"https://jobs.smartrecruiters.com/{job.get('company', {}).get('identifier', '')}/{job.get('id', '')}",
        })
    return kept


def normalize_ashby(jobs, days, max_experience, include_senior, strict_days):
    kept = []
    for job in jobs:
        title = job.get("title", "")
        if not title_is_target_role(title, include_senior):
            continue
        published_at = job.get("publishedAt", "")
        dt = None
        if published_at:
            try:
                dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        bucket, sort_key = date_bucket(dt, days)
        if strict_days and dt is not None and sort_key == 1:
            continue
        description = job.get("descriptionPlain", "") or ""
        req_years = extract_experience_years(description)
        ok, exp_note = passes_experience_filter(req_years, max_experience)
        if not ok:
            continue
        location = job.get("location", "") or job.get("locationName", "")
        kept.append({
            "title": title, "location": location, "posted": published_at or "date unknown",
            "date_bucket": bucket, "sort_key": sort_key,
            "experience_note": exp_note, "matched_keyword": matched_role_keyword(title),
            "link": job.get("jobUrl", "") or job.get("applyUrl", ""),
        })
    return kept


def normalize_workday(jobs, days, max_experience, include_senior, strict_days,
                       tenant, wd_host, site):
    kept = []
    for job in jobs:
        title = job.get("title", "")
        if not title_is_target_role(title, include_senior):
            continue
        posted_str = job.get("postedOn", "")
        age_days = relative_days_from_workday_string(posted_str)
        if age_days is None:
            bucket, sort_key = "date unknown", 2
        else:
            sort_key = 0 if age_days <= days else 1
            bucket = f"recent (<= {days}d)" if sort_key == 0 else f"older ({age_days}d ago)"
        if strict_days and age_days is not None and sort_key == 1:
            continue
        ok, exp_note = passes_experience_filter(None, max_experience)
        location = job.get("locationsText", "") or (job.get("bulletFields") or [""])[0]
        path = job.get("externalPath", "")
        link = f"https://{tenant}.{wd_host}.myworkdayjobs.com/{site}{path}" if path else ""
        kept.append({
            "title": title, "location": location, "posted": posted_str or "date unknown",
            "date_bucket": bucket, "sort_key": sort_key,
            "experience_note": exp_note, "matched_keyword": matched_role_keyword(title),
            "link": link,
        })
    return kept


def normalize_custom(listings, max_experience, include_senior):
    kept = []
    for j in listings:
        title = j["title"]
        if not title_is_target_role(title, include_senior):
            continue
        ok, exp_note = passes_experience_filter(None, max_experience)
        kept.append({
            "title": title, "location": "", "posted": "date unknown (custom site)",
            "date_bucket": "date unknown", "sort_key": 2,
            "experience_note": exp_note, "matched_keyword": matched_role_keyword(title),
            "link": j["link"],
        })
    return kept


# =====================================================================
# 9. Per-company resolver
# =====================================================================

def fetch_company_jobs(company, days, max_experience, include_senior, strict_days):
    name = company["name"]
    resolve_company_platform(company)  # cheap, cached; fills token/tenant if missing
    platform = company.get("platform")

    def try_hint():
        if platform == "greenhouse" and company.get("token"):
            region = "eu" if company.get("gh_region") == "eu" else None
            jobs, used = try_greenhouse(company["token"], region=region)
            if jobs is None:
                other = None if region == "eu" else "eu"
                jobs, used = try_greenhouse(company["token"], region=other)
            if jobs is not None:
                kept = normalize_greenhouse(jobs, days, max_experience, include_senior, strict_days)
                return jobs, kept, used
        if platform == "lever" and company.get("token"):
            jobs, used = try_lever(company["token"])
            if jobs is not None:
                kept = normalize_lever(jobs, days, max_experience, include_senior, strict_days)
                return jobs, kept, used
        if platform == "smartrecruiters" and company.get("token"):
            jobs, used = try_smartrecruiters(company["token"])
            if jobs is not None:
                kept = normalize_smartrecruiters(jobs, days, max_experience, include_senior, strict_days)
                return jobs, kept, used
        if platform == "workday":
            tenant, wd_host, site = company.get("tenant"), company.get("wd_host"), company.get("site")
            jobs, used = try_workday(tenant, wd_host, site)
            if jobs is not None:
                kept = normalize_workday(jobs, days, max_experience, include_senior, strict_days,
                                          tenant, wd_host, site)
                return jobs, kept, used
        if platform == "custom":
            careers_url = company.get("careers_url")
            listings = fetch_custom_site_jobs(careers_url) if careers_url else []
            kept = normalize_custom(listings, max_experience, include_senior)
            return listings, kept, "custom-scrape"
        return None, None, None

    raw, kept, used = try_hint()

    if raw:
        if kept:
            return kept, used, None
        return [], used, (f"{len(raw)} open role(s) found via {used}, but none matched your "
                           f"role/experience filters (date is advisory, not a hard cutoff).")
    if raw == []:
        if platform == "workday" and not company.get("wd_host"):
            return [], None, ("Workday tenant/host/site not confirmed yet. Run: "
                               f'python hulk_job_search_v6.py --guess-workday "{name}"')
        if platform == "custom":
            return [], None, (f"Custom career site, static scrape found nothing (likely "
                               f"JS-rendered) - check by hand: {company.get('careers_url')}")

    # --- Ashby check (uses ashby_token hint if present, else slug guess) ---
    ashby_candidates = [company["ashby_token"]] if company.get("ashby_token") else slug_variants(name)
    for candidate in ashby_candidates:
        jobs, used = try_ashby(candidate)
        if jobs:
            kept = normalize_ashby(jobs, days, max_experience, include_senior, strict_days)
            if kept:
                return kept, used, None
            return [], used, (f"{len(jobs)} open role(s) found via ashby, but none matched "
                               f"your role/experience filters.")

    # --- hint failed outright or nothing useful: full auto-detect ---
    if platform != "custom":
        for slug in slug_variants(name):
            for fn, norm in (
                (lambda s: try_greenhouse(s, region=None), normalize_greenhouse),
                (lambda s: try_greenhouse(s, region="eu"), normalize_greenhouse),
                (try_lever, normalize_lever),
                (try_smartrecruiters, normalize_smartrecruiters),
            ):
                jobs, used = fn(slug)
                if jobs:
                    kept = norm(jobs, days, max_experience, include_senior, strict_days)
                    if kept:
                        return kept, used, None
                    return [], used, (f"{len(jobs)} open role(s) found via auto-detected "
                                       f"{used}, but none matched your filters.")

    return [], None, ("Not reachable on Greenhouse/Lever/SmartRecruiters/Ashby under a guessed "
                       f"slug, and no confirmed Workday info. Check their careers page directly: "
                       f"{company.get('careers_url', '(no URL on file)')}")


# =====================================================================
# 10. Batch scan
# =====================================================================

def scan_all(days, max_experience, include_senior, strict_days, sleep, save_csv=True):
    print(f"Scanning {len(COMPANIES)} companies | roles: frontend/backend/fullstack/"
          f"SDE/software dev-eng/devops/graduate trainee/intern/associate/UI/UX/Product Manager/Project Manager/Forward"
          f"| max experience: {max_experience} yr(s) | recency window: {days} day(s) "
          f"({'strict cutoff' if strict_days else 'advisory - nothing dropped for being old'})\n")

    all_rows = []
    for company in COMPANIES:
        name = company["name"]
        results, platform_used, note = fetch_company_jobs(
            company, days, max_experience, include_senior, strict_days)
        results.sort(key=lambda j: j.get("sort_key", 2))
        tag = platform_used or company.get("platform", "unknown")
        print(f"[{tag:>14}] {name:<22} -> {len(results)} matching job(s)")
        if note:
            print(f"                 note: {note}")
        for job in results:
            all_rows.append({
                "company": name,
                "title": job["title"],
                "location": job.get("location", ""),
                "posted": job.get("posted", ""),
                "recency": job.get("date_bucket", ""),
                "experience_note": job.get("experience_note", ""),
                "matched_keyword": job.get("matched_keyword", ""),
                "link": job.get("link", ""),
                "platform": tag,
            })
        time.sleep(sleep)

    print(f"\nTotal matching jobs across all companies: {len(all_rows)} data row(s) "
          f"(the CSV will also have 1 header row on top of these).")

    if save_csv and all_rows:
        filename = f"hulk_job_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "company", "title", "location", "posted", "recency",
                "experience_note", "matched_keyword", "link", "platform",
            ])
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"Saved: {filename}")

    return all_rows


# =====================================================================
# 11. CLI
# =====================================================================

def interactive_single_company(days, max_experience, include_senior, strict_days):
    print("Hulk Job Search v6 - single company lookup\n")
    name = input("Company name: ").strip()

    company = next((c for c in COMPANIES if c["name"].lower() == name.lower()), None)
    if company is None:
        company = {"name": name, "platform": None, "careers_url": None}

    print(f"\nLooking up {name}...\n")
    results, platform_used, note = fetch_company_jobs(
        company, days, max_experience, include_senior, strict_days)
    results.sort(key=lambda j: j.get("sort_key", 2))

    if not results:
        print(f"No matching openings found via {platform_used or 'auto-detect'}.")
        if note:
            print(note)
        return

    print(f"Found via: {platform_used}\n{len(results)} matching job(s):\n")
    for i, job in enumerate(results, 1):
        print(f"{i}. {job['title']} - {job.get('location', '')}")
        print(f"   Posted: {job.get('posted', '')}  [{job.get('date_bucket', '')}]")
        print(f"   Experience: {job.get('experience_note', '')}")
        print(f"   {job['link']}\n")


def main():
    parser = argparse.ArgumentParser(description="Hulk Job Search v6 (fresher/0-1 YOE tuned)")
    parser.add_argument("--scan-all", action="store_true", help="Scan all 30 target companies")
    parser.add_argument("--days", type=int, default=5,
                         help="Recency window in days for the 'recent' tag/sort (default: 5)")
    parser.add_argument("--strict-days", action="store_true",
                         help="Hard-drop jobs older than --days instead of just tagging/sorting them")
    parser.add_argument("--max-experience", type=float, default=1.0,
                         help="Max years of experience required (default: 1.0)")
    parser.add_argument("--include-senior", action="store_true",
                         help="Don't exclude Senior/Staff/Lead/Principal/Manager titles")
    parser.add_argument("--sleep", type=float, default=0.4,
                         help="Seconds to wait between companies (default: 0.4)")
    parser.add_argument("--guess-workday", metavar="COMPANY_NAME", default=None,
                         help="Bounded grid-search for a company's Workday tenant/host/site")
    args = parser.parse_args()

    if args.guess_workday:
        company = next((c for c in COMPANIES if c["name"].lower() == args.guess_workday.lower()), None)
        tenant_hint = company.get("tenant") if company else None
        print(f"Guessing Workday config for '{args.guess_workday}'...")
        guess_workday(args.guess_workday, tenant_hint=tenant_hint)
        return

    if args.scan_all:
        scan_all(args.days, args.max_experience, args.include_senior, args.strict_days, args.sleep)
        return

    interactive_single_company(args.days, args.max_experience, args.include_senior, args.strict_days)


if __name__ == "__main__":
    main()