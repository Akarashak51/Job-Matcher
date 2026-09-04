# Job Finder

Enter a company + role, get live matching job openings from that company's
job board (Greenhouse, Lever, SmartRecruiters, Ashby, Workday, or a
generic career-page scrape as a fallback).

- **321 companies pre-loaded from `companies.csv`** (name, country, career
  website, platform hint) - see "Updating the company list" below. This
  replaces the old hardcoded 30-company list, which is kept as a fallback
  only (`hulk_job_search._BUILTIN_COMPANIES`) in case `companies.csv` is
  ever missing.
- Any other company name (not in the CSV) is still auto-detected on a
  best-effort basis, same as before.
- **AI-reranked matching** (optional, see "How AI is used" below) - an
  LLM re-scores the keyword-matched shortlist to catch titles that pass
  a keyword filter but aren't actually the role being searched for
  (`"Manager, Product Photography Studio"` matching a `"product manager"`
  search, for example).
- **Job Watcher** scans the *entire* CSV-driven company list in parallel,
  not just an API-backed subset - companies without a public JSON API
  (Own Portal / Taleo / unrecognized platform) are checked via a generic
  HTML career-page scraper instead of being skipped. When AI matching is
  configured, each scan cycle also gets a short AI-written digest instead
  of just a match count.
- **Google sign-in** (optional) - lets people log in; when combined with
  MongoDB Atlas (below), their history syncs across every device.
- **Light/dark theme** toggle (top-right), remembered per browser.
- **Search history** - synced to the cloud for signed-in users (via
  MongoDB Atlas), or saved in the browser only otherwise. Re-run or
  delete past searches.

## How AI is used

Everything up through the keyword pass (`role_search.build_role_pattern`)
is the pipeline's always-on, deterministic first filter - it has to run
regardless of whether AI is configured, since it's what turns "backend
developer" into something that can query 300+ different job boards at
all. What AI adds sits strictly on top of that, in `ai_match.py`, and is
entirely optional:

1. **Re-ranking a single search's shortlist** (`ai_match.score_candidates`).
   A keyword match on "product" and "manager" also matches `"Manager,
   Product Photography Studio"` - the words are right, the job isn't. The
   already-keyword-filtered shortlist (never the full unfiltered job
   list, to keep prompt size and cost bounded) gets sent to the model,
   which scores each listing 0-100 for actual fit and gives a one-line
   reason. Jobs are then sorted by that score.
2. **A digest for each Job Watcher cycle** (`ai_match.summarize_watcher_cycle`).
   The recurring background scan reasons over the *pattern* of a whole
   cycle's matches - a standout fit, a cluster of postings at one
   company - instead of just reporting a number, once per cycle over the
   small already-matched shortlist (not per company; scanning 300+
   companies with one LLM call each, every cycle, would make Job Watcher
   both slow and expensive - see the comment above `search_all_known_companies`
   in `role_search.py`).

**Both are fail-open by construction.** If `GEMINI_API_KEY` isn't set,
`ai_match.is_configured()` returns `False` and every function in that
module returns `None` immediately - callers treat `None` as "keep the
regex-only result," never as an error. The exact same fallback triggers
on a network error, a timeout, or a malformed model response, so a flaky
API can only ever cost you the AI layer, never break a search. This is
the same pattern `db.py` uses for MongoDB and `app.py` uses for Google
sign-in - the app runs in full without any of the three configured.

**Measured evidence, not a cherry-picked example:** `eval_matching.py` is
a small hand-labeled test set of role queries against job titles,
including deliberate keyword-matching traps (see above), run through the
regex-only baseline and the regex+AI pipeline side by side:

```bash
python3 eval_matching.py                    # regex-only baseline (no key needed)
GEMINI_API_KEY=AI... python3 eval_matching.py   # + the regex+AI comparison
```

On the included test set, regex-only precision is 0.60 (four of the ten
labeled titles are keyword-matching traps it can't tell apart from a real
match); adding the AI re-ranking step raises precision to 1.00 with
recall unchanged, since AI can only remove regex false positives, not
recover jobs the regex pass already excluded (that ceiling is inherent to
re-ranking a shortlist rather than reading every job, and is called out
explicitly in the script's own output, not glossed over). That number was
produced against a scripted stand-in for the model's responses, not a
live call - see the note in "Setting up Gemini" below before you quote it
in a pitch.

Configure with:

- `GEMINI_API_KEY` - from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
  (Google AI Studio's free tier is enough for this). Leave unset to run
  without the AI layer.
- `GEMINI_MODEL` (optional) - defaults to `gemini-3.6-flash`, a small,
  fast model, since job-listing classification doesn't need a large one;
  override if you want a different model.

### Setting up Gemini (free tier available)

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
   and sign in with a Google account.
2. Click **Create API key** (a free-tier key with rate limits is enough
   for this app - re-ranking a shortlist of a few jobs per search is a
   small, cheap call).
3. Set it as the `GEMINI_API_KEY` environment variable (see "Deploy for
   free on Render" below for where to put it in production, or export it
   locally before running `uvicorn`).
4. **Verify it actually works before you rely on it for a demo**: this
   project was built and code-reviewed in a sandboxed environment with no
   network access to `generativelanguage.googleapis.com`, so every
   AI-layer test here runs against a scripted stand-in for Gemini's
   response format, not a live call. That's normal engineering practice
   for unit tests, but it also means the real API's exact behavior
   (latency, JSON-mode compliance, rate limits) hasn't been observed
   firsthand - run `eval_matching.py` with a real key yourself once
   before recording your pitch, and treat the printed numbers as the
   ones to quote, not the ones in this README.

## Updating the company list

Edit `companies.csv` directly - it's a plain CSV with 4 columns:

```
company_name,country,careers_url,platform_hint
Razorpay,India,https://razorpay.com/careers,Lever
```

`platform_hint` can be `Greenhouse`, `Lever`, `SmartRecruiters`, `Ashby`,
`Workday`, or anything else (`Own Portal`, `Taleo`, blank, etc.) - anything
not recognized is treated as `custom` and handled by the generic scraper.
The hint doesn't need to be the exact API token/tenant - the app figures
that out itself the first time each company is searched (see below) and
remembers it, so you only ever need the company name + its public careers
page URL in the CSV.

Restart the app after editing `companies.csv` for changes to take effect
(it's loaded once at startup).

### How company resolution & caching works

The CSV only gives a careers page URL and a rough platform hint - not the
actual Greenhouse token or Workday tenant/host/site. The first time a
company is searched (either from the dashboard or Job Watcher), the app:

1. Fetches the careers URL once and looks for a known ATS pattern in the
   final redirected URL / page HTML (e.g. `boards.greenhouse.io/<token>`,
   `<tenant>.wd1.myworkdayjobs.com/<site>`).
2. Confirms the match with one real API call.
3. Saves the result to `platform_cache.json` so every future search/scan
   for that company reuses it instantly - no repeated discovery cost.
4. If nothing is found (common for heavily JS-rendered career pages),
   falls back to scraping the careers page's HTML directly for job-like
   links, and caches "this one really is custom" so it doesn't re-probe
   the page needlessly next time.

For Workday specifically, step 1 alone often isn't enough (many "Own
Portal"-style Workday pages don't put the tenant/host/site in plain HTML).
A single dashboard search will also try a slower brute-force guess as a
last resort (since the result gets cached and benefits every future scan)
- but Job Watcher's bulk scan intentionally skips that expensive guess
per company, so a never-before-searched Workday company may show 0
results in a scan until it's been searched once individually, or until
you run:

```bash
python warm_cache.py                 # resolve every company once, ahead of time
python warm_cache.py --limit 20       # quick test run on the first 20
python warm_cache.py --workday-only   # focus on just the Workday-hinted rows
```

This can take a while for 300+ companies (Workday guessing tries several
tenant/host/site combinations per company, capped at 45s total per
company so one stubborn site can't stall the whole run) - it's meant to
be run once after a fresh deploy or after a big CSV update, not on every
startup. `platform_cache.json` is safe to delete any time to force full
re-resolution (e.g. after fixing bad URLs in the CSV) - note that once a
company is marked "custom, confirmed" or "workday, unresolved" in the
cache, it's skipped instantly on future runs; delete its entry (or the
whole file) if you want it re-checked, e.g. after updating its URL.

### Coverage expectations

The generic scraper (used for `custom`-platform companies, and as a last
resort when a hinted ATS doesn't resolve) does a plain HTML fetch and
looks for job-like links - it works well on simple static career pages,
but can find nothing on heavily JavaScript-rendered ones (common on large
corporate career portals). When that happens, the response clearly says
so and links to the page directly rather than showing a false empty
result.

## The one failure case handled gracefully, end to end

Worth calling out explicitly (as the buildathon brief asks every track
to do): a Workday-hinted company from the CSV where the tenant/host/site
can't be found in the page's plain HTML - common on Workday deployments
that render the careers page client-side. The fallback chain, in order:

1. **Discovery** (`hulk_job_search.discover_platform_from_url`) tries to
   read the tenant/host/site straight from the redirected URL or page
   HTML - fails silently for JS-rendered pages, no exception raised.
2. **Brute-force guess** - a single dashboard search (not a Job Watcher
   scan, which explicitly skips this to stay fast) tries several
   tenant/host/site combinations, capped at 45s total so one stubborn
   site can't hang the request.
3. **Generic scrape fallback** (`fetch_custom_site_jobs`) - if Workday
   guessing also fails, the app falls back to scraping the plain careers
   page HTML for job-like links directly.
4. **Explicit "we couldn't" response** - if even that finds nothing (a
   heavily JS-rendered page with no plain-HTML job links either), the API
   returns `match_count: 0` with a `note` that says exactly what was
   tried and links straight to the company's careers page - never a
   silent empty result indistinguishable from "no jobs right now."

Every step either succeeds or fails **quietly** into the next one; the
only thing the person searching ever sees is either real results or one
clear sentence telling them what happened and where to look themselves.
Nothing in this chain raises an unhandled exception or returns a 500 -
confirmed by running the app with network access to job-board APIs fully
blocked (simulating every site above failing at once): every endpoint
still returns a clean `200` with an honest empty result and that same
explanatory note, rather than crashing.

The AI layer (`ai_match.py`) follows the identical philosophy one level
up: not configured, rate-limited, timed out, or a malformed response all
collapse to the same `None` return, which every caller treats as "fall
back to the regex-only result" - see "How AI is used" above.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

The site works fully without Google sign-in configured - the "Sign in
with Google" button just shows a friendly message until you set it up.
The AI re-ranking/digest layer works the same way: fully optional, see
"How AI is used" above.

## Setting up MongoDB Atlas (free, for cross-device history)

Without this, history still works but only in the visitor's browser (see
"About history" below). With it, signed-in users' history follows them
to any device.

1. Go to https://www.mongodb.com/cloud/atlas/register -> sign up (free).
2. Create a cluster: choose the **M0 Free** tier (always free, no credit
   card charge - 512MB storage, plenty for search history).
3. **Database Access** -> Add a database user (username + password -
   save these, you'll need them in the connection string).
4. **Network Access** -> Add IP Address -> **Allow access from anywhere**
   (`0.0.0.0/0`). This is the simplest option since Render's free tier
   uses dynamic IPs; the database user's password is what actually
   protects the data, so use a strong generated one.
5. Once the cluster is ready, click **Connect** -> **Drivers** -> copy
   the connection string. It looks like:
   `mongodb+srv://<username>:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`
6. Replace `<username>` and `<password>` with your actual database user
   credentials.
7. Set this as the `MONGODB_URI` environment variable (see deploy steps
   below). No need to create the database or collection by hand - the
   app creates them automatically on first write.

## Setting up Google sign-in (free)

1. Go to https://console.cloud.google.com/ -> create a project (free).
2. **APIs & Services -> OAuth consent screen** -> set it up as "External",
   fill in app name/support email, publish it (or leave in Testing mode
   and add your own Google account as a test user - both are free and
   fine for personal use).
3. **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID**
   - Application type: **Web application**
   - Authorized redirect URI:
     - Local testing: `http://127.0.0.1:8000/auth/callback`
     - Production: `https://<your-render-url>.onrender.com/auth/callback`
4. Copy the generated **Client ID** and **Client Secret**.
5. Set these as environment variables (see deploy steps below):
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `SESSION_SECRET` - any long random string, e.g. generate one with
     `python3 -c "import secrets; print(secrets.token_hex(32))"`

## Deploy for free on Render

1. Push this folder to a **GitHub repo** (`app.py`, `role_search.py`,
   `hulk_job_search.py`, `requirements.txt`, this README).
2. Go to https://render.com -> sign up (free) -> **New +** -> **Web Service**.
3. Connect your GitHub repo.
4. Fill in:
   - **Name**: whatever you like (e.g. `job-finder`)
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: **Free**
5. Under **Environment**, add:
   - The three Google sign-in variables from above (skip if you don't
     want login enabled yet).
   - `MONGODB_URI` from the Atlas setup above (skip if you don't want
     cloud history yet - the site falls back to browser-only history
     automatically).
   - `GEMINI_API_KEY` (skip if you don't want AI re-ranking/digests -
     the site falls back to regex-only matching automatically).
6. Click **Create Web Service**. Render builds and deploys automatically.
7. You'll get a URL like `https://job-finder-xxxx.onrender.com`. Go back
   to Google Cloud Console and make sure that exact URL + `/auth/callback`
   is listed as an authorized redirect URI (step 3 above).

### Free tier notes (be aware, not a blocker)

- Render's free web services **spin down after ~15 minutes of no traffic**
  and take ~30-50 seconds to wake up on the next request. That's normal
  for the free tier, not a bug.
- Searching an **unlisted company** tries several job-board APIs in a row
  and can take up to ~20-30 seconds.
- No database, no paid services used anywhere in this stack.

### About history

- **Signed in + `MONGODB_URI` set**: history is stored in MongoDB Atlas,
  scoped to your Google account's email. Sign in on any device, any
  browser, and it's there.
- **Signed in but `MONGODB_URI` not set**: history falls back to that
  browser's `localStorage`, scoped to your email (won't sync elsewhere).
  The History section says this explicitly.
- **Not signed in**: history is always browser-only (`localStorage`),
  scoped to "guest" on that browser. Clearing browser data / incognito
  wipes it.
- MongoDB Atlas's free M0 tier has no time limit and no cost - 512MB is
  far more than search history needs. The app keeps only the most
  recent 50 entries per account to stay well within that.

## Architecture

See `architecture.svg` for the full data-flow diagram. In short:

```
companies.csv --> companies_source.py --> hulk_job_search.COMPANIES
                                                    |
platform_cache.json <--> platform_cache.py <-------+ (resolved ATS token/tenant, cached)
                                                    |
                                     hulk_job_search.py (per-ATS fetchers,
                                     generic scraper, discovery/guessing)
                                                    |
                                          role_search.py
                                     regex/keyword pass (always on)
                                                    |
                                     ai_match.py (optional re-ranking +
                                     watcher digest - fails open to the
                                     line above if unconfigured/unreachable)
                                                    |
                                              app.py (FastAPI)
                                       /api/search, /api/search-all
                                                    |
                                       db.py (MongoDB, optional) <-- history
                                                    |
                                          single-page frontend
```

## Adding more pre-configured companies

Open `hulk_job_search.py` and add an entry to the `COMPANIES` list at the
top, following the existing pattern (platform, token/tenant, careers_url).
That's the only place you need to touch - `role_search.py` and `app.py`
pick it up automatically.

## Files

- `app.py` - FastAPI backend (search API, Google OAuth routes, history
  API) and the single-page frontend (form, results, history, theme
  toggle, auth UI), served as one HTML page with no build step.
- `db.py` - MongoDB Atlas connection and per-account history storage.
  Everything degrades gracefully to "not configured" if `MONGODB_URI`
  isn't set - no crashes, just a fallback to browser-only history.
- `role_search.py` - free-text role matching layer (the always-on regex
  pass) plus the thin wrappers that optionally apply `ai_match.py`'s
  re-ranking on top of it. Lets a visitor type any role ("product
  manager", "data analyst") instead of being limited to a fixed keyword
  list.
- `ai_match.py` - optional LLM layer: re-scores an already keyword-
  filtered shortlist for real fit, and writes a short digest each Job
  Watcher cycle. Fails open to regex-only behavior if `GEMINI_API_KEY`
  isn't set, the API errors, or the model's output can't be parsed - see
  "How AI is used" above.
- `eval_matching.py` - hand-labeled test set comparing the regex-only
  baseline against the regex+AI pipeline, with honest precision/recall/F1
  (including the recall-ceiling caveat) rather than a single example.
- `hulk_job_search.py` - unchanged scraping/company logic (imported, not
  duplicated).
- `companies_source.py` / `platform_cache.py` / `warm_cache.py` - load
  `companies.csv`, cache discovered ATS credentials to disk, and
  pre-warm that cache in one batch run.
- `architecture.svg` - data-flow diagram (see "Architecture" above).
- `PITCH_OUTLINE.md` - a suggested structure for the 5-minute pitch video.
- `requirements.txt` - pinned dependencies.
