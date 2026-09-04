"""
warm_cache.py
=============
One-time (or occasional) cache-warming pass over the whole companies.csv
list. For each company, tries to discover its real API credentials
(Greenhouse token, Lever token, Workday tenant/host/site, ...) and saves
them to platform_cache.json - so that Job Watcher's bulk scans (which,
by design, don't do the slow Workday brute-force guess mid-scan) get the
benefit immediately instead of only picking it up the first time someone
searches that company individually on the dashboard.

Safe to re-run any time (e.g. after updating companies.csv) - already
resolved / already-confirmed-custom companies are skipped instantly.

Usage:
    python warm_cache.py                 # all companies
    python warm_cache.py --limit 50      # just the first 50 (quick test)
    python warm_cache.py --workday-only  # only attempt Workday-hinted rows
    python warm_cache.py --no-guess      # skip the slow brute-force Workday guess
                                          # (only do the fast direct-discovery pass)
"""

import argparse
import time

from hulk_job_search import COMPANIES, resolve_company_platform


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                     help="Only process the first N companies (for a quick test run)")
    ap.add_argument("--workday-only", action="store_true",
                     help="Only attempt companies whose CSV hint is Workday")
    ap.add_argument("--no-guess", action="store_true",
                     help="Skip the slow brute-force Workday tenant guess; "
                          "only do the fast direct-discovery pass")
    ap.add_argument("--sleep", type=float, default=0.3,
                     help="Seconds to sleep between companies (be polite to their servers)")
    args = ap.parse_args()

    companies = COMPANIES
    if args.workday_only:
        companies = [c for c in companies if c.get("platform") == "workday"]
    if args.limit:
        companies = companies[:args.limit]

    print(f"Warming cache for {len(companies)} companies "
          f"(workday guess: {'off' if args.no_guess else 'on'})...\n")

    resolved = {"greenhouse": 0, "lever": 0, "smartrecruiters": 0, "ashby": 0, "workday": 0, "custom": 0}
    already_done = 0

    for i, company in enumerate(companies, 1):
        before = (company.get("token"), company.get("wd_host"), company.get("_custom_confirmed"))
        resolve_company_platform(company, try_workday_guess=not args.no_guess)
        after = (company.get("token"), company.get("wd_host"), company.get("_custom_confirmed"))

        if before == after and (company.get("token") or company.get("wd_host") or company.get("_custom_confirmed")):
            already_done += 1
            tag = "cached"
        else:
            tag = "NEW"

        platform = company.get("platform", "custom")
        resolved[platform] = resolved.get(platform, 0) + 1

        detail = ""
        if company.get("token"):
            detail = f"token={company['token']}"
        elif company.get("wd_host"):
            detail = f"tenant={company.get('tenant')} host={company['wd_host']} site={company.get('site')}"
        elif company.get("_custom_confirmed"):
            detail = "confirmed custom (no known ATS found)"

        print(f"[{i:>4}/{len(companies)}] {tag:>6} | {company['name']:<30} -> {platform:<15} {detail}")
        time.sleep(args.sleep)

    print("\nDone.")
    print(f"Already cached from a previous run: {already_done}")
    print("Final platform breakdown:", resolved)


if __name__ == "__main__":
    main()
