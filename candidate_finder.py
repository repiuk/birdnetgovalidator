#!/usr/bin/env python3
"""
Shared candidate-selection logic: find detections that need independent
verification, computed live from the database (never a stale snapshot).

By default, over a rolling window (default: last 7 days), every detection
is a candidate -- species-level noise is expected to already be filtered
out via excluded_species.txt, so there's no rarity gate by default.

Optional trigger (rarity_enabled=True):
  - Daily rarity: a species with rare_min-rare_max total detections on a
    given local calendar day (Europe/Amsterdam). Useful as an extra lens
    on top of the default "everything" view, not as the sole gate.

Cross-model disagreement (BirdNET vs Perch firing on the same audio source
within a short time proximity but naming different species) is implemented
below (find_disagreements) but NOT wired into get_candidates(). Multi-model
detection is a recent BirdNET-Go addition, not yet exposed in its own GUI,
and likely to change shape in future releases; the user doesn't currently
make verification decisions based on model agreement/disagreement. Keeping
the code in place in case that changes later.

Detections already reviewed by the user in BirdNET-Go's own UI (present in
`detection_reviews`, verified as either "correct" or "false_positive") are
excluded from the base row set, since a human has already made the call on
them there -- *unless* they're also locked. Locking means "hold this for a
later second look", so a review verdict shouldn't make it vanish from the
day it belongs to; those rows carry their `verified` value through so the
card can show it. Locked detections outside the rolling window still only
appear in the webapp's dedicated Locked view (see locked_finder.py) -- this
stays scoped to recent detections.
"""

import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import config

DB_PATH = config.DB_PATH
CLIPS_BASE = str(config.CLIPS_DIR)
LABELS_NL_PATH = config.BIRDA_LABELS_NL_PATH
EXCLUDED_SPECIES_PATH = config.EXCLUDED_SPECIES_PATH
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

MODEL_NAMES = {1: "BirdNET", 2: "Perch", 3: "BattyBirdNET"}


def connect_ro():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def load_excluded_species():
    """Scientific names to always skip, from a user-maintained list (one per
    line, blank lines and '#' comments ignored). Each line is looked up as a
    Dutch common name via the same birda label file the webapp uses for
    display; if it doesn't match anything there, it's used as-is as a
    literal scientific name instead. That fallback matters because
    BirdNET-Go's own models can disagree on taxonomy for the same species
    (e.g. jackdaw is 'Corvus monedula' from BirdNET but 'Coloeus monedula'
    from Perch) -- listing both forms catches every variant."""
    if not EXCLUDED_SPECIES_PATH.is_file():
        return set()

    common_to_sci = {}
    if LABELS_NL_PATH.is_file():
        for line in LABELS_NL_PATH.read_text().splitlines():
            if "_" in line:
                sci, common = line.split("_", 1)
                common_to_sci[common.strip().lower()] = sci

    excluded = set()
    for line in EXCLUDED_SPECIES_PATH.read_text().splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        excluded.add(common_to_sci.get(name.lower(), name))
    return excluded


def fetch_window(con, days):
    cur = con.cursor()
    cur.execute("SELECT MAX(detected_at) FROM detections;")
    max_ts = cur.fetchone()[0]
    cutoff = max_ts - days * 24 * 3600
    cur.execute(
        """
        SELECT d.id, d.detected_at, d.begin_time, d.end_time, d.source_id,
               d.model_id, l.scientific_name, d.confidence, d.clip_name,
               dr.verified
        FROM detections d
        JOIN labels l ON l.id = d.label_id
        LEFT JOIN detection_reviews dr ON dr.detection_id = d.id
        WHERE d.detected_at >= ?
          AND (dr.detection_id IS NULL
               OR d.id IN (SELECT detection_id FROM detection_locks))
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
            rid, ts, b1, e1, _, model, species, conf, clip, _verified = r
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
        rid, ts, b1, e1, src, model, species, conf, clip, _verified = r
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
    rid, ts, b1, e1, src, model, species, conf, clip, verified = r
    path, exists = clip_info(clip)
    return {
        "detection_id": rid,
        "detected_at_local": datetime.fromtimestamp(ts, tz=LOCAL_TZ).isoformat(),
        "source_id": src,
        "model": MODEL_NAMES.get(model, str(model)),
        "species": species,
        "confidence": conf,
        "begin_time": b1,
        "end_time": e1,
        "clip_name": clip,
        "clip_path": path,
        "clip_exists": exists,
        # Only ever non-None for a locked detection -- fetch_window excludes
        # already-reviewed detections unless they're locked (locking means
        # "hold for a second look", so a review verdict shouldn't hide it).
        "verified": verified,
        "reasons": list(reasons),
        **(extra or {}),
    }


def get_candidates(days=7, rare_min=1, rare_max=5, rarity_enabled=False):
    con = connect_ro()
    rows = fetch_window(con, days)

    excluded = load_excluded_species()
    if excluded:
        rows = [r for r in rows if r[6] not in excluded]

    if not rarity_enabled:
        # excluded_species.txt is doing the real filtering now, so by
        # default every detection in the window is a candidate -- the
        # daily-rarity trigger below is opt-in, not the sole gate.
        candidates = {r[0]: row_to_candidate(r, ["all"]) for r in rows}
        return sorted(candidates.values(), key=lambda c: c["detected_at_local"])

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
