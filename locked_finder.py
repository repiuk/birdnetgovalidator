#!/usr/bin/env python3
"""
Finds all currently-locked detections, regardless of age -- unlike
candidate_finder's rolling 7-day window, locking a detection is meant to
keep it around indefinitely for a later second look, so this has to search
the whole `detections` table.

BirdNET-Go's own `detection_locks` table has no reason/source column (just
detection_id + locked_at), so it can't tell a webapp-initiated "hold for
review" lock apart from one set directly in BirdNET-Go's own GUI for an
unrelated reason. The webapp works around this by always posting a
[validator]-marked comment when it locks something (see
birdnetgo_client.post_lock_comment) -- so a detection's origin here is
inferred from whether that marker comment exists, not from the lock itself.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from candidate_finder import MODEL_NAMES, clip_info, connect_ro

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def _marked_ids(cur, ids):
    """Of the given detection ids, which have a [validator]/legacy-marker
    comment -- i.e. were locked via this webapp, not directly in
    BirdNET-Go's own GUI. Shared by get_locked_detections() and
    get_locked_status_map()."""
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    cur.execute(
        f"""
        SELECT DISTINCT detection_id FROM detection_comments
        WHERE detection_id IN ({placeholders})
          AND (entry LIKE '[validator]%' OR entry LIKE '[birda-validator]%');
        """,
        ids,
    )
    return {row[0] for row in cur.fetchall()}


def get_locked_detections():
    con = connect_ro()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT d.id, d.detected_at, d.source_id, d.model_id,
                   l.scientific_name, d.confidence, d.clip_name,
                   dl.locked_at, dr.verified, d.begin_time, d.end_time
            FROM detections d
            JOIN labels l ON l.id = d.label_id
            JOIN detection_locks dl ON dl.detection_id = d.id
            LEFT JOIN detection_reviews dr ON dr.detection_id = d.id
            ORDER BY dl.locked_at DESC;
            """
        )
        rows = cur.fetchall()
        marked_ids = _marked_ids(cur, [r[0] for r in rows])
    finally:
        con.close()

    out = []
    for rid, ts, src, model, species, conf, clip, locked_at, verified, b1, e1 in rows:
        path, exists = clip_info(clip)
        out.append({
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
            "locked_at": locked_at,
            "verified": verified,
            "locked_via_validator": rid in marked_ids,
        })
    return out


def get_locked_status_map():
    """detection_id -> locked_via_validator, for every currently-locked
    detection. Lets a candidate row that's *also* currently locked show the
    same origin pill it would in the Locked view (see webapp/server.py
    merge()), instead of that being visible only there -- cheap since
    detection_locks is small (tens of rows, not thousands)."""
    con = connect_ro()
    try:
        cur = con.cursor()
        cur.execute("SELECT detection_id FROM detection_locks;")
        ids = [r[0] for r in cur.fetchall()]
        marked_ids = _marked_ids(cur, ids)
        return {rid: (rid in marked_ids) for rid in ids}
    finally:
        con.close()
