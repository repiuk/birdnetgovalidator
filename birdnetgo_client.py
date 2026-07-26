#!/usr/bin/env python3
"""
Writes birda verification results back into BirdNET-Go as detection comments,
via its own web API (POST /api/v2/detections/:id/review with just a "comment"
field, verified omitted so it doesn't touch the review status).

Idempotency guarantee: before posting, checks BirdNET-Go's own
detection_comments table (source of truth, not a local cache file) for an
existing comment starting with MARKER for that detection_id. If one is
already there, posting is skipped — so re-running birda (force re-run, or
future workflow changes that call this again) can never create a second
comment for the same detection.
"""

import http.cookiejar
import json
import urllib.request

import config
from candidate_finder import connect_ro

BASE_URL = config.BIRDNETGO_API_URL
MARKER = "[validator]"
# Comments posted before 2026-07-26 used this longer marker. Kept only for
# matching (has_existing_comment/get_marked_detection_ids) so old entries are
# still recognized as ours; all new comments use MARKER.
LEGACY_MARKER = "[birda-validator]"

VERDICT_LABEL = {
    "auto_confirm": "CC auto-confirm",
    "auto_reject": "DD auto-reject",
    "manual_review": "MD manual review",
    "no_detection": "NN no detection",
}


def _fmt_model(v):
    if v["top_species"] is None:
        return "no detection"
    return f"{v['top_species']} ({v['top_confidence']:.2f})"


def build_comment(candidate, rec):
    bn = rec["verify_birdnet_v24"]
    pc = rec["verify_perch_v2"]
    overall = VERDICT_LABEL.get(rec["overall_verdict"], rec["overall_verdict"])
    return (
        f"{MARKER} {overall} | "
        f"birdnet: {_fmt_model(bn)} | "
        f"perch: {_fmt_model(pc)} | "
        f"BirdNET-Go: {candidate['species']} ({candidate['confidence']:.2f})"
    )


def has_existing_comment(detection_id):
    con = connect_ro()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT 1 FROM detection_comments WHERE detection_id = ? AND (entry LIKE ? OR entry LIKE ?) LIMIT 1",
            (detection_id, MARKER + "%", LEGACY_MARKER + "%"),
        )
        return cur.fetchone() is not None
    finally:
        con.close()


def get_marked_detection_ids():
    """All detection ids that currently have a [validator] (or legacy
    [birda-validator]) comment in BirdNET-Go — live truth, so it reflects
    reality regardless of when or how many times verify_candidates has run
    for a given detection."""
    con = connect_ro()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT detection_id FROM detection_comments WHERE entry LIKE ? OR entry LIKE ?",
            (MARKER + "%", LEGACY_MARKER + "%"),
        )
        return {row[0] for row in cur.fetchall()}
    finally:
        con.close()


def _csrf_post(path, payload):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.open(f"{BASE_URL}/", timeout=10).read()  # primes the csrf cookie
    token = next((c.value for c in cj if c.name == "csrf"), None)
    if not token:
        raise RuntimeError("could not obtain CSRF token from BirdNET-Go")

    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-CSRF-Token": token},
        method="POST",
    )
    with opener.open(req, timeout=10) as resp:
        body = resp.read()
        return json.loads(body) if body else None


def _post_comment(detection_id, comment_text):
    return _csrf_post(
        f"/api/v2/detections/{detection_id}/review", {"comment": comment_text}
    )


def set_verification(detection_ids, verified):
    """
    Marks one or more detections as 'correct' or 'false_positive' in
    BirdNET-Go, via the same batch review endpoint its own GUI uses for bulk
    actions — equivalent to picking the radio button and hitting Save Review
    on the Review Detection screen, just without a comment.
    """
    if verified not in ("correct", "false_positive"):
        raise ValueError(f"invalid verified value: {verified!r}")
    return _csrf_post(
        "/api/v2/detections/batch/review",
        {"ids": [str(i) for i in detection_ids], "verified": verified},
    )


def set_lock(detection_ids, locked):
    """
    Locks or unlocks one or more detections in BirdNET-Go (the same toggle as
    its own GUI, which also happens to exempt the clip from disk-usage-based
    purging). Independent of review status -- a locked detection can stay
    unverified indefinitely, which is what makes it usable as a "hold for a
    second look" signal from the webapp.
    """
    return _csrf_post(
        "/api/v2/detections/batch/lock",
        {"ids": [str(i) for i in detection_ids], "locked": bool(locked)},
    )


def post_lock_comment(detection_id, locked, source):
    """
    Logs a lock/unlock action taken via the webapp. Always posts (like
    post_review_decision_comment) -- this is also how a "held for review"
    detection gets distinguished from one locked directly in BirdNET-Go's own
    GUI for an unrelated reason: BirdNET-Go's detection_locks table has no
    reason/source column, so this marker comment is the only record of which
    locks came from here.
    """
    action = "Locked" if locked else "Unlocked"
    comment_text = f"{MARKER} {action} via webapp ({source})"
    _post_comment(detection_id, comment_text)


def post_verification_comment(detection_id, candidate, rec):
    """
    Posts a birda-verification comment for one detection, unless a marker
    comment already exists for it. Returns True if a comment was posted just
    now, False if skipped because one already existed.
    """
    if has_existing_comment(detection_id):
        return False
    comment_text = build_comment(candidate, rec)
    _post_comment(detection_id, comment_text)
    return True


REVIEW_VERDICT_LABEL = {"correct": "Correct", "false_positive": "False positive"}


def post_review_decision_comment(detection_id, verified, source):
    """
    Posts a comment recording that this detection's review status was set
    via the webapp, and how (auto vs manual), so it's visible later in
    BirdNET-Go's own review page alongside a direct-in-Go review. Always
    posts -- no existing-comment check, since the point is a log entry per
    decision, not a single idempotent marker like build_comment().
    """
    label = REVIEW_VERDICT_LABEL.get(verified, verified)
    comment_text = f"{MARKER} Reviewed via webapp: {label} ({source})"
    _post_comment(detection_id, comment_text)
