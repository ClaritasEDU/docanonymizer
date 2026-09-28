"""Environment-driven configuration. Loads .env once at import time."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Source tree root: the directory that contains `app/`, `templates/`, `static/`.
# Always derived from this file's location - never overridable, so Flask can
# always find templates/static even under tests with a custom DOCANON_ROOT.
SOURCE_ROOT = Path(__file__).resolve().parent.parent

# Runtime data root: where uploads / output / keys / logs / config files live.
# Tests override via DOCANON_ROOT to isolate state per case.
ROOT = Path(os.environ.get("DOCANON_ROOT") or SOURCE_ROOT)

load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DEFAULT_ENDPOINT_URL = os.environ.get("DEFAULT_ENDPOINT_URL", "http://localhost:11434")
DEFAULT_ENDPOINT_STYLE = os.environ.get("DEFAULT_ENDPOINT_STYLE", "ollama")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "llama3.2")
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 50)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
CHUNK_TOKENS = _int("CHUNK_TOKENS", 2000)
CHUNK_OVERLAP_TOKENS = _int("CHUNK_OVERLAP_TOKENS", 200)
PORT = _int("PORT", 5000)
# Local LLM calls stream their answer; give up only after this many seconds
# with no new output (a slow machine is fine, a hung model is not).
LLM_STALL_TIMEOUT_S = _int("LLM_STALL_TIMEOUT_S", 180)
# Ollama context window. Its built-in default can be smaller than a chunk
# prompt, and Ollama then silently drops the start of the prompt.
OLLAMA_NUM_CTX = _int("OLLAMA_NUM_CTX", 8192)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
# Family Graph (community identifiers for rosters). Two read timeouts, in
# seconds (connecting always gives up after 5):
#   FAMILYGRAPH_TIMEOUT_S         quick calls - health check, [ TEST ], id lookup.
#   FAMILYGRAPH_ROSTER_TIMEOUT_S  roster plan and commit. A 2,000-row roster can
#                                 take 30-90 s on a small machine; the call fails
#                                 only after this long, with nothing half-done.
FAMILYGRAPH_TIMEOUT_S = _int("FAMILYGRAPH_TIMEOUT_S", 10)
FAMILYGRAPH_ROSTER_TIMEOUT_S = _int("FAMILYGRAPH_ROSTER_TIMEOUT_S", 600)

UPLOADS_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "output"
KEYS_DIR = ROOT / "keys"
LOG_FILE = ROOT / "anonymizer.log"
ENDPOINTS_FILE = ROOT / "endpoints.json"
GITHUB_FILE = ROOT / "github.json"
FAMILYGRAPH_FILE = ROOT / "familygraph.json"   # holds an API key - gitignored
TEMPLATES_DIR = SOURCE_ROOT / "templates"
STATIC_DIR = SOURCE_ROOT / "static"

# Ensure runtime dirs exist (cheap, idempotent).
for _d in (UPLOADS_DIR, OUTPUT_DIR, KEYS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
