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
    if top["scientific_name"] == original_species and top["confidence"] >= confirm_threshold:
        return "confirm", top["scientific_name"], top["confidence"]
    return "disagree", top["scientific_name"], top["confidence"]


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
        verdicts = [rec[f"verify_{m.replace('-', '_')}"]["verdict"] for m in MODELS]
        if "confirm" in verdicts:
            overall = "confirm"
        elif all(v == "no_detection" for v in verdicts):
            overall = "no_detection"
        else:
            overall = "disagree"
        rec["overall_verdict"] = overall

        try:
            rec["birdnetgo_comment_posted"] = birdnetgo_client.post_verification_comment(
                c["detection_id"], c, rec
            )
        except Exception as e:  # noqa: BLE001 - a comment-post failure must not lose the birda result
            rec["birdnetgo_comment_error"] = str(e)

        results[str(c["detection_id"])] = rec
    return results
