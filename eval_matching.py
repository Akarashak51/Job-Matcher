"""
eval_matching.py
==================
Answers the question the buildathon brief explicitly asks for: "honest
metrics", not one cherry-picked example. This is a small, hand-labeled
test set of (role query, job title) pairs with a ground-truth
"is this actually a relevant match" label, run through:

  1. REGEX BASELINE   - role_search.build_role_pattern / title_matches
                         alone (the pipeline's ALWAYS-ON first pass).
  2. REGEX + AI        - the same regex pass, then ai_match.score_candidates
                         re-scores whatever the regex let through and a
                         score threshold decides the final call (this is
                         exactly what search_known_company() does in
                         production - see role_search._apply_ai_ranking).

Important, and reported honestly below rather than glossed over: AI can
only IMPROVE PRECISION by removing false positives the regex pass let
through - it can never recover a true match the regex pass already
excluded, because in production that job is never in the shortlist AI
sees at all. So AI's recall on this test set is capped by the regex
pass's recall; the metric that actually demonstrates AI's value here is
precision (and F1), not recall.

Usage:
    python3 eval_matching.py
    (works with no API key - reports the regex baseline only, and says
    so; set GEMINI_API_KEY to also see the regex+AI comparison)
"""

import ai_match
from role_search import build_role_pattern, build_exclude_pattern, title_matches

# ---------------------------------------------------------------------
# Labeled test set. Each case is one role query against a mix of:
#   - clear true positives (should match)
#   - clear true negatives (should not match, and the regex correctly
#     rejects them - not interesting on their own, but needed so
#     precision/recall aren't computed against an all-positive set)
#   - REGEX FALSE POSITIVES: titles that contain every query word as a
#     whole word (so the keyword pass lets them through) but are NOT
#     actually the role being searched for - these are the cases the
#     AI re-ranking layer exists to catch.
#   - a REGEX FALSE NEGATIVE per query: a real match phrased in a way
#     the keyword pass structurally cannot catch (missing a required
#     token) - included to make the recall-ceiling caveat above
#     concrete rather than theoretical.
# ---------------------------------------------------------------------

CASES = [
    {
        "query": "backend developer",
        "max_experience": 5,
        "include_senior": False,
        "candidates": [
            {"title": "Backend Developer", "expected": True},
            {"title": "Developer, Backend Team", "expected": True},
            {"title": "Backend Developer (Node.js)", "expected": True},
            {"title": "Senior Backend Developer", "expected": False},  # correctly excluded by seniority filter
            {"title": "Frontend Developer", "expected": False},
            {"title": "Backend Developer Advocate", "expected": False},  # regex false positive: DevRel, not an IC engineering role
            {"title": "Backend Team Offsite & Culture Developer", "expected": False},  # regex false positive: HR/culture role, not engineering
            {"title": "SDE-2, Backend Systems", "expected": True},  # regex false negative: doesn't contain "developer" - AI cannot fix this
        ],
    },
    {
        "query": "product manager",
        "max_experience": 6,
        "include_senior": False,
        "candidates": [
            {"title": "Product Manager", "expected": True},
            {"title": "Associate Product Manager", "expected": True},
            {"title": "Product Manager - Payments", "expected": True},
            {"title": "Senior Product Manager", "expected": False},  # correctly excluded by seniority filter
            {"title": "Manager, Product Photography Studio", "expected": False},  # regex false positive: has both words, wrong domain entirely
            {"title": "Warehouse Product Returns Manager", "expected": False},  # regex false positive: logistics, not a PM role
            {"title": "Engineering Manager", "expected": False},
            {"title": "APM - New Grad Product", "expected": True},  # regex false negative: no whole-word "manager" - AI cannot fix this
        ],
    },
]


def _prf1(tp, fp, fn):
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else float("nan")
    return precision, recall, f1


def _fmt(x):
    return "n/a" if x != x else f"{x:.2f}"  # x != x is the NaN check


def run():
    ai_on = ai_match.is_configured()
    print("=" * 72)
    print("Matching pipeline evaluation")
    print("=" * 72)
    if not ai_on:
        print(
            "GEMINI_API_KEY is not set - showing the regex-only baseline.\n"
            "Set it to also see the regex+AI comparison this script is built for.\n"
        )

    grand_baseline = {"tp": 0, "fp": 0, "fn": 0}
    grand_ai = {"tp": 0, "fp": 0, "fn": 0}

    for case in CASES:
        query = case["query"]
        role_pattern = build_role_pattern(query)
        exclude_pattern = build_exclude_pattern(query, case["include_senior"])

        print(f'\nQuery: "{query}"')
        print("-" * 72)

        # ---- pass 1: regex baseline, exactly as role_search runs it ----
        baseline_kept = []  # candidates the regex pass would keep (mirrors _build_kept)
        b = {"tp": 0, "fp": 0, "fn": 0}
        for c in case["candidates"]:
            pred = title_matches(c["title"], role_pattern, exclude_pattern)
            if pred and c["expected"]:
                b["tp"] += 1
            elif pred and not c["expected"]:
                b["fp"] += 1
            elif not pred and c["expected"]:
                b["fn"] += 1
            tag = "KEEP" if pred else "drop"
            correct = "correct" if pred == c["expected"] else "WRONG"
            print(f'  regex   [{tag:4}] ({correct:7}) {c["title"]}')
            if pred:
                baseline_kept.append(c)

        for k in grand_baseline:
            grand_baseline[k] += b[k]
        bp, br, bf1 = _prf1(b["tp"], b["fp"], b["fn"])
        print(f"  regex-only:  precision={_fmt(bp)}  recall={_fmt(br)}  f1={_fmt(bf1)}")

        # ---- pass 2: AI re-ranks whatever the regex pass kept ----
        if not ai_on or not baseline_kept:
            continue

        scored = ai_match.score_candidates(
            query, case["max_experience"],
            [{"title": c["title"], "location": "", "experience_note": ""} for c in baseline_kept],
        )
        if not scored:
            print("  (AI call failed/unavailable for this case - falling back to regex-only, as production would)")
            continue

        THRESHOLD = 50
        a = {"tp": 0, "fp": 0, "fn": 0}
        for original, s in zip(baseline_kept, scored):
            ai_accept = s["ai_score"] >= THRESHOLD
            if ai_accept and original["expected"]:
                a["tp"] += 1
            elif ai_accept and not original["expected"]:
                a["fp"] += 1
            elif not ai_accept and original["expected"]:
                a["fn"] += 1
            verdict = "KEEP" if ai_accept else "drop"
            correct = "correct" if ai_accept == original["expected"] else "WRONG"
            print(f'  +AI     [{verdict:4}] ({correct:7}) {original["title"]}  '
                  f'-> score={s["ai_score"]} ("{s["ai_reason"]}")')
        # regex false negatives stay false negatives - AI never saw them
        a["fn"] += b["fn"]

        for k in grand_ai:
            grand_ai[k] += a[k]
        ap, ar, af1 = _prf1(a["tp"], a["fp"], a["fn"])
        print(f"  regex+AI:    precision={_fmt(ap)}  recall={_fmt(ar)}  f1={_fmt(af1)}")

    print("\n" + "=" * 72)
    print("TOTALS across all queries")
    print("=" * 72)
    bp, br, bf1 = _prf1(**grand_baseline)
    print(f"regex-only : tp={grand_baseline['tp']} fp={grand_baseline['fp']} fn={grand_baseline['fn']}"
          f"  ->  precision={_fmt(bp)}  recall={_fmt(br)}  f1={_fmt(bf1)}")
    if ai_on:
        ap, ar, af1 = _prf1(**grand_ai)
        print(f"regex+AI   : tp={grand_ai['tp']} fp={grand_ai['fp']} fn={grand_ai['fn']}"
              f"  ->  precision={_fmt(ap)}  recall={_fmt(ar)}  f1={_fmt(af1)}")
        print(
            "\nNote on recall: AI can only remove regex false positives, never add back "
            "a job the regex pass already excluded - so AI's recall is capped at the "
            "regex-only recall by construction. The metric AI is meant to move is "
            "precision (and therefore F1), not recall. See the module docstring."
        )
    else:
        print("\nSet GEMINI_API_KEY and re-run to see the regex+AI row here.")


if __name__ == "__main__":
    run()
