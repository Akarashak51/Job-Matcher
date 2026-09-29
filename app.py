"""
Hulk Job Finder - web app
==========================
Enter a company + role, get live matching job openings.
Includes: Google sign-in, light/dark theme, and local search history.

Run locally:
  pip install -r requirements.txt
  uvicorn app:app --reload
  open http://127.0.0.1:8000

Google login setup: see README.md for the Google Cloud Console steps and
which environment variables to set (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
SESSION_SECRET). Without them, the site still works fully - the "Sign in
with Google" button just shows a friendly "not configured yet" message.

Deploy: see README.md (Render free tier).
"""

import os

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field

import db
from role_search import (
    is_known_company,
    known_company_names,
    known_companies_directory,
    role_suggestions,
    search_known_company,
    search_unknown_company,
    search_all_known_companies,
    is_lookup_failure,
    company_identity,
    company_domain,
)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-change-me-in-production")
GOOGLE_CONFIGURED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

app = FastAPI(title="Job Finder")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

oauth = None
if GOOGLE_CONFIGURED:
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _https_redirect_uri(request: Request, route_name: str):
    """Build the callback URL, forcing https when behind Render's proxy
    (which terminates TLS before the app sees the request)."""
    url = request.url_for(route_name)
    if request.headers.get("x-forwarded-proto") == "https":
        url = url.replace(scheme="https")
    return url


# ---------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------

@app.get("/login")
async def login(request: Request):
    if not GOOGLE_CONFIGURED:
        return HTMLResponse(
            "<p style='font-family:sans-serif;max-width:480px;margin:60px auto;'>"
            "Google sign-in isn't configured on this deployment yet. "
            "The site owner needs to set <code>GOOGLE_CLIENT_ID</code> and "
            "<code>GOOGLE_CLIENT_SECRET</code> (see README.md). "
            "<a href='/'>&larr; Back to Job Finder</a></p>",
            status_code=503,
        )
    redirect_uri = _https_redirect_uri(request, "auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not GOOGLE_CONFIGURED:
        return RedirectResponse("/")
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.google.parse_id_token(request, token)
    request.session["user"] = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name") or userinfo.get("email"),
        "picture": userinfo.get("picture"),
    }
    return RedirectResponse("/")


@app.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/")


@app.get("/api/me")
def me(request: Request):
    return {
        "user": request.session.get("user"),
        "google_configured": GOOGLE_CONFIGURED,
        "cloud_history_configured": db.is_configured(),
    }


# ---------------------------------------------------------------------
# Cloud history API (MongoDB Atlas) - only for signed-in users, so
# history is scoped to a real account rather than "whoever has this URL".
# ---------------------------------------------------------------------

class HistoryEntryIn(BaseModel):
    company: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    maxExp: float | None = Field(default=None, ge=0, le=30)
    days: int | None = Field(default=None, ge=1, le=365)
    includeSenior: bool = False
    matchCount: int | None = Field(default=None, ge=0, le=100_000)
    platform: str | None = Field(default=None, max_length=80)
    ts: int | None = None


def _signed_in_email(request: Request):
    user = request.session.get("user")
    return user.get("email") if user else None


@app.get("/api/history")
def api_get_history(request: Request):
    email = _signed_in_email(request)
    if not email:
        return JSONResponse({"error": "not_signed_in"}, status_code=401)
    if not db.is_configured():
        return JSONResponse({"error": "not_configured"}, status_code=503)
    try:
        return {"history": db.get_history(email)}
    except db.DBError as e:
        return JSONResponse({"error": "db_error", "detail": str(e)}, status_code=502)


@app.post("/api/history")
def api_add_history(entry: HistoryEntryIn, request: Request):
    email = _signed_in_email(request)
    if not email:
        return JSONResponse({"error": "not_signed_in"}, status_code=401)
    if not db.is_configured():
        return JSONResponse({"error": "not_configured"}, status_code=503)
    try:
        return {"entry": db.add_history_entry(email, entry.model_dump())}
    except db.DBError as e:
        return JSONResponse({"error": "db_error", "detail": str(e)}, status_code=502)


@app.delete("/api/history/{entry_id}")
def api_delete_history_entry(entry_id: str, request: Request):
    email = _signed_in_email(request)
    if not email:
        return JSONResponse({"error": "not_signed_in"}, status_code=401)
    if not db.is_configured():
        return JSONResponse({"error": "not_configured"}, status_code=503)
    try:
        db.delete_history_entry(email, entry_id)
        return {"ok": True}
    except db.DBError as e:
        return JSONResponse({"error": "db_error", "detail": str(e)}, status_code=502)


@app.delete("/api/history")
def api_clear_history(request: Request):
    email = _signed_in_email(request)
    if not email:
        return JSONResponse({"error": "not_signed_in"}, status_code=401)
    if not db.is_configured():
        return JSONResponse({"error": "not_configured"}, status_code=503)
    try:
        db.clear_history(email)
        return {"ok": True}
    except db.DBError as e:
        return JSONResponse({"error": "db_error", "detail": str(e)}, status_code=502)


# ---------------------------------------------------------------------
# Job search API (unchanged)
# ---------------------------------------------------------------------

@app.get("/api/companies")
def list_companies():
    return {"companies": known_company_names()}


@app.get("/api/companies-full")
def list_companies_full():
    """Full directory with careers links + logos + whether Job Watcher
    can scan them live - used by the Profile page's company list."""
    return {"companies": known_companies_directory()}


@app.get("/api/roles")
def list_roles():
    return {"roles": role_suggestions()}


@app.get("/api/search")
def search(
    company: str = Query(..., min_length=1, max_length=120),
    role: str = Query(..., min_length=1, max_length=120),
    max_experience: float = Query(3.0, ge=0, le=30),
    include_senior: bool = Query(False),
    days: int = Query(30, ge=1, le=365),
):
    company = company.strip()
    role = role.strip()
    if not company or not role:
        raise HTTPException(status_code=422, detail="company and role cannot be blank")

    known = is_known_company(company)
    if known:
        jobs, platform_used, total_found, note = search_known_company(
            known, role, max_experience, days, include_senior
        )
        supported = True
        identity = company_identity(known)
    else:
        jobs, platform_used, total_found, note = search_unknown_company(
            company, role, max_experience, days, include_senior
        )
        supported = False
        # Best-effort identity for an unlisted company: no confirmed careers
        # URL, so leave it blank rather than guessing a wrong domain.
        identity = {"name": company, "careers_url": None, "logo_domain": None}

    return JSONResponse({
        "company": company,
        "role": role,
        "supported_company": supported,
        "platform_used": platform_used,
        "total_open_roles_seen": total_found,
        "match_count": len(jobs),
        "note": note,
        "lookup_failed": is_lookup_failure(note),
        "jobs": jobs,
        "company_careers_url": identity["careers_url"],
        "company_logo_domain": identity["logo_domain"],
    })


@app.get("/api/search-all")
def search_all(
    role: str = Query(..., min_length=1, max_length=120),
    max_experience: float = Query(3.0, ge=0, le=30),
    include_senior: bool = Query(False),
    days: int = Query(30, ge=1, le=365),
):
    """Used by Job Watcher mode: scans every configured company in
    parallel for the given role, instead of a single named company."""
    role = role.strip()
    if not role:
        raise HTTPException(status_code=422, detail="role cannot be blank")
    summary = search_all_known_companies(role, max_experience, days, include_senior)
    return JSONResponse({"role": role, **summary})


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job Finder &middot; Career Dispatch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script>
  // Apply saved/system theme immediately, before paint, to avoid a flash.
  (function () {
    var saved = localStorage.getItem('jobfinder_theme');
    var theme = saved || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
  })();
</script>
<style>
  :root {
    color-scheme: light dark;
    --bg: #F1EEE3; --card-bg: #FFFFFF; --text: #211E19; --muted: #6E6858; --border: #DEDACB;
    --accent: #A6741F; --accent-text: #FFFFFF; --recent-bg: #E7F1E1; --recent-text: #3C6B31;
    --note-bg: #FBF0DA; --note-border: #E3C688; --link: #2E5C8A;
    --danger: #9A3324; --danger-bg: #F7E6E1; --tag-bg: #EFEBDC; --tag-text: #5B5646;
    --input-border: #D3CDB9; --rule: #D8CFAE;
    --font-display: 'Source Serif 4', Georgia, serif;
    --font-mono: 'IBM Plex Mono', 'SFMono-Regular', Consolas, monospace;
    --font-body: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  html[data-theme="dark"] {
    --bg: #15161A; --card-bg: #1E2025; --text: #ECE8DC; --muted: #9C978A; --border: #33343B;
    --accent: #D9A552; --accent-text: #1B140A; --recent-bg: #1E3320; --recent-text: #8FCB86;
    --note-bg: #332B14; --note-border: #5C4A1E; --link: #8FB6E0;
    --danger: #E08670; --danger-bg: #3A1E18; --tag-bg: #2A2B30; --tag-text: #C9C5B8;
    --input-border: #3A3B41; --rule: #33343B;
  }
  * { box-sizing: border-box; }
  body {
    font-family: var(--font-body);
    max-width: 760px; margin: 0 auto; padding: 32px 18px 60px;
    line-height: 1.5; background: var(--bg); color: var(--text);
    transition: background 0.15s, color 0.15s;
  }

  /* ---------------- Masthead ---------------- */
  .masthead { display: flex; justify-content: space-between; align-items: flex-start;
              gap: 14px; padding-bottom: 18px; border-bottom: 2px solid var(--text); }
  .masthead-title { display: flex; flex-direction: column; gap: 2px; }
  .eyebrow { font-family: var(--font-mono); font-size: 0.68rem; letter-spacing: 0.14em;
             text-transform: uppercase; color: var(--accent); font-weight: 600; }
  h1.wordmark { font-family: var(--font-display); font-size: 2rem; font-weight: 700;
                margin: 0; letter-spacing: -0.01em; }
  .masthead-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; padding-top: 4px; }
  .icon-btn {
    background: var(--card-bg); border: 1px solid var(--border); color: var(--text);
    width: 36px; height: 36px; border-radius: 50%; font-size: 1rem;
    display: flex; align-items: center; justify-content: center; padding: 0; cursor: pointer;
  }
  #authSection { display: flex; align-items: center; gap: 8px; }
  .signin-btn {
    display: flex; align-items: center; gap: 8px; background: var(--card-bg);
    color: var(--text); border: 1px solid var(--border); padding: 7px 12px;
    border-radius: 6px; font-size: 0.8rem; font-weight: 600; text-decoration: none;
    cursor: pointer; font-family: var(--font-body);
  }
  .signin-btn svg { width: 15px; height: 15px; }
  .user-chip { display: flex; align-items: center; gap: 8px; }
  .user-chip img { width: 28px; height: 28px; border-radius: 50%; }
  .user-chip .user-name { font-size: 0.8rem; font-weight: 600; max-width: 100px;
                           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .signout-link { font-size: 0.74rem; color: var(--muted); text-decoration: none; }
  .signout-link:hover { text-decoration: underline; }

  /* ---------------- Primary tab nav ---------------- */
  .tab-nav { display: flex; gap: 22px; margin: 18px 0 22px; border-bottom: 1px solid var(--rule); }
  .tab-btn {
    background: transparent; border: none; padding: 9px 2px 11px; font-family: var(--font-mono);
    font-size: 0.76rem; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 600;
    color: var(--muted); cursor: pointer; position: relative; top: 1px;
    border-bottom: 2px solid transparent;
  }
  .tab-btn.active { color: var(--text); border-bottom-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

  /* ---------------- Search / Watcher mode toggle ---------------- */
  .sub { color: var(--muted); margin-bottom: 16px; font-size: 0.92rem; }
  .mode-toggle { display: flex; gap: 4px; background: var(--card-bg); border: 1px solid var(--border);
                 border-radius: 8px; padding: 4px; margin-bottom: 14px; }
  .mode-toggle button { flex: 1; background: transparent; color: var(--muted); font-weight: 600;
                         font-size: 0.85rem; padding: 9px; border-radius: 6px; cursor: pointer;
                         font-family: var(--font-body); border: none; }
  .mode-toggle button.active { background: var(--accent); color: var(--accent-text); }

  form { display: grid; gap: 12px; background: var(--card-bg); padding: 20px; border-radius: 10px;
         border: 1px solid var(--border); }
  label { font-size: 0.82rem; font-weight: 600; color: var(--text); }
  input[type=text], input[type=number], select {
    width: 100%; padding: 10px; border: 1px solid var(--input-border); border-radius: 6px;
    font-size: 1rem; box-sizing: border-box; background: var(--bg); color: var(--text);
    font-family: var(--font-body);
  }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  .checkbox-row { display: flex; align-items: center; gap: 8px; font-weight: 400; }
  button {
    padding: 12px; font-size: 0.95rem; border: none; border-radius: 6px;
    background: var(--accent); color: var(--accent-text); cursor: pointer; font-weight: 700;
    font-family: var(--font-body);
  }
  button:disabled { background: #999; cursor: not-allowed; }
  #status { margin: 16px 0; font-size: 0.85rem; color: var(--muted); min-height: 1.2em;
             font-family: var(--font-mono); }
  .note { background: var(--note-bg); border: 1px solid var(--note-border); padding: 10px 12px;
          border-radius: 6px; font-size: 0.88rem; margin-bottom: 16px; }

  /* ---------------- Job "dispatch slip" cards ---------------- */
  .job { background: var(--card-bg); border: 1px solid var(--border); border-left: 3px solid var(--accent);
         border-radius: 6px; padding: 14px 16px; margin-bottom: 10px; }
  .job h3 { margin: 0 0 4px 0; font-size: 1.02rem; font-family: var(--font-display); font-weight: 600; }
  .job a { color: var(--link); text-decoration: none; }
  .job a:hover { text-decoration: underline; }
  .meta { font-size: 0.82rem; color: var(--muted); margin-top: 4px; font-family: var(--font-mono); }
  .tag { display: inline-block; font-size: 0.72rem; padding: 2px 8px; border-radius: 999px;
         background: var(--tag-bg); color: var(--tag-text); margin-right: 6px;
         font-family: var(--font-mono); }
  .tag.recent { background: var(--recent-bg); color: var(--recent-text); }
  .tag.ai-score { background: var(--note-bg); color: var(--accent); border: 1px solid var(--note-border);
                  cursor: help; }
  .ai-reason { font-style: italic; opacity: 0.85; }
  .apply-link { font-family: var(--font-mono); font-weight: 700; font-size: 0.78rem; }
  .draft-note-wrap { margin-top: 6px; }
  .draft-toggle-btn { background: none; border: 1px solid var(--note-border); color: var(--accent);
    font-family: var(--font-mono); font-size: 0.72rem; padding: 3px 10px; border-radius: 999px;
    cursor: pointer; }
  .draft-toggle-btn:hover { background: var(--note-bg); }
  .draft-note { margin-top: 6px; padding: 10px 12px; background: var(--note-bg);
    border: 1px solid var(--note-border); border-radius: 8px; white-space: pre-wrap; }
  /* Keep a large result set usable without turning the whole dashboard
     into one very long page. The surrounding page can still scroll on
     small screens, while this list gets its own scroll area on desktop. */
  #results {
    max-height: min(68vh, 760px);
    overflow-y: auto;
    overscroll-behavior: contain;
    padding-right: 6px;
    scrollbar-gutter: stable;
  }
  #results:empty { display: none; }

  /* ---------------- Company badge (logo + name + careers link) ---------------- */
  .co-badge { display: flex; align-items: center; gap: 8px; }
  .co-logo-img { width: 22px; height: 22px; border-radius: 5px; object-fit: contain;
                 background: #fff; border: 1px solid var(--border); flex-shrink: 0; }
  .co-logo-fallback { width: 22px; height: 22px; border-radius: 5px; background: var(--accent);
                       color: var(--accent-text); display: none; align-items: center; justify-content: center;
                       font-family: var(--font-mono); font-weight: 700; font-size: 0.7rem; flex-shrink: 0; }
  .co-careers-link { font-size: 0.76rem; color: var(--link); text-decoration: none; font-family: var(--font-mono); }
  .co-careers-link:hover { text-decoration: underline; }
  .no-match-card { background: var(--card-bg); border: 1px dashed var(--border); border-radius: 8px;
                    padding: 14px 16px; margin-bottom: 12px; }

  /* ---------------- History tab ---------------- */
  .history-header { display: flex; justify-content: space-between; align-items: center;
                     margin-bottom: 4px; }
  .history-header h2 { font-size: 1.15rem; margin: 0; font-family: var(--font-display); }
  .history-header button { padding: 6px 12px; font-size: 0.78rem; background: transparent;
                            color: var(--danger); border: 1px solid var(--danger); font-weight: 600; }
  .history-header button:hover { background: var(--danger-bg); }
  .history-note { font-size: 0.76rem; color: var(--muted); margin: 0 0 14px 0; font-family: var(--font-mono); }
  #historyEmpty { color: var(--muted); font-size: 0.9rem; }
  .history-item { display: flex; justify-content: space-between; align-items: center;
                  gap: 10px; background: var(--card-bg); border: 1px solid var(--border);
                  border-left: 3px solid var(--link); border-radius: 6px; padding: 10px 14px; margin-bottom: 8px; }
  .history-info { min-width: 0; }
  .history-title { font-weight: 600; font-size: 0.95rem; overflow: hidden;
                    text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-display); }
  .history-sub { font-size: 0.78rem; color: var(--muted); margin-top: 2px; font-family: var(--font-mono); }
  .history-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .history-actions button { padding: 6px 10px; font-size: 0.78rem; font-weight: 600; }
  .rerun-btn { background: var(--accent); color: var(--accent-text); }
  .delete-btn { background: transparent; color: var(--danger); border: 1px solid var(--border); }
  .delete-btn:hover { background: var(--danger-bg); border-color: var(--danger); }

  /* ---------------- Profile tab ---------------- */
  .profile-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px;
                  padding: 18px 20px; margin-bottom: 18px; }
  .profile-card h2 { font-size: 1rem; margin: 0 0 12px 0; font-family: var(--font-mono);
                      text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .profile-account { display: flex; align-items: center; gap: 14px; }
  .profile-account img { width: 56px; height: 56px; border-radius: 50%; }
  .profile-avatar-fallback { width: 56px; height: 56px; border-radius: 50%; background: var(--accent);
                              color: var(--accent-text); display: flex; align-items: center; justify-content: center;
                              font-family: var(--font-display); font-size: 1.4rem; font-weight: 700; }
  .profile-name { font-family: var(--font-display); font-size: 1.15rem; font-weight: 600; }
  .profile-email { font-size: 0.85rem; color: var(--muted); }
  .stats-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
  .stat-box { background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
              padding: 12px 14px; }
  .stat-num { font-family: var(--font-display); font-size: 1.6rem; font-weight: 700; }
  .stat-label { font-size: 0.74rem; color: var(--muted); font-family: var(--font-mono);
                text-transform: uppercase; letter-spacing: 0.04em; margin-top: 2px; }
  .pref-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0;
              border-bottom: 1px solid var(--border); font-size: 0.9rem; }
  .pref-row:last-child { border-bottom: none; }
  .company-directory { display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
                       max-height: 460px; overflow-y: auto; overscroll-behavior: contain;
                       padding-right: 6px; scrollbar-gutter: stable; }
  .company-tile { display: flex; align-items: center; gap: 8px; background: var(--bg);
                   border: 1px solid var(--border); border-radius: 7px; padding: 8px 10px; }
  .company-tile-name { font-size: 0.85rem; font-weight: 600; overflow: hidden; text-overflow: ellipsis;
                        white-space: nowrap; }
  .company-tile-tag { font-family: var(--font-mono); font-size: 0.62rem; text-transform: uppercase;
                       letter-spacing: 0.04em; color: var(--muted); }
  .company-tile-tag.live { color: var(--recent-text); }
  .directory-filter { width: 100%; padding: 9px 10px; border: 1px solid var(--input-border);
                       border-radius: 6px; background: var(--bg); color: var(--text); margin-bottom: 12px;
                       font-family: var(--font-body); font-size: 0.9rem; }

  /* ---------------- Job Watcher status panel ---------------- */
  #watcherPanel { display: none; background: var(--card-bg); border: 1px solid var(--border);
                  border-radius: 10px; padding: 14px 16px; margin-top: 14px; }
  #watcherPanel.on { border-color: var(--recent-text); }
  .watcher-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  .watcher-status-text { font-size: 0.9rem; font-weight: 600; }
  .watcher-substatus { font-size: 0.78rem; color: var(--muted); margin-top: 3px; font-family: var(--font-mono); }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px;
         background: var(--muted); }
  .dot.live { background: var(--recent-text); box-shadow: 0 0 0 0 var(--recent-text);
              animation: pulse 1.4s infinite; }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(60,107,49,0.5); }
    70% { box-shadow: 0 0 0 8px rgba(60,107,49,0); }
    100% { box-shadow: 0 0 0 0 rgba(60,107,49,0); }
  }
  .stop-btn { background: transparent; color: var(--danger); border: 1px solid var(--danger);
              padding: 8px 14px; font-size: 0.83rem; flex-shrink: 0; }
  .stop-btn:hover { background: var(--danger-bg); }
  .company-group { margin: 18px 0 6px 0; }
  .company-group h4 { font-size: 0.95rem; margin: 0 0 8px 0; display: flex; align-items: center;
                       gap: 8px; font-family: var(--font-display); }
  .company-group .count-badge { font-size: 0.7rem; font-weight: 600; background: var(--tag-bg);
                                 color: var(--tag-text); padding: 2px 8px; border-radius: 999px;
                                 font-family: var(--font-mono); }

  @media (max-width: 480px) {
    .stats-grid, .company-directory { grid-template-columns: 1fr; }
    #results { max-height: 65vh; padding-right: 2px; }
    h1.wordmark { font-size: 1.6rem; }
  }
</style>
</head>
<body>

<div class="masthead">
  <div class="masthead-title">
    <span class="eyebrow">Career Dispatch</span>
    <h1 class="wordmark">Job Finder</h1>
  </div>
  <div class="masthead-right">
    <button id="themeToggle" class="icon-btn" type="button" title="Toggle theme">-</button>
    <div id="authSection"></div>
  </div>
</div>

<div class="tab-nav">
  <button type="button" class="tab-btn active" id="navSearch" onclick="showTab('search')">Search</button>
  <button type="button" class="tab-btn" id="navHistory" onclick="showTab('history')">History</button>
  <button type="button" class="tab-btn" id="navProfile" onclick="showTab('profile')">Profile</button>
</div>

<!-- ================= SEARCH TAB ================= -->
<div class="tab-panel active" id="tab-search">

  <p class="sub" id="modeSub">Enter a company and a role. We check their live job board and show you matching openings.</p>

  <div class="mode-toggle">
    <button type="button" id="modeSearchBtn" class="active" onclick="setMode('search')">Job Search</button>
    <button type="button" id="modeWatchBtn" onclick="setMode('watch')">Job Watcher</button>
  </div>

  <form id="searchForm">
    <div id="companyField">
      <label for="company">Company</label>
      <input type="text" id="company" list="companyList" placeholder="e.g. Razorpay, CRED, Zomato..." required>
      <datalist id="companyList"></datalist>
    </div>
    <div>
      <label for="role">Role</label>
      <input type="text" id="role" list="roleList" placeholder="e.g. backend developer, product manager, UI designer" required>
      <datalist id="roleList"></datalist>
    </div>
    <div class="row">
      <div>
        <label for="maxExp">Max experience (years)</label>
        <input type="number" id="maxExp" value="3" min="0" max="30" step="0.5">
      </div>
      <div>
        <label for="days">Recency window (days, advisory)</label>
        <input type="number" id="days" value="30" min="1" max="365">
      </div>
    </div>
    <div id="intervalField" style="display:none;">
      <label for="watchInterval">Check every</label>
      <select id="watchInterval">
        <option value="10">10 minutes</option>
        <option value="15" selected>15 minutes</option>
        <option value="20">20 minutes</option>
        <option value="30">30 minutes</option>
      </select>
    </div>
    <label class="checkbox-row"><input type="checkbox" id="includeSenior"> Include senior/lead/staff titles</label>
    <button type="submit" id="searchBtn">Search</button>
  </form>

  <div id="watcherPanel">
    <div class="watcher-row">
      <div>
        <div class="watcher-status-text"><span class="dot" id="watcherDot"></span><span id="watcherStatusText">Watcher is off</span></div>
        <div class="watcher-substatus" id="watcherSubStatus">Start it to scan every configured company on a timer.</div>
        <div class="watcher-substatus ai-reason" id="watcherDigest" style="display:none;"></div>
      </div>
      <button type="button" class="stop-btn" id="watcherStopBtn" onclick="stopWatcher()" style="display:none;">Stop watching</button>
    </div>
  </div>

  <div id="status" role="status" aria-live="polite"></div>
  <div id="results" aria-live="polite" aria-label="Job search results" tabindex="-1"></div>
</div>

<!-- ================= HISTORY TAB ================= -->
<div class="tab-panel" id="tab-history">
  <div class="history-header">
    <h2>Search History</h2>
    <button id="clearHistoryBtn" type="button">Clear all</button>
  </div>
  <p class="history-note" id="historyNote"></p>
  <div id="historyEmpty">No searches yet - your searches will show up here.</div>
  <div id="historyList"></div>
</div>

<!-- ================= PROFILE TAB ================= -->
<div class="tab-panel" id="tab-profile">

  <div class="profile-card">
    <h2>Account</h2>
    <div id="profileAccount"></div>
  </div>

  <div class="profile-card">
    <h2>Your Activity</h2>
    <div class="stats-grid" id="profileStats"></div>
  </div>

  <div class="profile-card">
    <h2>Preferences</h2>
    <div class="pref-row">
      <span>Theme</span>
      <button type="button" class="icon-btn" id="profileThemeToggle" title="Toggle theme" style="width:auto;height:auto;padding:6px 12px;border-radius:6px;">-</button>
    </div>
    <div class="pref-row">
      <span>Default max experience (years)</span>
      <input type="number" id="prefMaxExp" min="0" max="30" step="0.5" style="width:80px;">
    </div>
    <div class="pref-row">
      <span>Default recency window (days)</span>
      <input type="number" id="prefDays" min="1" max="365" style="width:80px;">
    </div>
  </div>

  <div class="profile-card">
    <h2 id="directoryHeading">Configured Companies</h2>
    <input type="text" class="directory-filter" id="directoryFilter" placeholder="Filter companies...">
    <div class="company-directory" id="companyDirectory"></div>
  </div>

</div>

<script>
// ================= Tab navigation =================
function showTab(tab) {
  ['search', 'history', 'profile'].forEach(t => {
    document.getElementById('tab-' + t).classList.toggle('active', t === tab);
    document.getElementById('nav' + t.charAt(0).toUpperCase() + t.slice(1)).classList.toggle('active', t === tab);
  });
  if (tab === 'history') renderHistory();
  if (tab === 'profile') renderProfile();
}

// ================= Theme =================
function currentTheme() {
  return document.documentElement.getAttribute('data-theme') || 'light';
}
function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('jobfinder_theme', theme);
  const icon = theme === 'dark' ? String.fromCodePoint(9728) : String.fromCodePoint(127769);
  document.getElementById('themeToggle').textContent = icon;
  const profileToggle = document.getElementById('profileThemeToggle');
  if (profileToggle) profileToggle.textContent = icon;
}
document.getElementById('themeToggle').addEventListener('click', () => {
  setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
});
setTheme(currentTheme()); // sync the button icon with whatever the inline head-script picked

// ================= Auth =================
let currentUser = null;
let cloudHistoryConfigured = false;

function renderAuth() {
  const el = document.getElementById('authSection');
  if (currentUser) {
    el.innerHTML = `
      <div class="user-chip">
        ${currentUser.picture ? `<img src="${currentUser.picture}" alt="">` : ''}
        <div>
          <div class="user-name">${currentUser.name || currentUser.email}</div>
          <a class="signout-link" href="/logout">Sign out</a>
        </div>
      </div>`;
  } else {
    el.innerHTML = `
      <a class="signin-btn" href="/login">
        <svg viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.5 6.1 29.5 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.7-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 16 19 13 24 13c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.5 6.1 29.5 4 24 4c-7.4 0-13.8 4-17.7 10.7z"/><path fill="#4CAF50" d="M24 44c5.4 0 10.3-2.1 14-5.5l-6.5-5.3C29.4 34.9 26.8 36 24 36c-5.3 0-9.7-3.4-11.3-8l-6.6 5.1C9.9 39.9 16.4 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.1-4 5.5l6.5 5.3C41.5 35.9 44 30.5 44 24c0-1.3-.1-2.7-.4-3.5z"/></svg>
        Sign in with Google
      </a>`;
  }
}

// ================= Company logo badge helper (shared) =================
function initialsOf(name) {
  return (name || '?').trim().charAt(0).toUpperCase();
}
function escapeHTML(value) {
  const el = document.createElement('div');
  el.textContent = value == null ? '' : String(value);
  return el.innerHTML;
}
function safeUrl(value) {
  try {
    const url = new URL(value, window.location.origin);
    return (url.protocol === 'https:' || url.protocol === 'http:') ? url.href : '';
  } catch (e) { return ''; }
}
function companyBadgeHTML(name, logoDomain, careersUrl, size) {
  size = size || 22;
  const safeName = escapeHTML(name);
  const safeDomain = /^[a-z0-9.-]+$/i.test(logoDomain || '') ? logoDomain : '';
  const safeCareersUrl = safeUrl(careersUrl);
  const logoHtml = logoDomain
    ? `<img class="co-logo-img" style="width:${size}px;height:${size}px;" src="https://logo.clearbit.com/${safeDomain}" alt=""
         onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
       <div class="co-logo-fallback" style="width:${size}px;height:${size}px;">${escapeHTML(initialsOf(name))}</div>`
    : `<div class="co-logo-fallback" style="display:flex;width:${size}px;height:${size}px;">${escapeHTML(initialsOf(name))}</div>`;
  const linkHtml = safeCareersUrl
    ? `<a class="co-careers-link" href="${escapeHTML(safeCareersUrl)}" target="_blank" rel="noopener">Careers page &rarr;</a>`
    : '';
  return `<div class="co-badge">${logoHtml}<div><div style="font-weight:600;">${safeName}</div>${linkHtml}</div></div>`;
}

// ================= History (cloud via MongoDB Atlas when signed in +
//                    configured; browser-only fallback otherwise) =================
const HISTORY_LIMIT = 50;
let historyList = [];

function isCloudMode() {
  return !!(currentUser && cloudHistoryConfigured);
}
function localHistoryKey() {
  return 'jobfinder_history_' + (currentUser && currentUser.email ? currentUser.email : 'guest');
}
function getLocalHistory() {
  try { return JSON.parse(localStorage.getItem(localHistoryKey())) || []; }
  catch (e) { return []; }
}
function saveLocalHistory(list) {
  localStorage.setItem(localHistoryKey(), JSON.stringify(list));
}

async function loadHistory() {
  if (isCloudMode()) {
    try {
      const resp = await fetch('/api/history');
      if (resp.ok) {
        const data = await resp.json();
        historyList = data.history || [];
        renderHistory();
        return;
      }
    } catch (e) { /* fall through to local */ }
  }
  historyList = getLocalHistory();
  renderHistory();
}

async function addHistoryEntry(entry) {
  if (isCloudMode()) {
    try {
      const resp = await fetch('/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(entry),
      });
      if (resp.ok) {
        const data = await resp.json();
        historyList.unshift(data.entry);
        renderHistory();
        return;
      }
    } catch (e) { /* fall through to local */ }
  }
  const localEntry = { ...entry, id: Date.now() };
  const list = [localEntry, ...getLocalHistory()].slice(0, HISTORY_LIMIT);
  saveLocalHistory(list);
  historyList = list;
  renderHistory();
}

async function deleteHistoryEntry(id) {
  if (isCloudMode()) {
    try {
      const resp = await fetch('/api/history/' + encodeURIComponent(id), { method: 'DELETE' });
      if (resp.ok) {
        historyList = historyList.filter(h => String(h.id) !== String(id));
        renderHistory();
        return;
      }
    } catch (e) { /* fall through to local */ }
  }
  const list = getLocalHistory().filter(h => String(h.id) !== String(id));
  saveLocalHistory(list);
  historyList = list;
  renderHistory();
}

async function clearHistory() {
  if (!confirm('Clear all search history? This cannot be undone.')) return;
  if (isCloudMode()) {
    try {
      const resp = await fetch('/api/history', { method: 'DELETE' });
      if (resp.ok) { historyList = []; renderHistory(); return; }
    } catch (e) { /* fall through to local */ }
  }
  saveLocalHistory([]);
  historyList = [];
  renderHistory();
}

function relativeTime(ts) {
  const diffSec = Math.floor((Date.now() - ts) / 1000);
  if (diffSec < 60) return 'just now';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return diffMin + 'm ago';
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return diffHr + 'h ago';
  const diffDay = Math.floor(diffHr / 24);
  return diffDay + 'd ago';
}
function historyModeNote() {
  if (isCloudMode()) {
    return 'Synced to your account (' + currentUser.email + ') - available on any device.';
  }
  if (currentUser) {
    return "Signed in as " + currentUser.email + ", but cloud history isn't set up on this deployment yet - saved in this browser only.";
  }
  return "Saved in this browser only. Sign in to sync history across devices.";
}
function renderHistory() {
  document.getElementById('historyNote').textContent = historyModeNote();

  const emptyEl = document.getElementById('historyEmpty');
  const listEl = document.getElementById('historyList');

  if (historyList.length === 0) {
    emptyEl.style.display = 'block';
    listEl.innerHTML = '';
    return;
  }
  emptyEl.style.display = 'none';

  listEl.innerHTML = historyList.map(h => `
    <div class="history-item">
      <div class="history-info">
        <div class="history-title">${escapeHTML(h.company)} &middot; ${escapeHTML(h.role)}</div>
        <div class="history-sub">${escapeHTML(h.matchCount)} match(es)${h.platform ? ' via ' + escapeHTML(h.platform) : ''} &middot; ${relativeTime(h.ts)}</div>
      </div>
      <div class="history-actions">
        <button class="rerun-btn" onclick="rerunFromHistory('${h.id}')">Re-run</button>
        <button class="delete-btn" onclick="deleteHistoryEntry('${h.id}')">Delete</button>
      </div>
    </div>
  `).join('');
}
function rerunFromHistory(id) {
  const entry = historyList.find(h => String(h.id) === String(id));
  if (!entry) return;
  showTab('search');
  document.getElementById('company').value = entry.company;
  document.getElementById('role').value = entry.role;
  document.getElementById('maxExp').value = entry.maxExp;
  document.getElementById('days').value = entry.days;
  document.getElementById('includeSenior').checked = entry.includeSenior;
  document.getElementById('searchForm').dispatchEvent(new Event('submit', { cancelable: true }));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
document.getElementById('clearHistoryBtn').addEventListener('click', clearHistory);

// ================= Profile tab =================
const PREF_KEY = 'jobfinder_prefs';
function getPrefs() {
  try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; }
  catch (e) { return {}; }
}
function savePrefs(prefs) {
  localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
}
function applyPrefsToForm() {
  const prefs = getPrefs();
  if (prefs.maxExp != null) document.getElementById('maxExp').value = prefs.maxExp;
  if (prefs.days != null) document.getElementById('days').value = prefs.days;
}
function wireProfilePrefInputs() {
  const prefs = getPrefs();
  const maxExpEl = document.getElementById('prefMaxExp');
  const daysEl = document.getElementById('prefDays');
  maxExpEl.value = prefs.maxExp != null ? prefs.maxExp : document.getElementById('maxExp').value;
  daysEl.value = prefs.days != null ? prefs.days : document.getElementById('days').value;
  maxExpEl.onchange = () => {
    const p = getPrefs(); p.maxExp = Number(maxExpEl.value); savePrefs(p);
    document.getElementById('maxExp').value = maxExpEl.value;
  };
  daysEl.onchange = () => {
    const p = getPrefs(); p.days = parseInt(daysEl.value, 10); savePrefs(p);
    document.getElementById('days').value = daysEl.value;
  };
  document.getElementById('profileThemeToggle').onclick = () => {
    setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
  };
}

function computeStats() {
  const searches = historyList.length;
  const totalMatches = historyList.reduce((sum, h) => sum + (h.matchCount || 0), 0);
  const counts = {};
  historyList.forEach(h => { counts[h.company] = (counts[h.company] || 0) + 1; });
  let topCompany = '-';
  let topCount = 0;
  Object.keys(counts).forEach(c => { if (counts[c] > topCount) { topCompany = c; topCount = counts[c]; } });
  const roleCounts = {};
  historyList.forEach(h => { roleCounts[h.role] = (roleCounts[h.role] || 0) + 1; });
  let topRole = '-';
  let topRoleCount = 0;
  Object.keys(roleCounts).forEach(r => { if (roleCounts[r] > topRoleCount) { topRole = r; topRoleCount = roleCounts[r]; } });
  return { searches, totalMatches, topCompany, topRole };
}

function renderProfile() {
  const accountEl = document.getElementById('profileAccount');
  if (currentUser) {
    accountEl.innerHTML = `
      <div class="profile-account">
        ${currentUser.picture
          ? `<img src="${currentUser.picture}" alt="">`
          : `<div class="profile-avatar-fallback">${initialsOf(currentUser.name || currentUser.email)}</div>`}
        <div>
          <div class="profile-name">${currentUser.name || 'Signed in'}</div>
          <div class="profile-email">${currentUser.email}</div>
          <a class="signout-link" href="/logout">Sign out</a>
        </div>
      </div>`;
  } else {
    accountEl.innerHTML = `
      <p style="margin:0 0 12px 0; color:var(--muted); font-size:0.9rem;">
        You're browsing as a guest - history is saved in this browser only.
      </p>
      <a class="signin-btn" href="/login">
        <svg viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.5 6.1 29.5 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.7-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 16 19 13 24 13c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.5 6.1 29.5 4 24 4c-7.4 0-13.8 4-17.7 10.7z"/><path fill="#4CAF50" d="M24 44c5.4 0 10.3-2.1 14-5.5l-6.5-5.3C29.4 34.9 26.8 36 24 36c-5.3 0-9.7-3.4-11.3-8l-6.6 5.1C9.9 39.9 16.4 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.1-4 5.5l6.5 5.3C41.5 35.9 44 30.5 44 24c0-1.3-.1-2.7-.4-3.5z"/></svg>
        Sign in with Google
      </a>`;
  }

  const stats = computeStats();
  document.getElementById('profileStats').innerHTML = `
    <div class="stat-box"><div class="stat-num">${stats.searches}</div><div class="stat-label">Searches run</div></div>
    <div class="stat-box"><div class="stat-num">${stats.totalMatches}</div><div class="stat-label">Total matches found</div></div>
    <div class="stat-box"><div class="stat-num" style="font-size:1.1rem;">${stats.topCompany}</div><div class="stat-label">Most searched company</div></div>
    <div class="stat-box"><div class="stat-num" style="font-size:1.1rem;">${stats.topRole}</div><div class="stat-label">Most searched role</div></div>
  `;

  wireProfilePrefInputs();
  renderCompanyDirectory();
}

let companyDirectoryData = [];
async function loadCompanyDirectory() {
  try {
    const resp = await fetch('/api/companies-full');
    const data = await resp.json();
    companyDirectoryData = data.companies || [];
  } catch (e) { companyDirectoryData = []; }
}
function renderCompanyDirectory(filterText) {
  filterText = (filterText || document.getElementById('directoryFilter').value || '').toLowerCase();
  const filtered = companyDirectoryData.filter(c => c.name.toLowerCase().includes(filterText));
  document.getElementById('directoryHeading').textContent =
    'Configured Companies (' + companyDirectoryData.length + ')';
  document.getElementById('companyDirectory').innerHTML = filtered.map(c => `
    <div class="company-tile">
      ${c.logo_domain
        ? `<img class="co-logo-img" style="width:20px;height:20px;" src="https://logo.clearbit.com/${c.logo_domain}" alt=""
             onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
           <div class="co-logo-fallback" style="width:20px;height:20px;">${initialsOf(c.name)}</div>`
        : `<div class="co-logo-fallback" style="display:flex;width:20px;height:20px;">${initialsOf(c.name)}</div>`}
      <div style="min-width:0;">
        <div class="company-tile-name">${c.name}</div>
        <div class="company-tile-tag ${c.live_searchable ? 'live' : ''}">${c.live_searchable ? 'Live scan' : 'Manual only'}</div>
      </div>
    </div>
  `).join('');
}
document.getElementById('directoryFilter').addEventListener('input', (e) => renderCompanyDirectory(e.target.value));

// ================= Role suggestions datalist =================
async function loadRoleSuggestions() {
  try {
    const resp = await fetch('/api/roles');
    const data = await resp.json();
    document.getElementById('roleList').innerHTML =
      (data.roles || []).map(r => `<option value="${r}">`).join('');
  } catch (e) { /* non-critical */ }
}

// ================= Mode toggle (Job Search / Job Watcher) =================
let currentMode = 'search'; // 'search' | 'watch'

function setMode(mode) {
  if (mode === currentMode) return;
  if (currentMode === 'watch' && watcherActive) stopWatcher();

  currentMode = mode;
  const isWatch = mode === 'watch';

  document.getElementById('modeSearchBtn').classList.toggle('active', !isWatch);
  document.getElementById('modeWatchBtn').classList.toggle('active', isWatch);
  document.getElementById('companyField').style.display = isWatch ? 'none' : 'block';
  document.getElementById('company').required = !isWatch;
  document.getElementById('intervalField').style.display = isWatch ? 'block' : 'none';
  document.getElementById('watcherPanel').style.display = isWatch ? 'block' : 'none';
  document.getElementById('searchBtn').textContent = isWatch
    ? (watcherActive ? 'Update watcher' : 'Start watching')
    : 'Search';

  document.getElementById('modeSub').textContent = isWatch
    ? "Give us a role. We will scan every configured company's live job board on a repeating timer and show you matches as soon as we find them."
    : 'Enter a company and a role. We check their live job board and show you matching openings.';

  document.getElementById('status').textContent = '';
  document.getElementById('results').innerHTML = '';
}

// ================= Shared job-card rendering =================
function jobCardHTML(job) {
  const tagClass = (job.date_bucket || '').startsWith('recent') ? 'tag recent' : 'tag';
  const link = safeUrl(job.link);
  const title = escapeHTML(job.title);
  // ai_score/ai_reason are only present when GEMINI_API_KEY is
  // configured server-side (see ai_match.py) - absent otherwise, so this
  // renders nothing extra and the card looks exactly as it always did.
  const hasAiScore = typeof job.ai_score === 'number';
  const aiBadge = hasAiScore
    ? `<span class="tag ai-score" title="${escapeHTML(job.ai_reason || '')}">AI fit: ${job.ai_score}/100</span>`
    : '';
  // draft_note is only present when ai_match.draft_outreach_note() ran for
  // this job (top matches, ai_score >= 80, capped per search - see
  // role_search._apply_ai_drafts). Absent otherwise; card renders exactly
  // as before when it's missing.
  const cardId = 'job-' + Math.random().toString(36).slice(2, 10);
  const hasDraft = typeof job.draft_note === 'string' && job.draft_note.length > 0;
  const draftBlock = hasDraft ? `
      <div class="draft-note-wrap">
        <button type="button" class="draft-toggle-btn" onclick="
          const el = document.getElementById('${cardId}');
          el.style.display = el.style.display === 'none' ? 'block' : 'none';
        ">Draft outreach note &darr;</button>
        <div class="meta draft-note" id="${cardId}" style="display:none">${escapeHTML(job.draft_note)}</div>
      </div>` : '';
  return `
    <div class="job">
      <h3>${link ? `<a href="${escapeHTML(link)}" target="_blank" rel="noopener">${title}</a>` : title}</h3>
      <div class="meta">
        ${job.location ? escapeHTML(job.location) + ' &middot; ' : ''}${escapeHTML(job.experience_note || '')}
      </div>
      <div class="meta">
        <span class="${tagClass}">${escapeHTML(job.date_bucket || 'date unknown')}</span>
        ${aiBadge}
        ${link ? `<span class="apply-link"><a href="${escapeHTML(link)}" target="_blank" rel="noopener">Apply &rarr;</a></span>` : ''}
      </div>
      ${hasAiScore && job.ai_reason ? `<div class="meta ai-reason">${escapeHTML(job.ai_reason)}</div>` : ''}
      ${draftBlock}
    </div>`;
}

// ================= Job Search (single company) =================
const SEARCH_PROGRESS_STEPS = [
  'Opening the {company} careers page...',
  'Reading open roles...',
  'Matching against "{role}" and your experience filter...',
  'Wrapping up...'
];

document.getElementById('searchForm').addEventListener('submit', (e) => {
  e.preventDefault();
  if (currentMode === 'watch') {
    if (watcherActive) { stopWatcher(); startWatcher(); } // "Update watcher" restarts with new settings
    else startWatcher();
    return;
  }
  runSingleSearch();
});

async function runSingleSearch() {
  const company = document.getElementById('company').value.trim();
  const role = document.getElementById('role').value.trim();
  const maxExp = document.getElementById('maxExp').value;
  const days = document.getElementById('days').value;
  const includeSenior = document.getElementById('includeSenior').checked;

  const statusEl = document.getElementById('status');
  const resultsEl = document.getElementById('results');
  const btn = document.getElementById('searchBtn');

  btn.disabled = true;
  resultsEl.innerHTML = '';

  // Visible, real-time progress so it's obvious a search is actually happening.
  let stepIdx = 0;
  const startedAt = Date.now();
  const tick = () => {
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
    const step = SEARCH_PROGRESS_STEPS[stepIdx % SEARCH_PROGRESS_STEPS.length]
      .replace('{company}', company).replace('{role}', role);
    statusEl.textContent = step + '  (' + elapsed + 's)';
    stepIdx++;
  };
  tick();
  const progressTimer = setInterval(tick, 900);

  try {
    const params = new URLSearchParams({
      company, role, max_experience: maxExp, days, include_senior: includeSenior
    });
    const resp = await fetch('/api/search?' + params.toString());
    const data = await resp.json();
    clearInterval(progressTimer);

    let html = '';
    if (!data.supported_company) {
      html += '<div class="note">"' + escapeHTML(data.company) + '" is not one of our pre-configured companies. ' +
              'We tried to auto-detect their job board' +
              (data.platform_used ? ' and found them on <b>' + escapeHTML(data.platform_used) + '</b>.' : ', but had no luck.') +
              '</div>';
    }
    if (data.note) {
      html += '<div class="note">' + escapeHTML(data.note) + '</div>';
    }

    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
    if (data.match_count > 0) {
      statusEl.textContent = 'MATCH: ' + data.match_count + ' role(s) found' +
        (data.platform_used ? ' via ' + data.platform_used : '') + '. (' + elapsed + 's)';
    } else if (data.lookup_failed) {
      // Not a real "no match": the job board could not be reached/read.
      statusEl.textContent = 'COULD NOT CHECK ' + company + ' - job board unreachable, try again. (' + elapsed + 's)';
    } else {
      statusEl.textContent = 'NO MATCH for "' + role + '" at ' + company +
        (data.platform_used ? ' (checked via ' + data.platform_used + ')' : '') + '. (' + elapsed + 's)';
    }

    // Always show who we checked, win or lose - a company badge with a
    // direct careers link, so a miss still leaves you one click from
    // checking it yourself.
    html += `<div class="no-match-card">${companyBadgeHTML(data.company, data.company_logo_domain, data.company_careers_url, 26)}</div>`;

    data.jobs.forEach(job => { html += jobCardHTML(job); });
    resultsEl.innerHTML = html;

    await addHistoryEntry({
      ts: Date.now(),
      company, role,
      maxExp: Number(maxExp),
      days: parseInt(days, 10),
      includeSenior,
      matchCount: data.match_count,
      platform: data.platform_used,
    });
  } catch (err) {
    clearInterval(progressTimer);
    statusEl.textContent = 'Something went wrong: ' + err;
  } finally {
    btn.disabled = false;
  }
}

// ================= Job Watcher (all companies, on a timer) =================
let watcherActive = false;
let watcherIntervalId = null;
let watcherCountdownId = null;
let watcherNextCheckAt = 0;
let watcherCyclesRun = 0;

function fmtClock(totalSeconds) {
  totalSeconds = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function startWatcher() {
  const role = document.getElementById('role').value.trim();
  if (!role) {
    document.getElementById('role').focus();
    document.getElementById('status').textContent = 'Enter a role to watch first.';
    return;
  }
  watcherActive = true;
  watcherCyclesRun = 0;
  document.getElementById('watcherPanel').classList.add('on');
  document.getElementById('watcherDot').classList.add('live');
  document.getElementById('watcherStopBtn').style.display = 'inline-block';
  document.getElementById('searchBtn').textContent = 'Update watcher';
  document.getElementById('status').textContent = '';

  runWatcherScan(); // scans immediately; each scan's own `finally` schedules the next one
}

function scheduleNextCycle() {
  // Uses setTimeout (not setInterval) so the next scan is scheduled *after*
  // the current one finishes - scans never overlap even if a scan itself
  // takes a while, and the interval the user picked is honored as a gap
  // between scans rather than a fixed clock tick.
  const minutes = parseInt(document.getElementById('watchInterval').value, 10) || 15;
  const intervalMs = minutes * 60 * 1000;
  clearTimeout(watcherIntervalId);
  clearInterval(watcherCountdownId);
  watcherNextCheckAt = Date.now() + intervalMs;

  watcherIntervalId = setTimeout(runWatcherScan, intervalMs);
  watcherCountdownId = setInterval(updateWatcherCountdown, 1000);
  updateWatcherCountdown();
}

function updateWatcherCountdown() {
  if (!watcherActive) return;
  const remaining = (watcherNextCheckAt - Date.now()) / 1000;
  document.getElementById('watcherSubStatus').textContent =
    'Next check in ' + fmtClock(remaining) + ' \u00b7 ' + watcherCyclesRun + ' scan(s) run so far.';
}

async function runWatcherScan() {
  const role = document.getElementById('role').value.trim();
  const maxExp = document.getElementById('maxExp').value;
  const days = document.getElementById('days').value;
  const includeSenior = document.getElementById('includeSenior').checked;
  const minutes = document.getElementById('watchInterval').value;

  const statusText = document.getElementById('watcherStatusText');
  const subStatus = document.getElementById('watcherSubStatus');
  const resultsEl = document.getElementById('results');
  const statusEl = document.getElementById('status');

  statusText.textContent = 'Scanning all companies for "' + role + '"...';
  subStatus.textContent = 'This runs across every configured company in parallel, so it stays fast.';

  const startedAt = Date.now();
  try {
    const params = new URLSearchParams({
      role, max_experience: maxExp, days, include_senior: includeSenior
    });
    const resp = await fetch('/api/search-all?' + params.toString());
    const data = await resp.json();
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
    watcherCyclesRun++;

    if (!watcherActive) return; // stopped while the request was in flight

    const unreachable = data.companies_unreachable || 0;
    if (data.total_matches > 0) {
      statusText.textContent = 'MATCH: ' + data.total_matches + ' role(s) found across ' +
        data.companies_with_matches + ' compan' + (data.companies_with_matches === 1 ? 'y' : 'ies') + '.';
    } else if (unreachable > data.companies_scanned / 2) {
      // Most boards didn't answer - this is a connectivity/blocking problem, not "no jobs".
      statusText.textContent = 'SCAN INCOMPLETE: ' + unreachable + ' of ' + data.companies_scanned +
        ' job boards could not be reached. Will retry next cycle.';
    } else {
      statusText.textContent = 'NO MATCH for "' + role + '" in any company this scan.';
    }
    statusEl.textContent = 'Scanned ' + data.companies_scanned + ' compan' +
      (data.companies_scanned === 1 ? 'y' : 'ies') +
      (unreachable ? ' (' + unreachable + ' could not be reached)' : '') +
      (data.companies_skipped.length ? ' (' + data.companies_skipped.length + ' skipped - no live job-board API)' : '') +
      ' in ' + elapsed + 's. Watching every ' + minutes + ' min.';

    // ai_digest is only present when GEMINI_API_KEY is configured
    // server-side (see ai_match.summarize_watcher_cycle) - hidden otherwise.
    const digestEl = document.getElementById('watcherDigest');
    if (data.ai_digest) {
      digestEl.textContent = '\u2728 ' + data.ai_digest;
      digestEl.style.display = 'block';
    } else {
      digestEl.style.display = 'none';
    }

    let html = '';
    if (data.matched.length === 0) {
      html = '<div class="note">No role matched "' + role + '" in any configured company yet. ' +
             'We will check again automatically.</div>';
    } else {
      data.matched.forEach(companyResult => {
        html += `<div class="company-group">
          <h4>${companyBadgeHTML(companyResult.company, companyResult.logo_domain, companyResult.careers_url, 20)}
              <span class="count-badge">${companyResult.match_count} match(es)${companyResult.platform_used ? ' &middot; ' + companyResult.platform_used : ''}</span></h4>`;
        companyResult.jobs.forEach(job => { html += jobCardHTML(job); });
        html += '</div>';
      });
    }
    resultsEl.innerHTML = html;

    if (data.total_matches > 0) {
      await addHistoryEntry({
        ts: Date.now(),
        company: 'Watcher (all companies)',
        role,
        maxExp: Number(maxExp),
        days: parseInt(days, 10),
        includeSenior,
        matchCount: data.total_matches,
        platform: data.companies_with_matches + ' companies',
      });
    }
  } catch (err) {
    if (!watcherActive) return;
    statusText.textContent = 'Scan failed: ' + err;
  } finally {
    if (watcherActive) scheduleNextCycle();
  }
}

function stopWatcher() {
  watcherActive = false;
  clearTimeout(watcherIntervalId);
  clearInterval(watcherCountdownId);
  watcherIntervalId = null;
  watcherCountdownId = null;
  document.getElementById('watcherPanel').classList.remove('on');
  document.getElementById('watcherDot').classList.remove('live');
  document.getElementById('watcherStopBtn').style.display = 'none';
  document.getElementById('watcherStatusText').textContent = 'Watcher is off';
  document.getElementById('watcherSubStatus').textContent = 'Start it to scan every configured company on a timer.';
  document.getElementById('watcherDigest').style.display = 'none';
  document.getElementById('searchBtn').textContent = 'Start watching';
}

// ================= Init =================
(async function init() {
  applyPrefsToForm();
  loadRoleSuggestions();
  loadCompanyDirectory();

  try {
    const resp = await fetch('/api/companies');
    const data = await resp.json();
    document.getElementById('companyList').innerHTML =
      (data.companies || []).map(c => `<option value="${c}">`).join('');
  } catch (e) { /* non-critical */ }

  try {
    const resp = await fetch('/api/me');
    const data = await resp.json();
    currentUser = data.user;
    cloudHistoryConfigured = data.cloud_history_configured;
  } catch (e) { /* stay signed out */ }
  renderAuth();
  await loadHistory();
})();
</script>

</body>
</html>
"""