"""LLM adapter layer.

All LLM I/O for the app goes through `llm_call(prompt, endpoint=...) -> str`.
Two API styles are supported per PRD 7.2:
  - "ollama":  POST {base_url}/api/generate
  - "openai":  POST {base_url}/v1/chat/completions

Both stream the answer. A dense spreadsheet chunk can take a local model
minutes to answer; waiting for the whole answer in one piece hit a hard
timeout on a 25-row donor sheet and aborted the run. Streaming lets the call
run as long as tokens keep arriving and fail only when the model stalls for
LLM_STALL_TIMEOUT_S.

`schema` (Ollama only) constrains the answer to a JSON schema, so the reply
is always parseable. If the model runs out of room mid-answer the call
raises LLMTruncated, and the detector splits the chunk and tries again -
a cut-off answer is never treated as "no PII".

Never logs prompt or response text - they contain the document.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import requests

from . import endpoints as endpoints_mod
from .config import LLM_STALL_TIMEOUT_S, OLLAMA_NUM_CTX
from .logging_setup import get_logger

log = get_logger("llm")

_CONNECT_TIMEOUT = 10


class LLMError(RuntimeError):
    """Raised when the active LLM endpoint returns an error or is unreachable."""


class LLMTruncated(LLMError):
    """The model stopped because it ran out of room (length limit)."""


def llm_call(prompt: str, endpoint: Optional[dict] = None, timeout: Optional[int] = None,
             schema: Optional[dict] = None) -> str:
    """Send a prompt to the active (or supplied) endpoint and return the raw text response.

    `timeout` is the stall timeout: seconds allowed with no new output.
    """
    ep = endpoint or endpoints_mod.get_active()
    if ep is None:
        raise LLMError("no LLM endpoint configured")
    style = ep.get("api_style")
    base = (ep.get("base_url") or "").rstrip("/")
    model = ep.get("model")
    if not base or not model:
        raise LLMError("endpoint missing base_url or model")

    if not endpoints_mod.is_local_url(base):
        # Loud, every call - the privacy mandate says local-only, and this is
        # the last checkpoint before document text goes on the wire.
        log.warning("NON-LOCAL LLM endpoint in use: nickname=%s - document "
                    "text is leaving this machine", ep.get("nickname"))

    stall = timeout or LLM_STALL_TIMEOUT_S
    started = time.monotonic()
    if style == "ollama":
        text, reason = _call_ollama(base, model, prompt, stall, schema)
    elif style == "openai":
        text, reason = _call_openai(base, model, prompt, stall)
    else:
        raise LLMError(f"unsupported api_style: {style}")

    elapsed = time.monotonic() - started
    log.debug(
        "llm_call complete: endpoint=%s style=%s tokens_in~%d chars_out=%d stop=%s t=%.2fs",
        ep.get("nickname"), style, len(prompt) // 4, len(text), reason, elapsed,
    )
    if reason == "length":
        raise LLMTruncated("the model ran out of room before finishing its answer")
    return text


def _post_stream(url: str, body: dict, stall: int) -> requests.Response:
    try:
        resp = requests.post(url, json=body, stream=True, timeout=(_CONNECT_TIMEOUT, stall))
    except requests.RequestException as exc:
        raise LLMError(f"request failed: {type(exc).__name__}") from exc
    return resp


def _error_detail(resp: requests.Response) -> str:
    try:
        detail = resp.json().get("error")
        if isinstance(detail, dict):
            detail = detail.get("message")
        return str(detail or "")[:200]
    except (ValueError, AttributeError):
        return ""


def _call_ollama(base: str, model: str, prompt: str, stall: int,
                 schema: Optional[dict]) -> tuple[str, Optional[str]]:
    url = f"{base}/api/generate"
    body = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX, "num_predict": -1},
    }
    if schema is not None:
        body["format"] = schema
    resp = _post_stream(url, body, stall)
    if resp.status_code == 400 and schema is not None:
        # Older Ollama builds don't accept a JSON-schema "format".
        log.warning("ollama rejected structured output (%s) - retrying without schema",
                    _error_detail(resp) or "http 400")
        body.pop("format")
        resp = _post_stream(url, body, stall)
    if resp.status_code != 200:
        raise LLMError(f"ollama http {resp.status_code} {_error_detail(resp)}".strip())
    parts: list[str] = []
    reason: Optional[str] = None
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise LLMError("ollama: malformed stream line") from exc
            if obj.get("error"):
                raise LLMError(f"ollama: {str(obj['error'])[:200]}")
            parts.append(obj.get("response") or "")
            if obj.get("done"):
                reason = obj.get("done_reason")
                break
        else:
            raise LLMError("ollama: stream ended before the answer finished")
    except requests.RequestException as exc:
        raise LLMError(f"ollama stalled: no output for {stall}s ({type(exc).__name__})") from exc
    finally:
        resp.close()
    return "".join(parts), reason


def _call_openai(base: str, model: str, prompt: str, stall: int) -> tuple[str, Optional[str]]:
    url = f"{base}/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "stream": True,
    }
    resp = _post_stream(url, body, stall)
    if resp.status_code != 200:
        raise LLMError(f"openai http {resp.status_code} {_error_detail(resp)}".strip())
    parts: list[str] = []
    reason: Optional[str] = None
    try:
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
                choice = obj["choices"][0]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError("openai: malformed stream chunk") from exc
            parts.append((choice.get("delta") or {}).get("content") or "")
            reason = choice.get("finish_reason") or reason
        if reason is None and not parts:
            raise LLMError("openai: empty response")
    except requests.RequestException as exc:
        raise LLMError(f"openai stalled: no output for {stall}s ({type(exc).__name__})") from exc
    finally:
        resp.close()
    return "".join(parts), reason
