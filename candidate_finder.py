#!/usr/bin/env python3
"""
Shared candidate-selection logic: find detections that need independent
verification, computed live from the database (never a stale snapshot).

Active trigger, over a rolling window (default: last 7 days):
  - Daily rarity: a species with 1-5 total detections on a given local
    calendar day (Europe/Amsterdam).

Cross-model disagreement (BirdNET vs Perch firing on the same audio source
within a short time proximity but naming different species) is implemented
below (find_disagreements) but NOT wired into get_candidates(). Multi-model
detection is a recent BirdNET-Go addition, not yet exposed in its own GUI,
and likely to change shape in future releases; the user doesn't currently
make verification decisions based on model agreement/disagreement. Keeping
the code in place in case that changes later.

Detections already reviewed by the user in BirdNET-Go's own UI (present in
`detection_reviews`, verified as either "correct" or "false_positive") are
excluded from the base row set entirely, since a human has already made the
call on them there.
"""

import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import config

DB_PATH = config.DB_PATH
CLIPS_BASE = str(config.CLIPS_DIR)
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

MODEL_NAMES = {1: "BirdNET", 2: "Perch", 3: "BattyBirdNET"}


def connect_ro():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def fetch_window(con, days):
    cur = con.cursor()
    cur.execute("SELECT MAX(detected_at) FROM detections;")
    max_ts = cur.fetchone()[0]
    cutoff = max_ts - days * 24 * 3600
    cur.execute(
        """
        SELECT d.id, d.detected_at, d.begin_time, d.end_time, d.source_id,
               d.model_id, l.scientific_name, d.confidence, d.clip_name
        FROM detections d
        JOIN labels l ON l.id = d.label_id
        WHERE d.detected_at >= ?
          AND d.id NOT IN (SELECT detection_id FROM detection_reviews)
        ORDER BY d.source_id, d.begin_time;
        """,
        (cutoff,),
    )
    return cur.fetchall()


def find_disagreements(rows, proximity_seconds):
    """Not currently used by get_candidates() — see module docstring."""
    by_source = defaultdict(list)
    for r in rows:
        by_source[r[4]].append(r)

    pairs = []
    used_in_pair = set()
    for src, items in by_source.items():
        items.sort(key=lambda r: r[2])  # sort by begin_time
        n = len(items)
        for i, r in enumerate(items):
            rid, ts, b1, e1, _, model, species, conf, clip = r
            if rid in used_in_pair:
                continue
            matches = []
            for j in range(i + 1, n):
                r2 = items[j]
                b2 = r2[2]
                if (b2 - b1) / 1000.0 > proximity_seconds:
                    break
                if r2[5] != model and r2[0] not in used_in_pair:
                    matches.append(r2)
            disagreeing = [m for m in matches if m[6] != species]
            if disagreeing:
                used_in_pair.add(rid)
                for m in disagreeing:
                    used_in_pair.add(m[0])
                pairs.append((r, disagreeing))
    return pairs


def find_daily_rarities(rows, rare_min, rare_max):
    by_day_species = defaultdict(list)
    for r in rows:
        rid, ts, b1, e1, src, model, species, conf, clip = r
        day = datetime.fromtimestamp(ts, tz=LOCAL_TZ).date()
        by_day_species[(day, species)].append(r)
    return {
        k: v for k, v in by_day_species.items() if rare_min <= len(v) <= rare_max
    }


def clip_info(clip_name):
    if not clip_name:
        return None, False
    path = os.path.join(CLIPS_BASE, clip_name)
    return path, os.path.isfile(path)


def row_to_candidate(r, reasons, extra=None):
    rid, ts, b1, e1, src, model, species, conf, clip = r
    path, exists = clip_info(clip)
    return {
        "detection_id": rid,
        "detected_at_local": datetime.fromtimestamp(ts, tz=LOCAL_TZ).isoformat(),
        "source_id": src,
        "model": MODEL_NAMES.get(model, str(model)),
        "species": species,
        "confidence": conf,
        "clip_name": clip,
        "clip_path": path,
        "clip_exists": exists,
        "reasons": list(reasons),
        **(extra or {}),
    }


def get_candidates(days=7, rare_min=1, rare_max=5):
    con = connect_ro()
    rows = fetch_window(con, days)

    rarities = find_daily_rarities(rows, rare_min, rare_max)

    candidates = {}

    for (day, species), items in rarities.items():
        for r in items:
            rid = r[0]
            if rid in candidates:
                if "daily_rarity" not in candidates[rid]["reasons"]:
                    candidates[rid]["reasons"].append("daily_rarity")
                candidates[rid]["day_species_count"] = len(items)
            else:
                cand = row_to_candidate(r, ["daily_rarity"])
                cand["day_species_count"] = len(items)
                candidates[rid] = cand

    return sorted(candidates.values(), key=lambda c: c["detected_at_local"])
