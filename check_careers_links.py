#!/usr/bin/env python3
"""
Checks every careers_url in a companies CSV and reports its live status.

Usage:
    pip install requests
    python check_careers_links.py companies_1500_final.csv

Outputs:
    link_check_results.csv   -- one row per company with status + notes
    Also prints a summary to the console.
"""

import csv
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
TIMEOUT = 10          # seconds per request
MAX_WORKERS = 10      # parallel requests -- keep modest to avoid rate-limits/bans
RETRIES = 2


def check_url(company, url):
    """Returns (company, url, final_url, status, note)."""
    last_err = ""
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(
                url,
                headers=HEADERS,
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            code = r.status_code
            final_url = r.url
            if code == 200:
                note = "OK"
            elif code in (403, 999):
                note = "Blocked by bot-protection (site likely fine for humans)"
            elif 300 <= code < 400:
                note = "Redirected"
            elif code == 404:
                note = "NOT FOUND - URL likely wrong/outdated"
            elif code >= 500:
                note = "Server error - retry later"
            else:
                note = f"Unexpected status {code}"
            return (company, url, final_url, code, note)
        except requests.exceptions.SSLError as e:
            last_err = f"SSL error: {e}"
        except requests.exceptions.ConnectionError as e:
            last_err = f"Connection error: {e}"
        except requests.exceptions.Timeout:
            last_err = "Timed out"
        except requests.exceptions.RequestException as e:
            last_err = f"Request failed: {e}"
        time.sleep(1)  # brief pause before retry
    return (company, url, "", "ERROR", last_err)


def main():
    if len(sys.argv) < 2:
        print("Usage: python check_careers_links.py <companies.csv>")
        sys.exit(1)

    in_path = sys.argv[1]
    out_path = "link_check_results.csv"

    with open(in_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    print(f"Checking {len(rows)} careers URLs with {MAX_WORKERS} parallel workers...")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(check_url, r["company_name"], r["careers_url"]): r
            for r in rows
        }
        done = 0
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 50 == 0 or done == len(rows):
                print(f"  ...{done}/{len(rows)} checked")

    # Preserve original row order
    order = {r["company_name"]: i for i, r in enumerate(rows)}
    results.sort(key=lambda r: order.get(r[0], 0))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company_name", "original_url", "final_url", "status_code", "note"])
        w.writerows(results)

    ok = sum(1 for r in results if r[3] == 200)
    not_found = sum(1 for r in results if r[3] == 404)
    blocked = sum(1 for r in results if r[3] in (403, 999))
    errors = sum(1 for r in results if r[3] == "ERROR")
    other = len(results) - ok - not_found - blocked - errors

    print("\n--- Summary ---")
    print(f"OK (200):              {ok}")
    print(f"Blocked (403/999):     {blocked}  (often fine -- site is blocking scripts, not down)")
    print(f"Not found (404):       {not_found}  <-- review these")
    print(f"Connection errors:     {errors}  <-- review these")
    print(f"Other status codes:    {other}")
    print(f"\nFull details written to: {out_path}")


if __name__ == "__main__":
    main()
