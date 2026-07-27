#!/usr/bin/env python3
"""
CLI wrapper around candidate_finder.get_candidates() — prints a summary
and optionally writes the live candidate list to a JSON file. The webapp
calls candidate_finder directly (live, never stale); this script is for
manual/scripted inspection.
"""

import argparse
import json

import candidate_finder


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="rolling window size in days")
    ap.add_argument("--rarity", action="store_true", help="filter to a daily-rarity range instead of all detections")
    ap.add_argument("--rare-min", type=int, default=1)
    ap.add_argument("--rare-max", type=int, default=5)
    ap.add_argument("--out", type=str, default=None, help="write candidate list as JSON to this path")
    args = ap.parse_args()

    result = candidate_finder.get_candidates(
        days=args.days,
        rare_min=args.rare_min,
        rare_max=args.rare_max,
        rarity_enabled=args.rarity,
    )

    missing_clips = [c for c in result if c["clip_name"] and not c["clip_exists"]]
    no_clip = [c for c in result if not c["clip_name"]]

    print(f"Window: last {args.days} day(s)")
    print(f"Total unique candidates: {len(result)}")
    print(f"  - with clip on disk: {len(result) - len(missing_clips) - len(no_clip)}")
    print(f"  - clip_name set but missing on disk: {len(missing_clips)}")
    print(f"  - no clip_name at all: {len(no_clip)}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nWrote {len(result)} candidates to {args.out}")


if __name__ == "__main__":
    main()
