#!/usr/bin/env python3
"""
Shared verification logic: run birda models on candidate clips and compute
a confirm/disagree/no_detection verdict per model against the original
BirdNET-Go production detection. Used by both the CLI script and the
webapp's on-demand "Run" button.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import birdnetgo_client
import config

ORT_DYLIB_PATH = config.BIRDA_ORT_DYLIB_PATH
BIRDA_BIN = config.BIRDA_BIN
RESULTS_DIR = config.RESULTS_DIR
FFMPEG_BIN = config.FFMPEG_BIN
QUIET_MAX_DB_THRESHOLD = config.QUIET_MAX_DB_THRESHOLD
LOUD_MIN_DB_THRESHOLD = config.LOUD_MIN_DB_THRESHOLD
CONFIRM_THRESHOLD = 0.8
MODELS = ["birdnet-v24", "perch-v2"]

VOLUME_RE = re.compile(r"(mean|max)_volume:\s*(-?[\d.]+) dB")


def measure_loudness(clip_path):
    """Peak and mean loudness (dBFS) of a clip via ffmpeg's volumedetect
    filter -- ffmpeg decodes and discards the audio (-f null -), only its
    stderr analysis output is used. Returns (mean_db, max_db), either None
    if ffmpeg's output couldn't be parsed (e.g. a corrupt/empty clip)."""
    cmd = [FFMPEG_BIN, "-i", clip_path, "-af", "volumedetect", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    values = dict(VOLUME_RE.findall(result.stderr))
    mean_db = float(values["mean"]) if "mean" in values else None
    max_db = float(values["max"]) if "max" in values else None
    return mean_db, max_db


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


def top_detection(detections):
    if not detections:
        return None
    return max(detections, key=lambda d: d["confidence"])


def verdict_for(detections, original_species, confirm_threshold=CONFIRM_THRESHOLD):
    if not detections:
        return "no_detection", None, None
    top = top_detection(detections)
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
        rec = {}
        for model in MODELS:
            data = load_model_result(model_outdirs[model], c["clip_path"])
            detections = data["detections"] if data else []
            verdict, top_species, top_conf = verdict_for(detections, c["species"], confirm_threshold)
            rec[f"verify_{model.replace('-', '_')}"] = {
                "verdict": verdict,
                "top_species": top_species,
                "top_confidence": top_conf,
            }
        bn_verdict = rec["verify_birdnet_v24"]["verdict"]
        pc_verdict = rec["verify_perch_v2"]["verdict"]
        rec["overall_verdict"] = overall_bucket(bn_verdict, pc_verdict)

        mean_db, max_db = measure_loudness(c["clip_path"])
        rec["loudness"] = {"mean_db": mean_db, "max_db": max_db}
        rec["quiet"] = max_db is not None and max_db < QUIET_MAX_DB_THRESHOLD
        rec["loud"] = max_db is not None and max_db > LOUD_MIN_DB_THRESHOLD

        try:
            rec["birdnetgo_comment_posted"] = birdnetgo_client.post_verification_comment(
                c["detection_id"], c, rec
            )
        except Exception as e:  # noqa: BLE001 - a comment-post failure must not lose the birda result
            rec["birdnetgo_comment_error"] = str(e)

        results[str(c["detection_id"])] = rec
    return results
