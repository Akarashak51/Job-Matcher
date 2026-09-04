# 5-minute pitch outline (Open Track)

The brief says: "Prioritize a working demo over extra features... Record
your pitch video like you are explaining the build to an engineer, not a
recruiter, focus on architecture and trade-offs. Document the one failure
case you handled gracefully." This outline is built around exactly that,
timed to fit a 5-minute video.

## 0:00 – 0:40 — The problem (be concrete, not generic)

- Don't say "job searching is hard." Say the specific thing: checking
  300+ companies' career pages one by one for a specific role is either
  impossibly slow by hand, or requires 300+ different scrapers because
  every company uses a different ATS (Greenhouse, Lever, Workday, or
  nothing public at all).
- Show the `companies.csv` — 321 real companies, not a toy list.

## 0:40 – 1:40 — Architecture walkthrough (use `architecture.svg`)

Walk the diagram left to right, narrating the decisions, not just the
boxes:

- CSV → normalized platform hint → in-memory company records. Why a CSV
  and not hardcoded Python: adding a company is a spreadsheet edit, not a
  code change.
- `hulk_job_search.py` resolves the *actual* ATS token/tenant the first
  time a company is searched (a hint like "Workday" isn't enough to query
  it), and `platform_cache.py` persists that so it's never paid twice.
  Mention `warm_cache.py` as the "do this once after deploy" tool.
- The regex/keyword pass (`role_search.py`) is the **always-on** filter —
  say explicitly that this runs with or without AI, because it's what
  makes "backend developer" queryable against 300+ heterogeneous APIs at
  all.
- The AI layer (`ai_match.py`) sits **on top of, not instead of**, that
  filter — say why: an unbounded LLM call per job across 300+ companies
  every watcher cycle would be slow and expensive, so AI re-ranks an
  already-small shortlist instead of judging everything from scratch.

## 1:40 – 3:00 — Meaningful use of AI (this is the section judges will scrutinize most)

- Show a live example: a query like "product manager" keyword-matching
  `"Manager, Product Photography Studio"` — a real regex false positive.
  Show the AI-scored version dropping it and explaining why in one line.
- Show the Job Watcher digest: not just "12 matches found" but a short
  written summary of the *pattern* across a scan cycle — this is the
  agentic piece, reasoning over a batch of results, not classifying one
  input at a time.
- **Say the metric out loud, don't just show it**: on the labeled test
  set in `eval_matching.py`, regex-only precision is 0.60; regex+AI
  raises it to 1.00, with recall unchanged. Explain *why* recall doesn't
  move — AI can only remove false positives from an already-filtered
  shortlist, it can't recover a job the keyword pass excluded before AI
  ever saw it. Saying this limitation yourself, unprompted, is exactly
  what "honest metrics" means in the brief.

## 3:00 – 4:00 — The one failure case, in detail

Pick the Workday fallback chain (documented in README.md under "The one
failure case handled gracefully, end to end") and narrate it as a
sequence of decisions, not a list of features:

1. Try to read the tenant/host/site from the page — fails silently on
   JS-rendered pages.
2. Fall back to a bounded brute-force guess (capped at 45s, and only on a
   single dashboard search — never during a Job Watcher scan, because
   that would make the whole scan slow for one company).
3. Fall back further to generic HTML scraping.
4. If that also fails: return a clear, honest "couldn't reach this" note
   with a direct link — never a silent empty result.
- Then say the same philosophy applies to the AI layer one level up:
  unconfigured, rate-limited, or a malformed response — all collapse to
  the same "fall back to regex-only" behavior, verified by literally
  running the app with network access to every job board blocked and
  confirming it still returns clean `200`s with honest results instead of
  crashing.

## 4:00 – 4:40 — Demo, live

- One dashboard search with a visible AI fit score + reason on a card.
- Start the Job Watcher, let one cycle complete, show the digest text
  appearing.
- If time allows, run `eval_matching.py` on screen and let the
  precision numbers print live — more convincing than a slide.

## 4:40 – 5:00 — Close

- One sentence on what you'd build next given more time (e.g., persisting
  watcher-cycle history so the digest can compare cycle-over-cycle, or
  widening AI scoring to unlisted companies' best-effort search).
- Restate the one metric: **0.60 → 1.00 precision, recall held constant,
  on a labeled set with deliberate keyword traps.**

## Things to avoid on camera

- Don't read the README out loud — narrate decisions and trade-offs, the
  brief explicitly asks for this framing ("not a recruiter").
- Don't claim recall improved from AI — it structurally can't, and
  overclaiming here undermines the "honest metrics" credibility the
  precision number earns you.
- Don't skip the failure case to save time — it's explicitly graded per
  the brief; cut the demo before you cut this section.
