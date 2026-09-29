"""
Offline regression tests for the "search/watcher suddenly returns nothing"
bug: an inconclusive network failure must never be cached as "custom site".

Run:  python -m unittest test_resolve_cache -v
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import requests

_TMP = tempfile.mkdtemp()
os.environ["PLATFORM_CACHE_PATH"] = os.path.join(_TMP, "platform_cache.json")
os.environ.pop("GEMINI_API_KEY", None)

import platform_cache  # noqa: E402
import hulk_job_search as h  # noqa: E402
import role_search as rs  # noqa: E402


class FakeResp:
    def __init__(self, status=200, payload=None, text="", url="https://x"):
        self.status_code, self._payload, self.text, self.url = status, payload, text, url
        self.ok = status < 400

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


GH_JOBS = {"jobs": [
    {"title": "Backend Engineer", "location": {"name": "Bengaluru"},
     "updated_at": "2026-09-20T00:00:00Z", "absolute_url": "https://x/1", "content": ""},
    {"title": "Marketing Lead", "location": {"name": "Delhi"},
     "updated_at": "2026-09-20T00:00:00Z", "absolute_url": "https://x/2", "content": ""},
]}


def fake_factory(mode):
    """mode: 'blocked' (403 everywhere) | 'down' (ConnectionError) | 'clean404' | 'ok'"""
    def fake(method, url, **kw):
        if mode == "down":
            raise requests.ConnectionError("network down")
        if mode == "blocked":
            return FakeResp(403, text="blocked")
        if mode == "clean404":
            return FakeResp(404, text="not found")
        if mode == "ok":
            if "boards-api.greenhouse.io/v1/boards/razorpay/jobs" in url:
                return FakeResp(200, GH_JOBS)
            return FakeResp(404)
        raise AssertionError(mode)
    return fake


def fresh_company():
    c = dict(next(x for x in h.load_companies() if x["name"] == "Razorpay"))
    c["token"] = None
    c["platform"] = "greenhouse"
    for k in ("_custom_confirmed", "_negative_at", "_lookup_unreliable"):
        c.pop(k, None)
    return c


def reset_cache():
    platform_cache._cache = {}
    if os.path.exists(platform_cache.CACHE_PATH):
        os.remove(platform_cache.CACHE_PATH)


def search(company):
    return rs.search_known_company(company, "backend developer", 3, 30, False, apply_ai=False)


class ResolveCacheTests(unittest.TestCase):
    def setUp(self):
        reset_cache()

    def _assert_nothing_cached(self):
        self.assertEqual(platform_cache.all_cached(), {})

    def test_blocked_403_is_not_cached_and_reports_lookup_failed(self):
        c = fresh_company()
        with mock.patch.object(h, "_bounded_request", fake_factory("blocked")):
            jobs, used, total, note = search(c)
        self.assertEqual(jobs, [])
        self.assertTrue(rs.is_lookup_failure(note), note)
        self._assert_nothing_cached()
        self.assertNotEqual(c["platform"], "custom")          # hint preserved
        self.assertFalse(c.get("_custom_confirmed"))

    def test_network_down_is_not_cached(self):
        c = fresh_company()
        with mock.patch.object(h, "_bounded_request", fake_factory("down")):
            jobs, used, total, note = search(c)
        self.assertTrue(rs.is_lookup_failure(note))
        self._assert_nothing_cached()

    def test_recovers_on_next_search_after_transient_failure(self):
        c = fresh_company()
        with mock.patch.object(h, "_bounded_request", fake_factory("blocked")):
            search(c)
        with mock.patch.object(h, "_bounded_request", fake_factory("ok")):
            jobs, used, total, note = search(c)
        self.assertEqual([j["title"] for j in jobs], ["Backend Engineer"])
        self.assertEqual(used, "greenhouse")
        self.assertEqual(platform_cache.get_cached_platform("Razorpay")["token"], "razorpay")

    def test_genuine_clean_404_is_still_cached_as_custom(self):
        c = fresh_company()
        with mock.patch.object(h, "_bounded_request", fake_factory("clean404")):
            h.resolve_company_platform(c)
        self.assertEqual(c["platform"], "custom")
        self.assertEqual(platform_cache.get_cached_platform("Razorpay")["platform"], "custom")

    def test_expired_negative_is_reprobed_in_process(self):
        c = fresh_company()
        with mock.patch.object(h, "_bounded_request", fake_factory("clean404")):
            h.resolve_company_platform(c)
        self.assertEqual(c["platform"], "custom")
        c["_negative_at"] = time.time() - platform_cache.NEGATIVE_TTL_SECONDS - 60
        with mock.patch.object(h, "_bounded_request", fake_factory("ok")):
            h.resolve_company_platform(c)
        self.assertEqual((c["platform"], c["token"]), ("greenhouse", "razorpay"))

    def test_old_poisoned_cache_file_entry_is_ignored_after_ttl(self):
        old = time.time() - platform_cache.NEGATIVE_TTL_SECONDS - 3600
        from datetime import datetime, timezone
        platform_cache._cache = {"razorpay": {
            "platform": "custom",
            "discovered_at": datetime.fromtimestamp(old, timezone.utc).isoformat()}}
        self.assertIsNone(platform_cache.get_cached_platform("Razorpay"))
        # a recent negative is still honoured
        platform_cache._cache["razorpay"]["discovered_at"] = datetime.now(timezone.utc).isoformat()
        self.assertEqual(platform_cache.get_cached_platform("Razorpay")["platform"], "custom")

    def test_positive_cache_entries_never_expire(self):
        platform_cache._cache = {"razorpay": {
            "platform": "greenhouse", "token": "razorpay",
            "discovered_at": "2020-01-01T00:00:00+00:00"}}
        self.assertEqual(platform_cache.get_cached_platform("Razorpay")["token"], "razorpay")

    def test_confirmed_board_down_reports_failure_not_no_match(self):
        c = fresh_company()
        c.update(platform="greenhouse", token="razorpay", confirmed=True)
        with mock.patch.object(h, "_bounded_request", fake_factory("blocked")):
            jobs, used, total, note = search(c)
        self.assertTrue(rs.is_lookup_failure(note))

    def test_real_no_match_is_not_flagged_as_failure(self):
        c = fresh_company()
        c.update(platform="greenhouse", token="razorpay", confirmed=True)
        with mock.patch.object(h, "_bounded_request", fake_factory("ok")):
            jobs, used, total, note = rs.search_known_company(
                c, "quantum chef", 3, 30, False, apply_ai=False)
        self.assertEqual(jobs, [])
        self.assertFalse(rs.is_lookup_failure(note))
        self.assertIn("none matched", note)


if __name__ == "__main__":
    unittest.main()
