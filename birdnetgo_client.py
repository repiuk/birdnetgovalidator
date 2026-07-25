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
MARKER = "[birda-validator]"

VERDICT_LABEL = {
    "confirm": "Confirm",
    "disagree": "Disagree",
    "no_detection": "No detection",
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
            "SELECT 1 FROM detection_comments WHERE detection_id = ? AND entry LIKE ? LIMIT 1",
            (detection_id, MARKER + "%"),
        )
        return cur.fetchone() is not None
    finally:
        con.close()


def get_marked_detection_ids():
    """All detection ids that currently have a [birda-validator] comment in
    BirdNET-Go — live truth, so it reflects reality regardless of when or how
    many times verify_candidates has run for a given detection."""
    con = connect_ro()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT detection_id FROM detection_comments WHERE entry LIKE ?",
            (MARKER + "%",),
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
