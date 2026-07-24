#!/usr/bin/env python3
"""
CLI: run verification for candidates (optionally filtered to one day) and
merge into verification_cache.json. The webapp's "Run" button does the
same thing on-demand for a single day via verifier.verify_candidates().

Usage:
  python3 verify_candidates.py --candidates candidates_last7d.json --day 2026-07-21
  python3 verify_candidates.py --candidates candidates_last7d.json   # all days
"""

import argparse
import json
from collections import Counter

import config
import verifier

CACHE_PATH = config.CACHE_PATH


def load_cache():
    if CACHE_PATH.is_file():
        return json.load(open(CACHE_PATH))
    return {}


def save_cache(cache):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", default="candidates_last7d.json")
    ap.add_argument("--day", default=None, help="only verify candidates from this local date, e.g. 2026-07-21")
    ap.add_argument("--force", action="store_true", help="re-verify even if already cached")
    args = ap.parse_args()

    candidates = json.load(open(args.candidates))
    if args.day:
        candidates = [c for c in candidates if c["detected_at_local"].startswith(args.day)]
    testable = [c for c in candidates if c["clip_exists"]]

    cache = load_cache()
    if not args.force:
        testable = [c for c in testable if str(c["detection_id"]) not in cache]

    print(f"Verifying {len(testable)} candidate(s)...")
    if testable:
        new_records = verifier.verify_candidates(testable)
        cache.update(new_records)
        save_cache(cache)

    verdicts = [cache[str(c["detection_id"])]["overall_verdict"] for c in candidates if str(c["detection_id"]) in cache]
    print(f"Cache now has {len(cache)} total records.")
    print("Verdict breakdown for this selection:", dict(Counter(verdicts)))


if __name__ == "__main__":
    main()
