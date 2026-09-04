# Job Finder

## Razorpay Hackathon Submission

**Job Finder** is an AI-assisted job discovery product for candidates who are tired of opening dozens of career pages and still missing relevant opportunities.

The product combines a deterministic job-search pipeline with an optional Gemini reasoning layer. A candidate can search for a role at one company, or turn on **Job Watcher** to scan the configured company directory in parallel. Gemini improves the quality of the shortlist, explains why a result is relevant, summarizes a scan cycle, and can draft a short outreach note for the strongest matches.

Razorpay is included as a pre-configured company in the directory, alongside hundreds of other companies. The app is independent of Razorpay and uses publicly available careers pages and job-board endpoints.

## The Problem

Job seekers face three practical problems:

- Job openings are spread across different ATS platforms and company websites.
- Keyword matching produces false positives. For example, a search for `product manager` can incorrectly return `Manager, Product Photography Studio`.
- A candidate can miss a relevant opening because the title uses a synonym such as `software engineer` instead of `developer`.

Job Finder addresses these problems with a layered workflow:

1. Discover jobs from supported ATS platforms or a public careers page.
2. Normalize common role synonyms and title variations.
3. Filter by role, seniority, experience, and posting age.
4. Optionally ask Gemini to judge fit on the already-filtered shortlist.
5. Present direct application links, fit scores, reasons, and useful follow-up actions.

## Why This Fits the Hackathon

The project demonstrates a practical AI workflow rather than using an LLM as a decorative chatbot:

- **Real user value:** less manual searching and fewer irrelevant results.
- **Agentic behavior:** Job Watcher scans many sources, then Gemini reasons over the pattern of matches and creates a digest.
- **Bounded autonomy:** outreach drafts are generated only for strong matches, are capped per search, and are never sent automatically.
- **Graceful degradation:** the core product works without Gemini, Google OAuth, or MongoDB.
- **Transparent evaluation:** the repository includes a labeled evaluation script comparing the deterministic baseline with the Gemini-enhanced pipeline.
- **Production-minded behavior:** timeouts, malformed responses, unavailable platforms, and partial failures become clear fallback messages instead of application crashes.

## Main Features

### Search one company

Enter a company and a free-text role such as:

- `backend developer`
- `product manager`
- `data analyst`
- `UI/UX designer`

The result includes the job title, location when available, posting age, experience information, source platform, and a direct application link.

### Job Watcher

Job Watcher scans the CSV-driven company directory in parallel. It returns:

- companies scanned and companies skipped,
- companies with matching roles,
- total matches,
- matching jobs grouped by company,
- an optional Gemini-written cycle digest.

The watcher intentionally does not make one Gemini call per company. It scans with the deterministic matcher first, then applies AI once to the small matched shortlist. This keeps latency and API usage bounded.

### Gemini fit scoring

When `GEMINI_API_KEY` is configured, Gemini scores each already-matched listing from 0 to 100 and supplies a short reason. Results are sorted by score.

Gemini is not the first or only filter. It cannot restore a listing that the deterministic pass excluded. This design keeps prompts smaller, limits cost, and ensures the product remains useful when the API is unavailable.

### Outreach note drafts

For the strongest AI-ranked results, the app can draft a short recruiter or hiring-manager outreach note. Drafting is bounded to a maximum of three attempts per search and the text is only displayed to the user. Nothing is sent automatically.

### Authentication and history

- Google sign-in is optional.
- Without MongoDB, history is stored in browser `localStorage`.
- With Google sign-in and MongoDB Atlas, the most recent 50 searches are stored per account and available across devices.
- Light/dark theme preference is stored in the browser.

## Quick Start

The app is a Python FastAPI service and has no frontend build step.

### Requirements

- Python 3.10 or newer is recommended.
- Internet access is needed for live careers pages and optional Gemini calls.
- API keys are optional for local exploration.

### Install and run

Windows PowerShell:

```powershell
cd "Job"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

For macOS or Linux, replace the activation command with:

```bash
source .venv/bin/activate
```

The basic search experience works immediately. Optional services are configured below.

## Gemini Setup

1. Create a key at [Google AI Studio](https://aistudio.google.com/apikey).
2. Set `GEMINI_API_KEY` in the environment where the app runs.
3. Optionally set `GEMINI_MODEL`; the code default is `gemini-2.5-flash`.
4. Restart the server.

PowerShell example:

```powershell
$env:GEMINI_API_KEY = "your-key"
$env:GEMINI_MODEL = "gemini-2.5-flash"
uvicorn app:app --reload
```

Gemini is used for three bounded tasks:

| Task | Input | Output |
| --- | --- | --- |
| Fit scoring | Keyword-matched shortlist | 0-100 score and a short reason per listing |
| Watcher digest | Matches from one scan cycle | Two to four sentence summary |
| Outreach draft | Strong match, role, and company | Short draft message, never auto-sent |

Every Gemini request has a timeout. Missing keys, HTTP errors, timeouts, rate limits, invalid JSON, or empty responses return control to the deterministic result. A Gemini failure therefore reduces enrichment but does not break a search.

## Evaluation

The repository includes `eval_matching.py`, a small hand-labeled test set designed around realistic role-title traps and synonym gaps. It compares:

1. **Regex-only baseline:** the deterministic pipeline used by the application.
2. **Regex plus AI:** the same shortlist after Gemini scoring and a score threshold.

Run the baseline without any API key:

```powershell
python eval_matching.py
```

Run the full comparison with Gemini enabled:

```powershell
$env:GEMINI_API_KEY = "your-key"
python eval_matching.py
```

The script prints true positives, false positives, false negatives, precision, recall, and F1 for each query and for the combined test set.

### How to interpret the results

The evaluation deliberately documents a limitation: AI only sees listings that pass the deterministic shortlist. It can remove false positives, but it cannot recover a true match excluded by the first pass. Therefore:

- precision is the primary metric for the Gemini enhancement,
- recall remains bounded by the deterministic matcher,
- results from a live Gemini run should be quoted instead of hard-coded numbers in a pitch.

This makes the evaluation reproducible and honest: the test set, labels, scoring threshold, and limitation are all visible in the repository.

## Company and Platform Coverage

The default directory is loaded from `companies.csv`. The application supports:

- Greenhouse
- Lever
- SmartRecruiters
- Ashby
- Workday
- generic public HTML career pages as a fallback

The CSV has four columns:

```csv
company_name,country,careers_url,platform_hint
Razorpay,India,https://razorpay.com/careers,Greenhouse
```

The platform hint is only a starting point. The app resolves the actual public ATS details and stores them in `platform_cache.json`. Restart the app after editing the CSV.

### Cache warming

To resolve companies before a large Job Watcher run:

```powershell
python warm_cache.py
python warm_cache.py --limit 20
python warm_cache.py --workday-only
```

Delete an entry from `platform_cache.json`, or delete the file, when a careers URL changes and a company needs to be resolved again.

### Coverage limitations

Some career pages render job listings only in the browser with JavaScript. A plain HTML scraper cannot see those listings. When platform discovery and fallback scraping both fail, the API returns a clear note and the careers-page link rather than pretending there are no jobs.

Unknown companies are handled on a best-effort basis using common ATS slug variants. They are not guaranteed to work, especially for Workday and custom portals.

## Optional MongoDB Atlas History

Set `MONGODB_URI` to enable cloud history. The app creates the database collection and index automatically on first write.

For a free Atlas setup:

1. Create an Atlas account and an M0 free cluster.
2. Create a database user with a strong password.
3. Add the deployment network to Network Access. Render's free tier commonly requires `0.0.0.0/0`; use a strong database password.
4. Copy the Drivers connection string.
5. Set `MONGODB_URI` and optionally `MONGODB_DB_NAME`.

Example:

```powershell
$env:MONGODB_URI = "mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority"
$env:MONGODB_DB_NAME = "jobfinder"
```

If MongoDB is not configured, the rest of the product continues to work and history remains browser-only.

## Optional Google Sign-In

1. Create an OAuth client in Google Cloud Console.
2. Choose **Web application**.
3. Add `http://127.0.0.1:8000/auth/callback` as a local redirect URI.
4. For production, add `https://YOUR_DOMAIN/auth/callback`.
5. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and a long random `SESSION_SECRET`.

Without these variables, the sign-in control reports that authentication is not configured; it does not prevent job searching.

## Deployment on Render

Create a Render Web Service connected to this repository with:

| Setting | Value |
| --- | --- |
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app:app --host 0.0.0.0 --port $PORT` |

Add the environment variables needed for the features you want:

```text
SESSION_SECRET
GEMINI_API_KEY
GEMINI_MODEL
MONGODB_URI
MONGODB_DB_NAME
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
```

After deployment, add the exact Render URL plus `/auth/callback` to the Google OAuth redirect list. Render's free service may sleep after inactivity, so the first request after a quiet period can take longer.

## Architecture

```text
companies.csv
    |
companies_source.py --> hulk_job_search.py --> platform_cache.json
                                |
                         role_search.py
                    deterministic role matching
                                |
                         ai_match.py
                 optional Gemini ranking and actions
                                |
                             app.py
                 FastAPI API and single-page frontend
                         |                |
                 Google OAuth       db.py / MongoDB
```

Important boundaries:

- `hulk_job_search.py` retrieves and normalizes data from job platforms.
- `role_search.py` performs role normalization, seniority filtering, experience filtering, and date filtering.
- `ai_match.py` enriches an existing shortlist and fails open.
- `app.py` exposes the web page and API endpoints.
- `db.py` stores optional per-user history.

## API Surface

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Serves the single-page application |
| `GET /api/search` | Searches one company and role |
| `GET /api/search-all` | Runs Job Watcher across configured companies |
| `GET /api/companies` | Lists company names |
| `GET /api/companies-full` | Lists company metadata and careers links |
| `GET /api/roles` | Lists role suggestions |
| `GET /api/me` | Reports sign-in and cloud-history status |
| `GET/POST/DELETE /api/history` | Reads, writes, and clears signed-in history |

Example request:

```text
/api/search?company=Razorpay&role=backend%20developer&max_experience=3&days=30
```

The response includes `match_count`, `total_open_roles_seen`, `platform_used`, `note`, and a `jobs` array. AI-enriched jobs may also include `ai_score`, `ai_reason`, and `draft_note`.

## Repository Guide

- `app.py` - FastAPI routes and the single-page web interface.
- `role_search.py` - free-text role matching, filters, AI integration, and Job Watcher.
- `ai_match.py` - Gemini calls, response parsing, scoring, digest, and outreach drafts.
- `hulk_job_search.py` - platform adapters, discovery, scraping, and company data.
- `companies.csv` - default company directory.
- `companies_source.py` - company source loading and fallback data.
- `platform_cache.py` - cached platform resolution.
- `warm_cache.py` - batch cache warming utility.
- `db.py` - optional MongoDB Atlas history.
- `eval_matching.py` - labeled baseline-versus-AI evaluation.
- `architecture.svg` - visual data-flow diagram.
- `.env.example` - environment variable template.
- `requirements.txt` - pinned Python dependencies.

## Security and Privacy Notes

- Never commit `.env`, API keys, OAuth secrets, or database passwords.
- Gemini receives the role query and limited job-listing fields needed for scoring; it is not given the user's Google password.
- Outreach drafts are not sent automatically.
- MongoDB history is scoped to the signed-in Google email.
- Use a strong `SESSION_SECRET` and a strong MongoDB password in production.

## Troubleshooting

**The app starts but Gemini scores do not appear**

Check that `GEMINI_API_KEY` is set in the same terminal or deployment environment that starts Uvicorn. Invalid keys, rate limits, timeouts, and malformed responses intentionally fall back to deterministic results.

**A company returns no jobs**

Check the `note` field and open the returned careers URL. The portal may be JavaScript-rendered, may have changed its ATS, or may not publish matching roles in the selected date or experience range. Run `warm_cache.py` after correcting a URL.

**Google login fails after deployment**

Make sure the production callback URL exactly matches the deployed domain and that `SESSION_SECRET`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET` are present.

**History is not syncing**

Cloud history requires both a signed-in user and `MONGODB_URI`. Without either one, browser-only history is the expected behavior.

## License

See [LICENSE](LICENSE).
