"""
role_search.py
================
Wraps hulk_job_search.py's per-platform fetchers with FREE-TEXT role
matching (instead of the fixed frontend/backend/SDE/etc. keyword set),
so a website visitor can type any role - "product manager", "data
analyst", "ui designer" - and company name, and get matches.

Two entry points:
  - search_known_company(company, role_query, max_experience, days, include_senior)
        For companies already configured in hulk_job_search.COMPANIES.
        Uses the confirmed platform/token directly - fast, one request.

  - search_unknown_company(name, role_query, max_experience, days, include_senior)
        For companies NOT in the list. Guesses a slug and tries
        Greenhouse / Lever / SmartRecruiters / Ashby. Does NOT attempt
        Workday guessing (too many combinations for a live web request)
        or JS-rendered custom sites. Returns clearly labeled "best
        effort" results, or an explicit "not found" note.
"""

import re
import concurrent.futures
from datetime import datetime, timezone

import ai_match
from hulk_job_search import (
    COMPANIES,
    HEADERS,
    EXCLUDE_KEYWORDS,
    ROLE_SUGGESTIONS,
    try_greenhouse,
    try_lever,
    try_smartrecruiters,
    try_ashby,
    try_workday,
    slug_variants,
    extract_experience_years,
    passes_experience_filter,
    date_bucket,
    relative_days_from_workday_string,
    company_identity,
    company_domain,
    resolve_company_platform,
    fetch_custom_site_jobs,
    _reset_probe_errors,
    _probe_was_inconclusive,
)


class LookupFailedNote(str):
    """A note string meaning "we could NOT check this company" (timeout,
    bot-block, rate limit, network down) - as opposed to a genuine
    "checked, nothing matched". It is still a plain str for JSON/UI
    purposes; callers use isinstance(note, LookupFailedNote) to tell the
    two apart, so a failed lookup is never reported as "NO MATCH"."""


def is_lookup_failure(note):
    return isinstance(note, LookupFailedNote)


def _lookup_failed_note(company):
    url = company.get("careers_url") or "their careers page"
    return LookupFailedNote(
        f"Couldn't reach {company['name']}'s job board just now (timeout, the site "
        f"blocked the request, or a network error). This is NOT a confirmed \"no match\" - "
        f"nothing was cached, so it will be re-checked automatically next time. "
        f"You can check directly: {url}"
    )


def _finalize(result, company, api_failed):
    """If a search came back empty AND something along the way was
    inconclusive, replace the misleading 'no match / can't extract' note
    with an explicit lookup-failed one. Real matches are never touched."""
    kept, used, total, note = result
    if not kept and (api_failed or _probe_was_inconclusive()):
        return [], used, total, _lookup_failed_note(company)
    return result


# ---------------------------------------------------------------------
# Free-text role matching
# ---------------------------------------------------------------------

# Common compound-word variants ("front end" / "front-end" / "frontend")
# normalized to one canonical form before matching, applied to BOTH the
# user's query and the job title, so word order/spacing/hyphens never
# cause a false "no match". Order matters: phrase-level compounds run
# first (so "software development engineer" collapses before the
# generic developer/engineer synonym rule below would otherwise leave
# "development" stranded as its own required token), and "ui/ux" is
# collapsed to two SEPARATE words ("ui ux"), not one merged word - a
# merged "uiux" token would stop a query for just "ui" or just "ux"
# from ever matching via \b at all, which was itself a silent bug.
_COMPOUND_NORMALIZATIONS = [
    (re.compile(r"front[\s-]*end", re.IGNORECASE), "frontend"),
    (re.compile(r"back[\s-]*end", re.IGNORECASE), "backend"),
    (re.compile(r"full[\s-]*stack", re.IGNORECASE), "fullstack"),
    (re.compile(r"dev[\s-]*ops", re.IGNORECASE), "devops"),
    
    # Merge any combination of UI and UX into a single canonical token
    # so queries for "ui", "ux", or "ui/ux" symmetrically match titles using any variant.
    (re.compile(r"\b(ui[\s/-]*ux|ux[\s/-]*ui|ui|ux)\b", re.IGNORECASE), "ui_ux"),
]

# Companies routinely use these words interchangeably in real job titles
# ("Backend Developer" / "Backend Engineer" / "Backend Programmer" are the
# same job). Without this, the AND-of-every-word matcher below silently
# rejects a title just because the company happened to pick a different
# synonym than the one the candidate typed - which was the main cause of
# "the job is right there on their careers page but the search says no
# match". Collapsing all of these to one canonical word, on BOTH the
# query and the title, fixes that regardless of which side used which
# word. Plurals are folded in too (engineers/developers -> engineer).
_ROLE_WORD_SYNONYMS = [
    # Expand common abbreviations directly to engineer
    (re.compile(r"\bsde\b", re.IGNORECASE), "engineer"),
    (re.compile(r"\bswe\b", re.IGNORECASE), "engineer"),
    
    # Standardize developers/programmers to engineer FIRST
    (re.compile(r"\b(developer|programmer|coder)s?\b", re.IGNORECASE), "engineer"),
    
    # Strip filler words like "software" and "development" when paired with engineer.
    # This prevents asymmetric mismatches (e.g., "backend developer" vs "backend software engineer" 
    # now both resolve to exactly "backend engineer").
    (re.compile(r"\bsoftware\s+(dev\s+|development\s+)?engineers?\b", re.IGNORECASE), "engineer"),
    
    # Catch any remaining plural engineers
    (re.compile(r"\bengineers\b", re.IGNORECASE), "engineer"),
]


def _normalize_role_text(text):
    for pattern, repl in _COMPOUND_NORMALIZATIONS:
        text = pattern.sub(repl, text)
    for pattern, repl in _ROLE_WORD_SYNONYMS:
        text = pattern.sub(repl, text)
    return text


def _plural_flexible(token):
    """A token pattern that doesn't care whether the query or the title
    happens to be singular or plural ("analyst" must still match
    "Analysts", and vice versa) - another common, avoidable source of a
    real listing being reported as "no match"."""
    if token.endswith("s") and len(token) > 3:
        return re.escape(token[:-1]) + "s?"
    return re.escape(token) + "s?"


def build_role_pattern(role_query):
    """
    Build a pattern that matches a job title containing every word from
    role_query, in ANY order, anywhere in the title - not just as one
    exact contiguous phrase. This is what makes "backend developer"
    correctly match titles like "Software Development Engineer -
    Backend" or "Backend Software Engineer" (via the developer/engineer
    synonym fold above), not just the literal phrase "backend developer".
    """
    normalized = _normalize_role_text(role_query.strip())
    tokens = [_plural_flexible(t) for t in re.split(r"\s+", normalized) if t]
    if not tokens:
        return re.compile(".*")
    lookaheads = "".join(rf"(?=.*\b{t}\b)" for t in tokens)
    return re.compile(lookaheads, re.IGNORECASE | re.DOTALL)


def build_exclude_pattern(role_query, include_senior):
    """
    The seniority-exclude list (senior/lead/staff/manager/director/...)
    exists to hide over-qualified titles when someone searches for a
    junior/mid role. But several of those words - "manager" above all -
    are legitimate role titles people search for directly ("product
    manager", "project manager"). If the user's own query already
    contains one of those words, it must not be used to exclude their
    own results.
    """
    if include_senior:
        return None
    role_lower = _normalize_role_text(role_query).lower()
    remaining = [k for k in EXCLUDE_KEYWORDS if k.strip().lower() not in role_lower]
    if not remaining:
        return None
    return re.compile("|".join(re.escape(k) for k in remaining), re.IGNORECASE)


def title_matches(title, role_pattern, exclude_pattern):
    if not title:
        return False
    if exclude_pattern is not None and exclude_pattern.search(title):
        return False
    return bool(role_pattern.search(_normalize_role_text(title)))


# ---------------------------------------------------------------------
# Per-platform raw -> normalized record extraction
# ---------------------------------------------------------------------

def _rec_greenhouse(job):
    return {
        "title": job.get("title", ""),
        "location": (job.get("location") or {}).get("name", ""),
        "posted_raw": job.get("updated_at", ""),
        "link": job.get("absolute_url", ""),
        "description": job.get("content", ""),
    }


def _rec_lever(job):
    return {
        "title": job.get("text", ""),
        "location": (job.get("categories") or {}).get("location", ""),
        "posted_raw": job.get("createdAt"),
        "link": job.get("hostedUrl", ""),
        "description": (job.get("description", "") or "") + " " + str(job.get("lists", "")),
    }


def _rec_smartrecruiters(job, company_token=""):
    """Normalize a SmartRecruiters posting into a public application link.

    Most listing responses include ``company.identifier``, but it is an
    optional field in the public API. The adapter already knows the company
    token used for the request, so use it as a dependable fallback rather
    than emitting ``jobs.smartrecruiters.com//<id>``.
    """
    company_identifier = (job.get("company") or {}).get("identifier") or company_token
    posting_id = job.get("id") or job.get("uuid") or ""
    link = job.get("applyUrl") or job.get("jobAdUrl")
    if not link and company_identifier and posting_id:
        link = f"https://jobs.smartrecruiters.com/{company_identifier}/{posting_id}"
    return {
        "title": job.get("name", ""),
        "location": (job.get("location") or {}).get("city", ""),
        "posted_raw": job.get("releasedDate", ""),
        "link": link or "",
        "description": "",
    }


def _rec_ashby(job):
    return {
        "title": job.get("title", ""),
        "location": job.get("location", "") or job.get("locationName", ""),
        "posted_raw": job.get("publishedAt", ""),
        "link": job.get("jobUrl", "") or job.get("applyUrl", ""),
        "description": job.get("descriptionPlain", "") or "",
    }


def _rec_workday(job, tenant, wd_host, site):
    path = job.get("externalPath", "")
    link = f"https://{tenant}.{wd_host}.myworkdayjobs.com/{site}{path}" if path else ""
    return {
        "title": job.get("title", ""),
        "location": job.get("locationsText", "") or (job.get("bulletFields") or [""])[0],
        "posted_raw": job.get("postedOn", ""),
        "link": link,
        "description": "",
    }


def _parse_dt(posted_raw, platform):
    if not posted_raw:
        return None
    try:
        if platform == "lever":
            return datetime.fromtimestamp(posted_raw / 1000, tz=timezone.utc)
        if platform in ("greenhouse", "greenhouse-eu", "smartrecruiters", "ashby"):
            return datetime.fromisoformat(str(posted_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError, OSError):
        return None
    return None


def _bucket(posted_raw, platform, days):
    if platform == "workday":
        age_days = relative_days_from_workday_string(posted_raw)
        if age_days is None:
            return "date unknown", 2
        sort_key = 0 if age_days <= days else 1
        return (f"recent (<= {days}d)" if sort_key == 0 else f"older ({age_days}d ago)"), sort_key
    dt = _parse_dt(posted_raw, platform)
    return date_bucket(dt, days)


def _build_kept(raw_jobs, platform, extractor, role_pattern, exclude_pattern,
                 max_experience, days, extra=()):
    kept = []
    for job in raw_jobs:
        rec = extractor(job, *extra) if extra else extractor(job)
        title = rec["title"]
        if not title_matches(title, role_pattern, exclude_pattern):
            continue
        bucket, sort_key = _bucket(rec["posted_raw"], platform, days)
        req_years = extract_experience_years(rec["description"]) if rec["description"] else None
        ok, exp_note = passes_experience_filter(req_years, max_experience)
        if not ok:
            continue
        kept.append({
            "title": title,
            "location": rec["location"] or "",
            "posted": rec["posted_raw"] or "date unknown",
            "date_bucket": bucket,
            "sort_key": sort_key,
            "experience_note": exp_note,
            "link": rec["link"],
        })
    kept.sort(key=lambda j: j["sort_key"])
    return kept


EXTRACTORS = {
    "greenhouse": _rec_greenhouse,
    "greenhouse-eu": _rec_greenhouse,
    "lever": _rec_lever,
    "smartrecruiters": _rec_smartrecruiters,
    "ashby": _rec_ashby,
}


# ---------------------------------------------------------------------
# Known company (already in hulk_job_search.COMPANIES) - use confirmed
# platform/token directly.
# ---------------------------------------------------------------------

def _build_kept_custom(listings, role_pattern, exclude_pattern):
    """Same idea as _build_kept, but for generic-scrape {"title","link"}
    listings that have no date/location/description to filter on - just
    a title match against the free-text role query."""
    kept = []
    for j in listings:
        title = j.get("title", "")
        searchable_text = f"{title} {j.get('description', '')}"
        if not title_matches(searchable_text, role_pattern, exclude_pattern):
            continue
        required_years = extract_experience_years(j.get("description", ""))
        experience_note = (
            f"{required_years}+ yrs stated" if required_years is not None
            else "not stated on career page"
        )
        kept.append({
            "title": title,
            "location": "",
            "posted": "date unknown (career-page scrape)",
            "date_bucket": "date unknown",
            "sort_key": 2,
            "experience_note": experience_note,
            "link": j.get("link", ""),
        })
    return kept


def _search_custom(company, role_pattern, exclude_pattern, role_query=""):
    careers_url = company.get("careers_url")
    # fetch_custom_site_jobs always scrapes the site's plain, unfiltered
    # listing (see its docstring - it never sends the query to the
    # third-party site) - all role matching happens locally, right below.
    listings = fetch_custom_site_jobs(careers_url) if careers_url else []
    if not listings:
        return [], "custom-scrape", 0, (
            f"Checked their career page but couldn't extract job listings "
            f"(often means it's a JavaScript-rendered page our scraper can't "
            f"read). Check it directly: {careers_url or '(no URL on file)'}"
        )
    kept = _build_kept_custom(listings, role_pattern, exclude_pattern)
    if kept:
        return kept, "custom-scrape", len(listings), None
    return [], "custom-scrape", len(listings), (
        f"Found {len(listings)} listing(s) on their career page, but none matched "
        f"\"{role_query}\" - try a broader role term, or check the page directly: {careers_url}"
    )


def _no_match_note(used, total_jobs, role_query, max_experience):
    """Explanatory note for the case that was previously silent: the
    platform API responded fine and had open roles, but every one of
    them was filtered out by the free-text role match / seniority
    exclude / experience-years filter. Without this, a company whose
    board genuinely has the role (visible if you open the careers page
    yourself) came back as a bare "no match" with zero explanation -
    indistinguishable from "this company has nothing right now"."""
    if total_jobs == 0:
        return f"Found this company on {used}, but they have no open roles listed right now."
    return (
        f"Found this company on {used} ({total_jobs} open role(s)), but none matched "
        f"\"{role_query}\" within your {max_experience}-year experience filter. Try a "
        f"broader role term, raise the experience cap, or check the board directly."
    )


def _search_known_company_impl(company, role_query, max_experience, days, include_senior,
                                allow_workday_guess=True):
    role_pattern = build_role_pattern(role_query)
    exclude_pattern = build_exclude_pattern(role_query, include_senior)

    # Cheap, cached: fills in token/tenant/wd_host/site if this company was
    # only loaded from the CSV with a platform *hint* and no credentials
    # yet. allow_workday_guess=True (single dashboard search) also allows the
    # slower brute-force Workday guess. Job Watcher's bulk scan passes False.
    #
    # If discovery was INCONCLUSIVE (timeout / 403 / 429 / network), the
    # company is left unresolved and NOT cached (see resolve_company_platform)
    # and company["_lookup_unreliable"] is True.
    resolve_company_platform(company, try_workday_guess=allow_workday_guess)
    platform = company.get("platform")
    api_failed = bool(company.get("_lookup_unreliable"))
    _reset_probe_errors()  # from here on, only track failures of THIS search's calls

    if platform == "greenhouse" and company.get("token"):
        region = "eu" if company.get("gh_region") == "eu" else None
        jobs, used = try_greenhouse(company["token"], region=region)
        if jobs is None:
            other = None if region == "eu" else "eu"
            jobs, used = try_greenhouse(company["token"], region=other)
        if jobs is not None:
            kept = _build_kept(jobs, "greenhouse", _rec_greenhouse, role_pattern,
                                exclude_pattern, max_experience, days)
            note = None if kept else _no_match_note(used, len(jobs), role_query, max_experience)
            return kept, used, len(jobs), note
        api_failed = True  # confirmed board didn't answer

    elif platform == "lever" and company.get("token"):
        jobs, used = try_lever(company["token"])
        if jobs is not None:
            kept = _build_kept(jobs, "lever", _rec_lever, role_pattern,
                                exclude_pattern, max_experience, days)
            note = None if kept else _no_match_note(used, len(jobs), role_query, max_experience)
            return kept, used, len(jobs), note
        api_failed = True

    elif platform == "smartrecruiters" and company.get("token"):
        jobs, used = try_smartrecruiters(company["token"])
        if jobs is not None:
            kept = _build_kept(jobs, "smartrecruiters", _rec_smartrecruiters, role_pattern,
                                exclude_pattern, max_experience, days, extra=(company["token"],))
            note = None if kept else _no_match_note(used, len(jobs), role_query, max_experience)
            return kept, used, len(jobs), note
        api_failed = True

    elif platform == "workday":
        tenant, wd_host, site = company.get("tenant"), company.get("wd_host"), company.get("site")
        jobs, used = try_workday(tenant, wd_host, site)
        if jobs is not None:
            kept = _build_kept(jobs, "workday", _rec_workday, role_pattern,
                                exclude_pattern, max_experience, days, extra=(tenant, wd_host, site))
            note = None if kept else _no_match_note(used, len(jobs), role_query, max_experience)
            return kept, used, len(jobs), note
        if tenant and wd_host and site:
            api_failed = True  # had real credentials and it still didn't answer

    elif platform == "custom":
        return _finalize(_search_custom(company, role_pattern, exclude_pattern, role_query),
                         company, api_failed)

    # confirmed hint failed to respond (network/API issue) - try ashby as a
    # secondary guess before giving up.
    ashby_candidates = [company["ashby_token"]] if company.get("ashby_token") else slug_variants(company["name"])
    for candidate in ashby_candidates:
        jobs, used = try_ashby(candidate)
        if jobs:
            kept = _build_kept(jobs, "ashby", _rec_ashby, role_pattern,
                                exclude_pattern, max_experience, days)
            note = None if kept else _no_match_note(used, len(jobs), role_query, max_experience)
            return kept, used, len(jobs), note

    # Last resort: even if the hint said a real ATS, that guess/discovery
    # may have been wrong or the API may be down - try scraping their
    # careers page directly rather than giving up with nothing.
    if company.get("careers_url"):
        return _finalize(_search_custom(company, role_pattern, exclude_pattern, role_query),
                         company, api_failed)

    return [], None, 0, _lookup_failed_note(company)


# ---------------------------------------------------------------------
# Unknown company - guess a slug, try the platforms with public JSON
# APIs. Bounded so a single request doesn't hang forever.
# ---------------------------------------------------------------------

def _search_unknown_company_impl(name, role_query, max_experience, days, include_senior):
    role_pattern = build_role_pattern(role_query)
    exclude_pattern = build_exclude_pattern(role_query, include_senior)
    variants = slug_variants(name)[:2]  # bound worst-case latency
    _reset_probe_errors()

    attempts = [
        ("greenhouse", lambda s: try_greenhouse(s, region=None)),
        ("greenhouse-eu", lambda s: try_greenhouse(s, region="eu")),
        ("lever", try_lever),
        ("smartrecruiters", try_smartrecruiters),
        ("ashby", try_ashby),
    ]

    for slug in variants:
        for platform, fn in attempts:
            jobs, used = fn(slug)
            if jobs:
                extractor = EXTRACTORS[used] if used in EXTRACTORS else EXTRACTORS[platform]
                extractor_extra = (slug,) if used == "smartrecruiters" else ()
                kept = _build_kept(jobs, platform, extractor, role_pattern,
                                    exclude_pattern, max_experience, days, extra=extractor_extra)
                if kept:
                    return kept, used, len(jobs), "best_effort"
                return [], used, len(jobs), (
                    f"Found this company on {used} ({len(jobs)} open role(s)), but "
                    f"none matched \"{role_query}\" with your experience filter."
                )

    if _probe_was_inconclusive():
        # Every guess either 404'd OR failed to answer - we can't claim
        # "unsupported" when part of the answer was a timeout/block.
        return [], None, 0, LookupFailedNote(
            f"Couldn't reach the job-board APIs while looking up \"{name}\" (timeout, block, "
            f"or network error), so we can't tell whether it's supported. Please try again shortly."
        )

    return [], None, 0, (
        "We don't have this company configured, and couldn't auto-detect it on "
        "Greenhouse, Lever, SmartRecruiters, or Ashby (it may run on Workday, a "
        "custom JS-rendered site, or a different slug than we guessed). We don't "
        "support this company yet - you're welcome to check their careers page "
        "directly, or ask to have it added."
    )


# ---------------------------------------------------------------------
# Optional LLM re-ranking (ai_match.py) on top of the regex/keyword pass
# above. The regex pass ALWAYS runs first and remains the only filter -
# this only reorders/annotates an already-filtered shortlist, and is a
# no-op (returns the input unchanged) whenever ai_match isn't configured
# or the call fails for any reason, so nothing here can make a search
# fail or return fewer honest results than before.
# ---------------------------------------------------------------------

def _apply_ai_ranking(kept, role_query, max_experience):
    if not kept or not ai_match.is_configured():
        return kept
    capped = kept[:ai_match.MAX_CANDIDATES_PER_CALL]
    remainder = kept[ai_match.MAX_CANDIDATES_PER_CALL:]
    scored = ai_match.score_candidates(role_query, max_experience, capped)
    if not scored:
        return kept  # AI unavailable/failed this call - fail open, keep regex order
    scored.sort(key=lambda j: -j.get("ai_score", 0))
    return scored + remainder


# ---------------------------------------------------------------------
# Optional bounded agentic step on top of AI ranking: for the strongest
# matches only, draft a tailored outreach note (see
# ai_match.draft_outreach_note). Capped at MAX_DRAFTS_PER_SEARCH calls so
# one search can never trigger more than a handful of extra model calls.
# Fail-open throughout - a missing/failed draft just means that job's
# card has no "draft_note" key; nothing else about the response changes.
# ---------------------------------------------------------------------

def _apply_ai_drafts(kept, role_query, company_name):
    if not kept or not ai_match.is_configured():
        return kept
    drafted = 0
    for job in kept:
        if drafted >= ai_match.MAX_DRAFTS_PER_SEARCH:
            break
        if job.get("ai_score", 0) < ai_match.DRAFT_SCORE_THRESHOLD:
            continue
        note = ai_match.draft_outreach_note(role_query, job, company_name)
        if note:
            job["draft_note"] = note
        drafted += 1  # count attempts, not just successes, so one flaky call can't lift the cap
    return kept


def search_known_company(company, role_query, max_experience, days, include_senior,
                          allow_workday_guess=True, apply_ai=True):
    kept, used, total_found, note = _search_known_company_impl(
        company, role_query, max_experience, days, include_senior, allow_workday_guess
    )
    if apply_ai:
        kept = _apply_ai_ranking(kept, role_query, max_experience)
        kept = _apply_ai_drafts(kept, role_query, company["name"])
    return kept, used, total_found, note


def search_unknown_company(name, role_query, max_experience, days, include_senior, apply_ai=True):
    kept, used, total_found, note = _search_unknown_company_impl(
        name, role_query, max_experience, days, include_senior
    )
    if apply_ai:
        kept = _apply_ai_ranking(kept, role_query, max_experience)
        kept = _apply_ai_drafts(kept, role_query, name)
    return kept, used, total_found, note


# ---------------------------------------------------------------------
# Job Watcher support - scan every configured company at once, in
# parallel (network calls, so threads - not CPU-bound), for a given
# role. Companies with a confirmed/discoverable API (Greenhouse, Lever,
# SmartRecruiters, Ashby, Workday) are queried directly; everything else
# ("custom" / Own Portal / Taleo / unresolved) falls back to the generic
# career-page scraper (fetch_custom_site_jobs) instead of being skipped -
# so a full scan now covers the whole CSV, not just the API-backed subset.
#
# allow_workday_guess is always False here: the expensive brute-force
# Workday tenant guess (up to ~40 requests) is only worth paying for a
# single company on demand (see search_known_company) - for a scan of
# hundreds of companies it would make one scan take far too long. Any
# Workday company resolved via a dashboard search gets cached to disk,
# so it becomes fast here on the *next* scan automatically.
# ---------------------------------------------------------------------

def search_all_known_companies(role_query, max_experience, days, include_senior, max_workers=16):
    scannable = [c for c in COMPANIES if c.get("careers_url")]
    skipped = [
        {"company": c["name"], "careers_url": c.get("careers_url", ""),
         "logo_domain": company_domain(c)}
        for c in COMPANIES if not c.get("careers_url")
    ]

    def _run(company):
        try:
            # apply_ai=False: an LLM call per company, times up to 300+
            # companies every cycle, would make the watcher slow and
            # expensive. The regex/keyword pass alone still decides every
            # match here; AI re-ranking + the cycle digest are applied
            # once, below, over the already-small "matched" shortlist.
            jobs, platform_used, total_found, note = search_known_company(
                company, role_query, max_experience, days, include_senior,
                allow_workday_guess=False, apply_ai=False,
            )
        except Exception as e:  # a single company's failure shouldn't sink the scan
            jobs, platform_used, total_found = [], None, 0
            note = LookupFailedNote(f"error while scanning: {e}")
        return {
            "company": company["name"],
            "careers_url": company.get("careers_url", ""),
            "logo_domain": company_domain(company),
            "platform_used": platform_used,
            "total_open_roles_seen": total_found,
            "match_count": len(jobs),
            "note": note,
            "lookup_failed": is_lookup_failure(note),
            "jobs": jobs,
        }

    results = []
    if scannable:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for res in ex.map(_run, scannable):
                results.append(res)

    results.sort(key=lambda r: (-r["match_count"], r["company"].lower()))
    matched = [r for r in results if r["match_count"] > 0]
    unreachable = [r["company"] for r in results if r["lookup_failed"]]

    # AI layer, applied ONCE per cycle over the already-filtered "matched"
    # shortlist (typically a handful of companies, not 300+) rather than
    # per company mid-scan: re-score/re-rank each matched company's jobs,
    # then have the model reason over the whole cycle's pattern of results
    # into a short digest. Both are no-ops (ai_digest stays None, jobs
    # keep their regex-only order) whenever ai_match isn't configured or
    # the call fails - the watcher's core behavior never depends on it.
    ai_digest = None
    if matched and ai_match.is_configured():
        for company_result in matched:
            company_result["jobs"] = _apply_ai_ranking(
                company_result["jobs"], role_query, max_experience
            )
        matched.sort(key=lambda r: (
            -max((j.get("ai_score", 0) for j in r["jobs"]), default=0),
            -r["match_count"],
        ))
        ai_digest = ai_match.summarize_watcher_cycle(role_query, matched)

    return {
        "companies_scanned": len(scannable),
        "companies_skipped": skipped,
        "companies_with_matches": len(matched),
        "companies_unreachable": len(unreachable),
        "unreachable_names": unreachable[:50],
        "total_matches": sum(r["match_count"] for r in results),
        "matched": matched,
        "results": results,
        "ai_digest": ai_digest,
    }


def is_known_company(name):
    return next((c for c in COMPANIES if c["name"].lower() == name.strip().lower()), None)


def known_company_names():
    return sorted(c["name"] for c in COMPANIES)


def known_companies_directory():
    """Full company directory for the UI (name, careers link, logo,
    live-searchable or not) - used by the company picker and Profile page."""
    return sorted(
        (
            {
                "name": c["name"],
                "careers_url": c.get("careers_url", ""),
                "logo_domain": company_domain(c),
                "live_searchable": bool(c.get("careers_url")),
            }
            for c in COMPANIES
        ),
        key=lambda c: c["name"].lower(),
    )


    kws = list(dict.fromkeys(k.strip() for k in remaining))
    return re.compile(
        "|".join(rf"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])" for k in kws),
        re.IGNORECASE,
    )