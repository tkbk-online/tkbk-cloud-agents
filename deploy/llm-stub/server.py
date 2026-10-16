#!/usr/bin/env python3
"""LLM stub — an OpenAI-compatible /v1/chat/completions that plays a tiny scripted agent.

Only for local runs WITHOUT a real TKBK key (docker compose --profile stub): the sandbox agent calls it
through TKBK_OPENAI_BASE_URL=http://tkbk-llm-stub:8089/v1. Scenario per conversation (keyed by the
first user message): 1) call tool `terminal` to create TKBK_CLOUD_AGENT.md, 2) answer with a summary
and no tool call, which ends the OpenHands run. No model, no network, stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE: dict[str, int] = {}
MODEL = os.environ.get("STUB_MODEL", "stub")
COMMAND = (
    "printf '# TKBK Cloud Agents\\n\\nThis file was created by the local LLM stub (no real model).\\n' "
    "> TKBK_CLOUD_AGENT.md && ls -la"
)


def _conv_key(messages: list[dict]) -> str:
    first = next((m for m in messages if m.get("role") == "user"), None)
    text = json.dumps(first.get("content") if first else "", ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _tool_names(tools: list[dict]) -> list[str]:
    out = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            out.append(str(fn["name"]))
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        print("[llm-stub]", fmt % args, flush=True)

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        elif self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        messages = req.get("messages") or []
        tools = _tool_names(req.get("tools") or [])
        key = _conv_key(messages)
        step = STATE.get(key, 0)
        STATE[key] = step + 1
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if step == 0 and "terminal" in tools and not has_tool_result:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_stub_1", "type": "function",
                "function": {"name": "terminal", "arguments": json.dumps({"command": COMMAND})},
            }]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content":
                       "Created TKBK_CLOUD_AGENT.md in the repository (LLM stub run). Nothing else was changed."}
            finish = "stop"
        self._json(200, {
            "id": f"chatcmpl-stub-{int(time.time())}", "object": "chat.completion", "created": int(time.time()),
            "model": MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8089"))
    print(f"[llm-stub] listening on :{port} as model {MODEL!r}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
