"""
ai_match.py
============
Optional LLM layer on top of the existing regex/keyword pipeline in
role_search.py. It never runs as the ONLY filter - the regex/keyword pass
in role_search.py always runs first and remains the sole filter when this
module is unavailable, so nothing about the app's existing behavior
changes if no API key is configured.

Uses Google's Gemini API (generativelanguage.googleapis.com).

Two things use this module:

  1. score_candidates()
     Single-company dashboard search. Takes an already regex-filtered
     shortlist and asks the model to judge REAL fit (title, seniority,
     domain) rather than just keyword overlap, e.g. "Backend developer"
     matching "Software Development Engineer - Backend" (good) vs.
     "Backend Team Happy Hour Coordinator" slipping past a looser regex
     (bad) - the model catches what a keyword pattern structurally can't.

  2. summarize_watcher_cycle()
     Job Watcher. After a full scan across every configured company, asks
     the model to write a short natural-language digest of that cycle
     instead of just a match count - e.g. flagging that several companies
     posted a burst of senior-only roles, or that a specific match looks
     like an unusually strong fit. This is the "agentic" half: the
     recurring background loop reasons about the *pattern* of a batch of
     results, not just a single classification.

Both are opt-in via the GEMINI_API_KEY environment variable. If it isn't
set, is_configured() returns False and both public functions return None
immediately. Callers MUST treat None as "AI unavailable right now, fall
back to the existing regex-only behavior" and must never raise on it -
same fail-open philosophy as db.py (Mongo) and app.py (Google sign-in):
the app works fully without this configured, it just doesn't get the
extra ranking/digest layer. The same fail-open path also covers network
errors, timeouts, rate limits, and malformed model output, so a flaky API
never breaks a search or a scan - see eval_matching.py for the metrics
that depend on this holding.
"""

import json
import os

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# A dashboard search or a watcher cycle should never hang waiting on the
# model - if it's slow, fail open and show the regex-only results instead.
REQUEST_TIMEOUT_SECONDS = 12

# Bounds prompt size, cost, and worst-case latency per call. Dashboard
# searches are already regex-filtered down to a shortlist before this
# module ever sees them, so 25 is generous headroom, not a real limit.
MAX_CANDIDATES_PER_CALL = 25

# Watcher digests summarize across many companies at once; bound how many
# individual jobs get quoted into that one prompt.
MAX_DIGEST_JOBS = 40

# Bounded agentic step (see draft_outreach_note below): only the strongest
# matches get a drafted note, and only up to this many per single search,
# so one search can never trigger more than a handful of extra model calls.
DRAFT_SCORE_THRESHOLD = 80
MAX_DRAFTS_PER_SEARCH = 3


def is_configured():
    return bool(GEMINI_API_KEY)


def _call_gemini(prompt, max_output_tokens, json_mode=False):
    """Low-level call. Returns the response text, or None on ANY failure
    (network error, timeout, non-200 status, malformed body, or the
    model stopping for a reason other than finishing normally) - never
    raises, since every caller here needs to fail open."""
    if not GEMINI_API_KEY:
        return None

    generation_config = {"maxOutputTokens": max_output_tokens}
    if json_mode:
        generation_config["responseMimeType"] = "application/json"

    try:
        resp = requests.post(
            API_URL_TEMPLATE.format(model=GEMINI_MODEL),
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "content-type": "application/json",
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        return text or None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def _parse_json_block(text):
    """Strip ```json fences if the model added them anyway, then parse.
    Returns None (never raises) on any parse failure."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if "\n" in cleaned:
            first_line, rest = cleaned.split("\n", 1)
            cleaned = rest if first_line.strip().lower() in ("json", "") else cleaned
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None


def score_candidates(role_query, max_experience, candidates):
    """
    candidates: list of job dicts (the same shape role_search._build_kept
    produces) - each needs at least "title"; "location" and
    "experience_note" are used as extra context when present.

    Returns a NEW list of dicts (copies of the input candidates, in the
    same relative content, now carrying "ai_score" (0-100 int) and
    "ai_reason" (short string)) - or None if AI is unavailable/failed,
    which the caller must treat as "keep the original regex ordering
    unchanged".
    """
    if not is_configured() or not candidates:
        return None

    capped = candidates[:MAX_CANDIDATES_PER_CALL]
    listing_lines = []
    for i, c in enumerate(capped):
        title = (c.get("title") or "").replace('"', "'")
        location = (c.get("location") or "").replace('"', "'")
        exp_note = (c.get("experience_note") or "").replace('"', "'")
        listing_lines.append(
            f'{i}: title="{title}" location="{location}" experience_note="{exp_note}"'
        )

    prompt = (
        "You are screening a shortlist of job listings for a candidate.\n"
        f'The candidate is looking for: "{role_query}", with at most '
        f"{max_experience} years of experience.\n\n"
        "Listings (already keyword-matched, but keyword overlap alone can "
        "be misleading - e.g. a title can contain the right words while "
        "being a completely different role, or the right role at the "
        "wrong seniority):\n" + "\n".join(listing_lines) + "\n\n"
        "For EVERY listing above, score how well it actually fits what the "
        "candidate wants.\n\n"
        "Respond with ONLY a JSON array, one object per listing, no other "
        "text and no markdown fences, in this exact shape:\n"
        '[{"index": 0, "score": 0, "reason": "under 12 words"}]'
    )

    parsed = _parse_json_block(_call_gemini(prompt, max_output_tokens=1200, json_mode=True))
    if not isinstance(parsed, list):
        return None

    results = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(capped)):
            continue
        try:
            score = int(item.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        reason = str(item.get("reason", "") or "")[:160]

        out = dict(capped[idx])
        out["ai_score"] = score
        out["ai_reason"] = reason
        results.append(out)

    return results or None


def summarize_watcher_cycle(role_query, matched_companies):
    """
    matched_companies: the "matched" list search_all_known_companies()
    already produces - each item has "company", "match_count", "jobs"
    (list of job dicts with "title", possibly "ai_score"/"ai_reason").

    Returns a short (2-4 sentence) natural-language digest string, or
    None if AI is unavailable/failed - callers must fall back to showing
    just the plain match-count summary they already build today.
    """
    if not is_configured() or not matched_companies:
        return None

    lines = []
    job_count = 0
    for company_result in matched_companies:
        for job in company_result.get("jobs", []):
            if job_count >= MAX_DIGEST_JOBS:
                break
            tag = ""
            if "ai_score" in job:
                tag = f' (fit score {job["ai_score"]}/100)'
            lines.append(f'- {company_result["company"]}: "{job.get("title", "")}"{tag}')
            job_count += 1
        if job_count >= MAX_DIGEST_JOBS:
            break

    prompt = (
        "You are an assistant summarizing one scan cycle of an automated "
        f'job watcher looking for: "{role_query}".\n\n'
        f"{len(matched_companies)} companies had at least one matching "
        "role this cycle. Matches:\n" + "\n".join(lines) + "\n\n"
        "Write a 2-4 sentence digest a candidate would actually want to "
        "read: call out anything notable (a standout fit, an unusual "
        "cluster of postings at one company, a pattern across companies) "
        "rather than restating the raw list. Plain text only, no headers, "
        "no markdown, no JSON."
    )

    text = _call_gemini(prompt, max_output_tokens=300, json_mode=False)
    return text.strip() if text else None


def draft_outreach_note(role_query, job, company_name):
    """
    Bounded agentic step: for a single, already-strong match, draft a
    short (2-3 line) outreach note the candidate could send to a
    recruiter or hiring manager, referencing the actual job title and
    company. This is a genuine autonomous *action* (drafting text on the
    candidate's behalf) rather than pure classification/summarization -
    but it stays bounded and gated:
      - only called by role_search._apply_ai_drafts for jobs that
        already scored >= DRAFT_SCORE_THRESHOLD from score_candidates()
      - capped at MAX_DRAFTS_PER_SEARCH calls per search (see caller)
      - never auto-sent anywhere - the draft is just text returned to
        the caller, shown behind a "Draft outreach note" toggle in the
        UI; a human decides whether to use it at all

    Returns a short string, or None if AI is unavailable/failed - the
    caller must treat None as "no draft available", never an error.
    """
    if not is_configured() or not job:
        return None

    title = (job.get("title") or "").replace('"', "'")
    location = (job.get("location") or "").replace('"', "'")
    safe_company = (company_name or "").replace('"', "'")

    prompt = (
        "Draft a short, genuine-sounding outreach message a job candidate "
        "could send (e.g. on LinkedIn or by email) about a specific open "
        "role. Do not invent facts about the candidate's background - "
        "keep it generic on that front, and focused on genuine interest "
        "in the role.\n\n"
        f'Candidate is searching for: "{role_query}"\n'
        f'Company: "{safe_company}"\n'
        f'Job title: "{title}"\n'
        f'Location: "{location or "not specified"}"\n\n'
        "Write 2-3 sentences, plain text only, no subject line, no "
        "markdown, no placeholders like [Your Name] - end naturally "
        "without a signature line, since the candidate will add their "
        "own."
    )

    text = _call_gemini(prompt, max_output_tokens=180, json_mode=False)
    return text.strip() if text else None
