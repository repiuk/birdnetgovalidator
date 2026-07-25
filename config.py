#!/usr/bin/env python3
"""
Central configuration, overridable via environment variables so this repo
can be pointed at a different BirdNET-Go install without editing code.
"""

import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent


def _load_dotenv(path):
    """Minimal .env support (KEY=value per line) — no external dependency.
    Existing environment variables always take precedence."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(REPO_DIR / ".env")

# Root of BirdNET-Go's own data directory (contains birdnet.db, clips/, logs/).
BIRDNETGO_DATA_DIR = Path(
    os.environ.get("BIRDNETGO_DATA_DIR", str(Path.home() / "birdnet-go-app/data"))
)
DB_PATH = str(BIRDNETGO_DATA_DIR / "birdnet.db")
CLIPS_DIR = BIRDNETGO_DATA_DIR / "clips"

# BirdNET-Go's web API, as reachable from wherever this server runs (usually
# the same host as BirdNET-Go itself, so localhost is the default).
BIRDNETGO_API_URL = os.environ.get("BIRDNETGO_API_URL", "http://localhost:8080").rstrip("/")

# BirdNET-Go's web UI as reachable from a browser on your LAN — only needs to
# differ from BIRDNETGO_API_URL if "localhost" wouldn't resolve correctly
# from your phone/laptop (e.g. set it to http://192.168.1.50:8080).
BIRDNETGO_WEB_URL = os.environ.get("BIRDNETGO_WEB_URL", BIRDNETGO_API_URL).rstrip("/")

# birda (github.com/tphakala/birda) install location.
BIRDA_HOME = Path(os.environ.get("BIRDA_HOME", str(Path.home() / ".local/share/birda")))
BIRDA_BIN = os.environ.get("BIRDA_BIN", str(Path.home() / ".local/bin/birda"))
BIRDA_ORT_DYLIB_PATH = os.environ.get(
    "BIRDA_ORT_DYLIB_PATH", str(BIRDA_HOME / "libonnxruntime.so")
)
BIRDA_LABELS_NL_PATH = BIRDA_HOME / "models/birdnet-v24-nl.txt"

WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", "8091"))

# xeno-canto API v3 key, tied to a personal (or app) xeno-canto account —
# see https://xeno-canto.org/explore/api. Optional: reference-recording
# lookups are simply unavailable if unset.
XENO_CANTO_API_KEY = os.environ.get("XENO_CANTO_API_KEY", "")

# Generated/runtime data, always local to this checkout.
RESULTS_DIR = REPO_DIR / "birda_results"
CACHE_PATH = REPO_DIR / "verification_cache.json"
