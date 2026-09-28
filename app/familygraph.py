"""Family Graph connection - community identifiers for rosters.

Family Graph is the registry that knows who is who in the parish and school.
It is the only system that issues community identifiers: `I` + hex for an
individual, `F` + hex for a family/household. The same person keeps the same
id in every file, every year, and every product. This module is the client.

Privacy and security (CLAUDE.md privacy mandate):
  - Family Graph must run on this machine or the local network. A non-local
    URL is refused - there is no override, unlike LLM endpoints, because
    this call carries the whole roster.
  - Proxy environment variables are ignored (`trust_env = False`), so a
    roster can never be routed through a proxy that happens to be set.
  - The API key lives in `familygraph.json` (0600, gitignored) and is never
    returned by the API or written to the log.
  - Logs carry counts, HTTP status, and timings only - never a name, a cell
    value, or an identifier.

Settings file shape:
  {"base_url": "http://127.0.0.1:3500", "api_key": "sk_...",
   "category": "school" | "church" | "other"}
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from . import endpoints as endpoints_mod
from .config import FAMILYGRAPH_FILE, FAMILYGRAPH_ROSTER_TIMEOUT_S, FAMILYGRAPH_TIMEOUT_S
from .logging_setup import get_logger

log = get_logger("familygraph")

_LOCK = threading.Lock()
CATEGORIES = ("school", "church", "other")
_CONNECT_TIMEOUT_S = 5


class FamilyGraphError(RuntimeError):
    """A call to Family Graph failed. `status` is the HTTP status (0 = no
    connection); `body` is the parsed JSON body when there is one;
    `timed_out` is True when Family Graph was reached but did not answer
    within the read timeout."""

    def __init__(self, message: str, status: int = 0, body: Optional[dict] = None,
                 timed_out: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body or {}
        self.timed_out = timed_out


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _read() -> dict:
    if not FAMILYGRAPH_FILE.exists():
        return {}
    try:
        data = json.loads(FAMILYGRAPH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("familygraph.json unreadable: %s", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    with _LOCK:
        tmp = FAMILYGRAPH_FILE.with_suffix(".tmp")
        # Created 0600 from the start: the key is never world-readable, not
        # even for the instant between write and chmod.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, FAMILYGRAPH_FILE)
        try:
            os.chmod(FAMILYGRAPH_FILE, 0o600)
        except OSError:
            pass


def settings() -> dict:
    """Stored settings (with the key). Server-side use only."""
    return _read()


def redacted() -> dict:
    s = _read()
    return {
        "configured": is_configured(),
        "base_url": s.get("base_url") or "",
        "key_set": bool(s.get("api_key")),
        "category": s.get("category") or "other",
    }


def is_configured() -> bool:
    s = _read()
    return bool(s.get("base_url") and s.get("api_key"))


def _validate_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Family Graph URL must look like http://127.0.0.1:3500")
    if parsed.path not in ("", "/"):
        raise ValueError("Family Graph URL is the server address only, no path")
    if not endpoints_mod.is_local_url(url):
        raise ValueError(
            "Family Graph must run on this machine or the local network. "
            "A roster is never sent to a non-local address."
        )
    return url


def save(payload: dict) -> dict:
    cur = _read()
    url = _validate_url(payload.get("base_url") or cur.get("base_url") or "")
    key = payload.get("api_key")
    if isinstance(key, str) and key.strip():
        key = key.strip()
        if len(key) < 16 or any(c.isspace() for c in key):
            raise ValueError("that does not look like a Family Graph API key")
    else:
        key = cur.get("api_key") or ""
    if not key:
        raise ValueError("an API key is required (Family Graph: POST /api/keys)")
    category = payload.get("category") or cur.get("category") or "other"
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    _write({"base_url": url, "api_key": key, "category": category})
    log.info("family graph settings saved: host=%s category=%s", urlparse(url).hostname, category)
    return redacted()


def clear() -> bool:
    if FAMILYGRAPH_FILE.exists():
        FAMILYGRAPH_FILE.unlink()
        log.info("family graph settings removed")
        return True
    return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False          # never route a roster through a proxy
    return s


def _read_timed_out(exc: requests.ConnectionError) -> bool:
    """requests reports a read timeout hit while the body is downloading as a
    ConnectionError wrapping urllib3's ReadTimeoutError."""
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, requests.Timeout) or type(cur).__name__ == "ReadTimeoutError":
            return True
        nxt = cur.args[0] if cur.args and isinstance(cur.args[0], BaseException) else None
        reason = getattr(cur, "reason", None)
        cur = nxt or (reason if isinstance(reason, BaseException) else None) or cur.__cause__ or cur.__context__
    return False


def _call(method: str, path: str, body: Optional[dict] = None, timeout: Optional[float] = None) -> tuple[int, dict]:
    """One HTTP call. `timeout` is the read timeout in seconds; quick calls
    use the default (FAMILYGRAPH_TIMEOUT_S), roster plan/commit pass
    FAMILYGRAPH_ROSTER_TIMEOUT_S."""
    read_timeout = timeout or FAMILYGRAPH_TIMEOUT_S
    s = _read()
    base = s.get("base_url")
    key = s.get("api_key")
    if not base or not key:
        raise FamilyGraphError("Family Graph is not configured", 0)
    # Re-checked on every call: a hand-edited settings file gets no pass.
    try:
        base = _validate_url(base)
    except ValueError as exc:
        raise FamilyGraphError(str(exc), 0) from exc
    headers = {
        "Authorization": f"Bearer {key}",
        "x-family-graph-actor": "docanonymizer",
        "Accept": "application/json",
    }
    started = time.monotonic()
    try:
        with _session() as sess:
            resp = sess.request(
                method, f"{base}{path}", json=body, headers=headers,
                timeout=(_CONNECT_TIMEOUT_S, read_timeout),
                allow_redirects=False,
            )
    except requests.ConnectTimeout as exc:
        log.warning("family graph unreachable: %s %s (ConnectTimeout after %ds)", method, path, _CONNECT_TIMEOUT_S)
        raise FamilyGraphError("Family Graph is not reachable - is it running?", 0) from exc
    except (requests.Timeout, requests.ConnectionError) as exc:
        elapsed = time.monotonic() - started
        if isinstance(exc, requests.Timeout) or _read_timed_out(exc):
            log.warning("family graph timed out: %s %s after %.1fs (read timeout %ss)",
                        method, path, elapsed, read_timeout)
            raise FamilyGraphError(
                f"Family Graph did not answer within {read_timeout:g} seconds", 0, timed_out=True) from exc
        log.warning("family graph unreachable: %s %s (%s)", method, path, type(exc).__name__)
        raise FamilyGraphError("Family Graph is not reachable - is it running?", 0) from exc
    elapsed = time.monotonic() - started
    try:
        data = resp.json()
    except ValueError:
        data = {}
    log.info("family graph %s %s -> %d (%.2fs)", method, path, resp.status_code, elapsed)
    return resp.status_code, data if isinstance(data, dict) else {}


def _raise_for(status: int, data: dict, what: str) -> None:
    detail = data.get("detail") or data.get("error") or ""
    if status == 401:
        raise FamilyGraphError("Family Graph rejected the API key", status, data)
    if status == 403:
        raise FamilyGraphError(
            f"the API key is missing a scope for {what} (needs pii.read and import)", status, data)
    if status == 429:
        raise FamilyGraphError("Family Graph is rate limiting - wait a minute and retry", status, data)
    raise FamilyGraphError(f"Family Graph {what} failed ({status}) {detail}".strip(), status, data)


def check() -> dict:
    """Is Family Graph reachable, and does the key work? Never raises."""
    try:
        status, _ = _call("GET", "/api/health")
        if status != 200:
            return {"status": "err", "error": f"health check returned {status}"}
        # Any well-formed id: 404 means authenticated, 401/403 means not.
        status, _ = _call("GET", "/api/identity/roster/lookup/I0000000000000000")
    except FamilyGraphError as exc:
        return {"status": "err", "error": str(exc)}
    if status in (200, 404):
        return {"status": "ok"}
    if status == 401:
        return {"status": "err", "error": "Family Graph rejected the API key"}
    if status == 403:
        return {"status": "err", "error": "the API key lacks the pii.read scope"}
    return {"status": "err", "error": f"unexpected response {status} - is this Family Graph with roster support?"}


def plan(body: dict) -> dict:
    """Dry run - Family Graph writes nothing. Long read timeout: a large
    roster takes a while to match."""
    status, data = _call("POST", "/api/identity/roster/plan", body, timeout=FAMILYGRAPH_ROSTER_TIMEOUT_S)
    if status != 200:
        _raise_for(status, data, "roster plan")
    return data


def commit(body: dict) -> tuple[bool, dict]:
    """(True, result) when written; (False, plan) when Family Graph refused
    because review items are still undecided (nothing was written)."""
    status, data = _call("POST", "/api/identity/roster/commit", body, timeout=FAMILYGRAPH_ROSTER_TIMEOUT_S)
    if status == 201:
        return True, data
    if status == 409 and data.get("error") == "review_incomplete":
        return False, data.get("plan") or {}
    _raise_for(status, data, "roster commit")
    return False, {}  # unreachable
