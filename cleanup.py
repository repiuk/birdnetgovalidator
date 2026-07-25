#!/usr/bin/env python3
"""
Prunes local disk cruft that accumulates as this app runs over time:
verification_cache.json entries and birda_results/*.json files for
detections that have permanently aged out of candidate_finder's 7-day
window -- get_candidates() never looks further back than that, so once a
detection falls outside it, it can never be displayed or re-verified
again, making its cache/result entries dead weight.

Deliberately does NOT touch entries for detections still inside the 7-day
window even if clip_exists is False (clip already purged upstream by
BirdNET-Go's own retention policy) -- their cached verdict is still
meaningful review history, and the row needs it to render as anything
other than "not run".
"""

import json
from pathlib import Path

import candidate_finder
import config

CACHE_PATH = config.CACHE_PATH
RESULTS_DIR = config.RESULTS_DIR


def live_detection_state():
    """Every detection_id and clip filename stem that candidate_finder could
    still return under any rarity setting, within its fixed 7-day window."""
    candidates = candidate_finder.get_candidates(days=7, rare_min=1, rare_max=999999)
    live_ids = {str(c["detection_id"]) for c in candidates}
    live_stems = {Path(c["clip_path"]).stem for c in candidates if c["clip_path"]}
    return live_ids, live_stems


def clean_cache(live_ids):
    if not CACHE_PATH.is_file():
        return 0
    cache = json.loads(CACHE_PATH.read_text())
    stale_keys = [k for k in cache if k not in live_ids]
    for k in stale_keys:
        del cache[k]
    if stale_keys:
        CACHE_PATH.write_text(json.dumps(cache, indent=2))
    return len(stale_keys)


def clean_birda_results(live_stems):
    if not RESULTS_DIR.is_dir():
        return 0
    removed = 0
    for json_file in RESULTS_DIR.rglob("*.json"):
        stem = json_file.stem
        if stem.endswith(".BirdNET"):
            stem = stem[: -len(".BirdNET")]
        if stem not in live_stems:
            json_file.unlink()
            removed += 1
    return removed


def run_cleanup():
    live_ids, live_stems = live_detection_state()
    return {
        "cache_entries_removed": clean_cache(live_ids),
        "result_files_removed": clean_birda_results(live_stems),
    }
