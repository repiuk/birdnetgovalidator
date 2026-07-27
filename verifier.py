#!/usr/bin/env python3
"""
Shared verification logic: run birda models on candidate clips and compute
a confirm/disagree/no_detection verdict per model against the original
BirdNET-Go production detection. Used by both the CLI script and the
webapp's on-demand "Run" button.
"""

import json
import os
import subprocess
from pathlib import Path

import birdnetgo_client
import config

ORT_DYLIB_PATH = config.BIRDA_ORT_DYLIB_PATH
BIRDA_BIN = config.BIRDA_BIN
RESULTS_DIR = config.RESULTS_DIR
CONFIRM_THRESHOLD = 0.8
MODELS = ["birdnet-v24", "perch-v2"]

# BirdNET-Go exports clips longer than the actual detection: `precapture`
# seconds of audio before the event plus enough after to cover it (its
# "Pre-Detection Buffer" setting, config.yaml realtime.audio.export.precapture
# -- see config.BIRDNETGO_PRECAPTURE_SECONDS). So a detection's begin_time
# lands PRECAPTURE_SECONDS into the clip, and end_time lands
# (begin_time - end_time) later than that -- confirmed against real
# detections (id 99679: begin/end 13000ms apart, clip audio_duration 20.0s,
# filename timestamp matches end_time). Clips often contain other birds
# calling before/after the actual detection window, so without this offset,
# picking birda's single loudest detection anywhere in the whole clip can
# pick up an unrelated bird instead of the one BirdNET-Go actually flagged.
#
# This is a *global* BirdNET-Go setting, not recorded per-detection, and it
# can change over time (confirmed: 6 -> 7 seconds on this install, changed
# 2026-07-16) -- clips exported before a change no longer match this offset.
# verdict_for()'s same-species fallback below handles those older clips.
PRECAPTURE_SECONDS = config.BIRDNETGO_PRECAPTURE_SECONDS


def run_birda_batch(model, clip_paths):
    if not clip_paths:
        return RESULTS_DIR / model
    outdir = RESULTS_DIR / model
    outdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ORT_DYLIB_PATH=ORT_DYLIB_PATH)
    cmd = [
        BIRDA_BIN, "-m", model, "-f", "json", "-o", str(outdir),
        "--force", "--quiet",
    ] + clip_paths
    subprocess.run(cmd, env=env, check=True)
    return outdir


def load_model_result(outdir, clip_path):
    basename = Path(clip_path).stem
    # birda names output <basename>.BirdNET.json regardless of actual model (known bug)
    result_path = outdir / f"{basename}.BirdNET.json"
    if not result_path.exists():
        return None
    with open(result_path) as f:
        return json.load(f)


def detection_window_seconds(begin_time_ms, end_time_ms):
    """Convert a detection's absolute begin/end_time (ms) into a (start, end)
    offset in seconds within its exported clip -- see PRECAPTURE_SECONDS."""
    start = PRECAPTURE_SECONDS
    end = start + (end_time_ms - begin_time_ms) / 1000.0
    return start, end


def top_detection(detections, window=None):
    """Highest-confidence detection, restricted to those overlapping `window`
    (start, end) in clip-relative seconds when given -- so a loud bird calling
    outside the actual detection window doesn't get picked over the one
    BirdNET-Go actually flagged."""
    if window is not None:
        win_start, win_end = window
        detections = [
            d for d in detections
            if d["start_time"] < win_end and d["end_time"] > win_start
        ]
    if not detections:
        return None
    return max(detections, key=lambda d: d["confidence"])


def verdict_for(detections, original_species, window=None, confirm_threshold=CONFIRM_THRESHOLD):
    top = top_detection(detections, window)
    if top is None and window is not None:
        # Nothing overlapped the assumed detection window -- this clip may
        # have been exported under a since-changed PRECAPTURE_SECONDS (see
        # its definition above). Retry restricted to the original species
        # anywhere in the clip instead of giving up with no_detection: still
        # requires species agreement, so this can't pick up an unrelated
        # bird calling elsewhere the way an unrestricted retry could.
        top = top_detection([d for d in detections if d["scientific_name"] == original_species])
    if top is None:
        return "no_detection", None, None
    same_species = top["scientific_name"] == original_species
    if same_species and top["confidence"] >= confirm_threshold:
        return "confirm", top["scientific_name"], top["confidence"]
    if same_species:
        # Same species as the original call, but below the confirm
        # threshold -- not a real disagreement, just not confident enough
        # on its own to auto-confirm.
        return "weak_match", top["scientific_name"], top["confidence"]
    return "disagree", top["scientific_name"], top["confidence"]


MATCHING_VERDICTS = ("confirm", "weak_match")


def overall_bucket(bn_verdict, pc_verdict):
    """
    Combine the two per-model verdicts (each confirm/weak_match/disagree/
    no_detection) into one of four buckets. The models must actually agree
    on the species to be auto-confirmable -- a single high-confidence
    confirm from one model is not enough if the other model disagrees, or
    found nothing at all:
      - auto_confirm: both models confirm the original species (CC)
      - manual_review: both models agree on the species (each is confirm or
        weak_match), but at least one is below the confirm threshold --
        a real judgment call, needs a human + xeno-canto
      - no_detection: neither model found anything (NN)
      - auto_reject: everything else -- a real species mismatch somewhere,
        or one model found nothing while the other did find something
    """
    if bn_verdict in MATCHING_VERDICTS and pc_verdict in MATCHING_VERDICTS:
        if bn_verdict == "confirm" and pc_verdict == "confirm":
            return "auto_confirm"
        return "manual_review"
    if bn_verdict == "no_detection" and pc_verdict == "no_detection":
        return "no_detection"
    return "auto_reject"


def verify_candidates(candidates, confirm_threshold=CONFIRM_THRESHOLD):
    """
    candidates: list of candidate dicts (from find_candidates.py) with
    clip_exists=True guaranteed by caller.
    Returns: dict keyed by str(detection_id) -> verification record.
    """
    clip_paths = [c["clip_path"] for c in candidates]

    model_outdirs = {}
    for model in MODELS:
        model_outdirs[model] = run_birda_batch(model, clip_paths)

    results = {}
    for c in candidates:
        window = None
        if c.get("begin_time") is not None and c.get("end_time") is not None:
            window = detection_window_seconds(c["begin_time"], c["end_time"])

        rec = {}
        for model in MODELS:
            data = load_model_result(model_outdirs[model], c["clip_path"])
            detections = data["detections"] if data else []
            verdict, top_species, top_conf = verdict_for(
                detections, c["species"], window, confirm_threshold
            )
            rec[f"verify_{model.replace('-', '_')}"] = {
                "verdict": verdict,
                "top_species": top_species,
                "top_confidence": top_conf,
            }
        bn_verdict = rec["verify_birdnet_v24"]["verdict"]
        pc_verdict = rec["verify_perch_v2"]["verdict"]
        rec["overall_verdict"] = overall_bucket(bn_verdict, pc_verdict)

        try:
            rec["birdnetgo_comment_posted"] = birdnetgo_client.post_verification_comment(
                c["detection_id"], c, rec
            )
        except Exception as e:  # noqa: BLE001 - a comment-post failure must not lose the birda result
            rec["birdnetgo_comment_error"] = str(e)

        results[str(c["detection_id"])] = rec
    return results
