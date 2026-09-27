"""LLM adapter over a real local HTTP server: streaming, truncation, stalls,
old-Ollama schema fallback, OpenAI-style SSE. No mocks of requests."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Server:
    def __init__(self, handler_fn):
        outer = self
        self.requests = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                handler_fn(self, body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _send_lines(h, lines, status=200, ctype="application/x-ndjson", pause=0.0):
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.end_headers()
    for ln in lines:
        h.wfile.write((ln + "\n").encode())
        h.wfile.flush()
        if pause:
            time.sleep(pause)


def _ep(url, style="ollama"):
    return {"base_url": url, "api_style": style, "model": "m", "nickname": "t"}


def test_ollama_streams_and_sends_context_and_schema():
    from app import llm
    def h(req, body):
        _send_lines(req, [json.dumps({"response": '[{"text":'}), json.dumps({"response": '"Jane","type":"PERSON"}]'}),
                          json.dumps({"response": "", "done": True, "done_reason": "stop"})], pause=0.05)
    srv = _Server(h)
    try:
        out = llm.llm_call("p", endpoint=_ep(srv.url), schema={"type": "array"})
        assert json.loads(out) == [{"text": "Jane", "type": "PERSON"}]
        _, body = srv.requests[0]
        assert body["stream"] is True and body["format"] == {"type": "array"}
        assert body["options"]["num_ctx"] >= 8192 and body["options"]["temperature"] == 0
    finally:
        srv.close()


def test_slow_but_steady_answer_is_not_a_timeout():
    """Total time well past the stall timeout is fine while tokens keep coming."""
    from app import llm
    lines = [json.dumps({"response": "["})] + [json.dumps({"response": " "})] * 12 + \
            [json.dumps({"response": "]", "done": True, "done_reason": "stop"})]
    srv = _Server(lambda h, b: _send_lines(h, lines, pause=0.25))
    try:
        assert llm.llm_call("p", endpoint=_ep(srv.url), timeout=1).strip() == "[" + " " * 12 + "]"
    finally:
        srv.close()


def test_stalled_model_raises():
    from app import llm
    def h(req, body):
        _send_lines(req, [json.dumps({"response": "["})])
        time.sleep(3)
    srv = _Server(h)
    try:
        with pytest.raises(llm.LLMError, match="stalled"):
            llm.llm_call("p", endpoint=_ep(srv.url), timeout=1)
    finally:
        srv.close()


def test_length_stop_raises_truncated():
    from app import llm
    srv = _Server(lambda h, b: _send_lines(h, [json.dumps({"response": '[{"text":"Ja', "done": True,
                                                              "done_reason": "length"})]))
    try:
        with pytest.raises(llm.LLMTruncated):
            llm.llm_call("p", endpoint=_ep(srv.url))
    finally:
        srv.close()


def test_old_ollama_without_schema_support_falls_back():
    from app import llm
    def h(req, body):
        if "format" in body:
            _send_lines(req, [json.dumps({"error": "invalid format"})], status=400, ctype="application/json")
        else:
            _send_lines(req, [json.dumps({"response": "[]", "done": True, "done_reason": "stop"})])
    srv = _Server(h)
    try:
        assert llm.llm_call("p", endpoint=_ep(srv.url), schema={"type": "array"}) == "[]"
        assert "format" in srv.requests[0][1] and "format" not in srv.requests[1][1]
    finally:
        srv.close()


def test_stream_that_ends_early_is_an_error():
    from app import llm
    srv = _Server(lambda h, b: _send_lines(h, [json.dumps({"response": '[{"text":"Jane"'})]))
    try:
        with pytest.raises(llm.LLMError, match="ended before"):
            llm.llm_call("p", endpoint=_ep(srv.url))
    finally:
        srv.close()


def test_openai_compatible_sse_stream():
    from app import llm
    chunks = [{"choices": [{"delta": {"content": '[{"text":"Jane",'}, "finish_reason": None}]},
              {"choices": [{"delta": {"content": '"type":"PERSON"}]'}, "finish_reason": "stop"}]}]
    lines = [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]
    srv = _Server(lambda h, b: _send_lines(h, lines, ctype="text/event-stream"))
    try:
        out = llm.llm_call("p", endpoint=_ep(srv.url, "openai"))
        assert json.loads(out) == [{"text": "Jane", "type": "PERSON"}]
        assert srv.requests[0][0] == "/v1/chat/completions" and srv.requests[0][1]["stream"] is True
    finally:
        srv.close()


def test_openai_length_finish_raises_truncated():
    from app import llm
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': '[{'}, 'finish_reason': 'length'}]})}",
             "data: [DONE]"]
    srv = _Server(lambda h, b: _send_lines(h, lines, ctype="text/event-stream"))
    try:
        with pytest.raises(llm.LLMTruncated):
            llm.llm_call("p", endpoint=_ep(srv.url, "openai"))
    finally:
        srv.close()
