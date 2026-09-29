"""
platform_cache.py
==================
With 300+ companies loaded from a CSV that only gives us a careers URL and
a platform *hint* ("Workday", "Greenhouse", "Own Portal"...), we need to
resolve the actual API token / Workday tenant for each one before we can
query it. That resolution step (discover_platform_from_url, in
hulk_job_search.py) costs one or more HTTP requests per company, so it's
done lazily (only when a company is actually searched) and the result is
cached here to disk - so the 2nd search for a company, or the next Job
Watcher scan, doesn't pay that cost again.

Storage: a single JSON file, keyed by lowercased company name. Safe to
delete any time (e.g. after you update companies.csv with corrected
URLs) - it will just get rebuilt lazily as companies are searched again.

    {
      "razorpay": {"platform": "greenhouse", "token": "razorpaysoftwareprivatelimited",
                    "discovered_at": "2026-09-03T12:00:00+00:00"},
      "cognizant": {"platform": "workday", "tenant": "cognizant", "wd_host": "wd1",
                     "site": "CognizantCareers", "discovered_at": "..."}
    }
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

CACHE_PATH = os.environ.get(
    "PLATFORM_CACHE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform_cache.json"),
)

# "Negative" entries (platform == "custom", or {"unresolved": True}) mean "we
# probed and found no known ATS". That verdict can go stale (company moves to
# Greenhouse/Lever, or the probe was wrong), so it expires; positive entries
# (real token/tenant) never do.
NEGATIVE_TTL_SECONDS = float(os.environ.get("PLATFORM_CACHE_NEGATIVE_TTL_HOURS", "24")) * 3600

_lock = threading.Lock()
_cache = None  # lazy-loaded dict, held in memory once loaded


def _load():
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save():
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_cache, f, indent=2, sort_keys=True)
    os.replace(tmp_path, CACHE_PATH)  # atomic on POSIX - avoids a half-written file


def _is_negative(entry):
    return bool(entry) and (entry.get("platform") == "custom" or entry.get("unresolved"))


def negative_entry_timestamp(entry):
    """Epoch seconds when a cache entry was written (falls back to 'now'
    if the timestamp is missing/unparseable, so the TTL starts fresh)."""
    try:
        return datetime.fromisoformat(entry["discovered_at"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return time.time()


def get_cached_platform(company_name):
    """Return the cached {platform, token/tenant/wd_host/site, ...} dict
    for a company, or None if we've never resolved it - or if the only
    thing we have is an expired negative ("custom") verdict."""
    with _lock:
        cache = _load()
        entry = cache.get(company_name.strip().lower())
        if _is_negative(entry) and (time.time() - negative_entry_timestamp(entry)) > NEGATIVE_TTL_SECONDS:
            return None
        return entry


def set_cached_platform(company_name, data):
    """Persist a successfully-discovered platform (data is whatever subset
    of {platform, token, tenant, wd_host, site} applies)."""
    with _lock:
        cache = _load()
        entry = dict(data)
        entry["discovered_at"] = datetime.now(timezone.utc).isoformat()
        cache[company_name.strip().lower()] = entry
        _save()


def all_cached():
    with _lock:
        return dict(_load())


def clear_cache():
    """Wipe the whole cache (e.g. after fixing a batch of bad URLs)."""
    global _cache
    with _lock:
        _cache = {}
        _save()