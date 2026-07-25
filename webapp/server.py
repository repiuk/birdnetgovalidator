#!/usr/bin/env python3
"""
Small local web server for browsing BirdNET-Go validation candidates.
Pure stdlib (no Flask) — serves the static UI, a candidates API (merged
with any cached verification results), a per-day "run birda now" action,
and audio clips with HTTP Range support for scrubbing.
"""

import json
import mimetypes
import random
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs, urlencode

WEBAPP_DIR = Path(__file__).parent
PROJECT_DIR = WEBAPP_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
import config  # noqa: E402
import verifier  # noqa: E402
import candidate_finder  # noqa: E402
import birdnetgo_client  # noqa: E402

CACHE_PATH = config.CACHE_PATH
CLIPS_BASE = config.CLIPS_DIR
PORT = config.WEBAPP_PORT

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

NOT_RUN = {"verdict": "not_run", "top_species": None, "top_confidence": None}
NO_LOUDNESS = {"mean_db": None, "max_db": None}

# Dutch common names, matching BirdNET-Go's own GUI locale (config.yaml locale: nl).
# Bundled with the birda birdnet-v24 model install; covers birds only, so
# non-bird species (e.g. insects Perch also classifies) fall back to the
# scientific name.
LABELS_NL_PATH = config.BIRDA_LABELS_NL_PATH


def load_common_names():
    mapping = {}
    if LABELS_NL_PATH.is_file():
        for line in LABELS_NL_PATH.read_text().splitlines():
            if "_" in line:
                sci, common = line.split("_", 1)
                mapping[sci] = common
    return mapping


COMMON_NAMES = load_common_names()


def load_candidates(rare_min=1, rare_max=5):
    # Always computed live from the DB — a static snapshot goes stale within
    # minutes since new detections stream in continuously.
    return candidate_finder.get_candidates(days=7, rare_min=rare_min, rare_max=rare_max)


def load_cache():
    if not CACHE_PATH.is_file():
        return {}
    return json.load(open(CACHE_PATH))


def save_cache(cache):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def with_top_common_name(v):
    if v.get("top_species"):
        return {**v, "top_common_name": COMMON_NAMES.get(v["top_species"], v["top_species"])}
    return v


def merge(candidates, cache):
    marked_ids = birdnetgo_client.get_marked_detection_ids()
    out = []
    for c in candidates:
        rec = dict(c)
        rec["common_name"] = COMMON_NAMES.get(c["species"], c["species"])
        cached = cache.get(str(c["detection_id"]))
        if cached:
            rec["verify_birdnet_v24"] = with_top_common_name(cached["verify_birdnet_v24"])
            rec["verify_perch_v2"] = with_top_common_name(cached["verify_perch_v2"])
            rec["overall_verdict"] = cached["overall_verdict"]
            rec["loudness"] = cached.get("loudness", NO_LOUDNESS)
            rec["quiet"] = cached.get("quiet", False)
            rec["loud"] = cached.get("loud", False)
        else:
            rec["verify_birdnet_v24"] = NOT_RUN
            rec["verify_perch_v2"] = NOT_RUN
            rec["overall_verdict"] = "not_run"
            rec["loudness"] = NO_LOUDNESS
            rec["quiet"] = False
            rec["loud"] = False
        rec["note_posted"] = c["detection_id"] in marked_ids
        out.append(rec)
    return out


def local_day(candidate):
    return candidate["detected_at_local"][:10]


XENO_CANTO_CACHE = {}
XENO_CANTO_QUALITY_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
XENO_CANTO_ACCEPTABLE_QUALITY = {"A", "B"}
XENO_CANTO_POOL_SIZE = 15
XENO_CANTO_SAMPLE_SIZE = 3


def _https(url):
    if url and url.startswith("//"):
        return "https:" + url
    return url


def fetch_xenocanto_pool(species):
    """The pool of quality A/B reference recordings for a scientific name,
    via xeno-canto API v3. Cached in-memory per species (never expires
    within a process lifetime — a species' available recordings barely
    change day to day, and this avoids hammering the API for common species
    that show up on many candidates). A random sample of this pool is what
    actually gets shown per request — see fetch_xenocanto()."""
    if species in XENO_CANTO_CACHE:
        return XENO_CANTO_CACHE[species]
    if not config.XENO_CANTO_API_KEY:
        return {"error": "no XENO_CANTO_API_KEY configured"}

    query = f'sp:"{species}" grp:birds'
    params = urlencode({"query": query, "key": config.XENO_CANTO_API_KEY, "per_page": 50})
    url = f"https://xeno-canto.org/api/3/recordings?{params}"

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        return {"error": str(e)}

    if "error" in data:
        return {"error": data["error"].get("message", str(data["error"]))}

    recordings = [
        r for r in data.get("recordings", [])
        if r.get("file") and r.get("q") in XENO_CANTO_ACCEPTABLE_QUALITY
    ]
    recordings.sort(key=lambda r: XENO_CANTO_QUALITY_ORDER.get(r.get("q"), 5))

    result = {
        "recordings": [
            {
                "id": r["id"],
                "url": _https(r["url"]),
                "file": _https(r["file"]),
                "recordist": r.get("rec", ""),
                "license": _https(r.get("lic", "")),
                "quality": r.get("q", ""),
                "length": r.get("length", ""),
                "country": r.get("cnt", ""),
            }
            for r in recordings[:XENO_CANTO_POOL_SIZE]
        ]
    }
    XENO_CANTO_CACHE[species] = result
    return result


def fetch_xenocanto(species):
    """A random sample of XENO_CANTO_SAMPLE_SIZE recordings from the cached
    quality pool, so repeated clicks surface different recordings instead
    of always the same top few."""
    pool = fetch_xenocanto_pool(species)
    if "error" in pool:
        return pool
    recordings = pool["recordings"]
    sample = random.sample(recordings, min(XENO_CANTO_SAMPLE_SIZE, len(recordings)))
    return {"recordings": sample}


def days_summary(candidates, cache):
    by_day = defaultdict(lambda: {"total": 0, "verified": 0, "with_clip": 0})
    for c in candidates:
        d = by_day[local_day(c)]
        d["total"] += 1
        if c["clip_exists"]:
            d["with_clip"] += 1
        if str(c["detection_id"]) in cache:
            d["verified"] += 1
    return [
        {"day": day, **stats}
        for day, stats in sorted(by_day.items(), reverse=True)
    ]


class Handler(BaseHTTPRequestHandler):
    server_version = "BirdNETValidator/1.0"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_file(WEBAPP_DIR / "index.html", "text/html")
        elif path == "/api/candidates":
            self._serve_candidates(parse_qs(parsed.query))
        elif path == "/api/days":
            self._serve_days(parse_qs(parsed.query))
        elif path == "/api/config":
            self._send_json({"birdnetgo_web_url": config.BIRDNETGO_WEB_URL})
        elif path == "/api/xenocanto":
            self._serve_xenocanto(parse_qs(parsed.query))
        elif path.startswith("/audio/"):
            self._serve_audio(unquote(path[len("/audio/"):]))
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/run":
            self._handle_run()
        elif path == "/api/review":
            self._handle_review()
        else:
            self.send_error(404, "Not found")

    def _serve_file(self, path, content_type):
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _rarity_params(self, qs):
        rare_min = int(qs.get("rare_min", [1])[0])
        rare_max = int(qs.get("rare_max", [5])[0])
        return rare_min, rare_max

    def _serve_candidates(self, qs):
        rare_min, rare_max = self._rarity_params(qs)
        candidates = load_candidates(rare_min, rare_max)
        cache = load_cache()
        self._send_json(merge(candidates, cache))

    def _serve_days(self, qs):
        rare_min, rare_max = self._rarity_params(qs)
        candidates = load_candidates(rare_min, rare_max)
        cache = load_cache()
        self._send_json(days_summary(candidates, cache))

    def _serve_xenocanto(self, qs):
        species = qs.get("species", [""])[0]
        if not species:
            self._send_json({"error": "missing 'species'"}, status=400)
            return
        self._send_json(fetch_xenocanto(species))

    def _handle_run(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        day = payload.get("day")
        force = bool(payload.get("force", False))
        confirm_threshold = float(payload.get("confirm_threshold", verifier.CONFIRM_THRESHOLD))
        rare_min = int(payload.get("rare_min", 1))
        rare_max = int(payload.get("rare_max", 5))

        if not day:
            self._send_json({"error": "missing 'day'"}, status=400)
            return

        candidates = load_candidates(rare_min, rare_max)
        day_candidates = [c for c in candidates if local_day(c) == day and c["clip_exists"]]

        cache = load_cache()
        to_run = day_candidates if force else [
            c for c in day_candidates if str(c["detection_id"]) not in cache
        ]

        if to_run:
            try:
                new_records = verifier.verify_candidates(to_run, confirm_threshold)
            except subprocess.CalledProcessError as e:
                self._send_json({"error": f"birda failed: {e}"}, status=500)
                return
            cache.update(new_records)
            save_cache(cache)

        self._send_json({
            "day": day,
            "ran": len(to_run),
            "skipped_cached": len(day_candidates) - len(to_run),
            "results": merge(day_candidates, cache),
        })

    def _handle_review(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        detection_ids = payload.get("detection_ids") or []
        verified = payload.get("verified")
        source = payload.get("source", "manual")
        if not detection_ids or verified not in ("correct", "false_positive"):
            self._send_json(
                {"error": "missing detection_ids or invalid verified value"}, status=400
            )
            return
        try:
            birdnetgo_client.set_verification(detection_ids, verified)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)
            return

        comment_errors = []
        for detection_id in detection_ids:
            try:
                birdnetgo_client.post_review_decision_comment(detection_id, verified, source)
            except Exception as e:  # noqa: BLE001 - a comment-post failure must not lose the review write
                comment_errors.append(str(e))

        resp = {"ok": True, "count": len(detection_ids)}
        if comment_errors:
            resp["comment_errors"] = comment_errors
        self._send_json(resp)

    def _serve_audio(self, clip_name):
        full_path = (CLIPS_BASE / clip_name).resolve()
        try:
            full_path.relative_to(CLIPS_BASE.resolve())
        except ValueError:
            self.send_error(403, "Forbidden")
            return
        if not full_path.is_file():
            self.send_error(404, "Clip not found (may have been purged)")
            return

        file_size = full_path.stat().st_size
        content_type = mimetypes.guess_type(str(full_path))[0] or "audio/flac"
        range_header = self.headers.get("Range")

        start, end = 0, file_size - 1
        status = 200
        if range_header:
            m = RANGE_RE.match(range_header)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with open(full_path, "rb") as f:
            f.seek(start)
            remaining = length
            chunk_size = 64 * 1024
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except BrokenPipeError:
                    break
                remaining -= len(chunk)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving on http://0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
