#!/usr/bin/env python3
"""LOCAL DEVELOPMENT ONLY: an OpenAI-compatible /v1/chat/completions server backed by
the ``claude`` CLI (``claude -p``).

Why: the evaluators will run the harness with DeepSeek or Qwen through the harness's
real OpenAI-compatible HTTP adapter. This bridge lets us exercise that exact adapter
and the whole pipeline end to end on this machine with a real model, without any
provider key. It is never used by the harness itself and is not part of a release
profile.

    AI_API_KEY=local-bridge-token python scripts/claude_openai_bridge.py --port 8765 \
        --emulate deepseek --flaky-429 0.05
    AI_API_KEY=local-bridge-token python scripts/claude_openai_bridge.py --port 8767 \
        --emulate groq --tpm 8000          # GroqCloud free tier: 8K tokens per minute
    HARNESS_MODEL_PROFILE=claude-bridge AI_API_KEY=local-bridge-token harness run ...

Quirk emulation (to test provider compatibility):
  --emulate deepseek   reject response_format json_schema with HTTP 400 (DeepSeek only
                       supports json_object) and require the word "json" in the prompt
  --emulate groq       the deepseek checks, plus HTTP 400 for reasoning_format "raw" with
                       json_object, and HTTP 400 json_validate_failed (reply text in
                       failed_generation) when a json_object reply is not valid JSON
  --flaky-429 P        answer a fraction P of requests with 429 + Retry-After: 1
  --tpm N              GroqCloud-style tokens-per-minute limit over a sliding 60 s window;
                       a request costs ceil(message chars / 4) + max_tokens. HTTP 413 when
                       one request exceeds N, 429 + retry-after while the window is full
The model's own habit of wrapping JSON in Markdown fences is passed through untouched.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOCK = threading.Lock()
STATS = {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "errors": 0, "injected_429": 0}
TPM_WINDOW: deque = deque()  # --tpm: (monotonic time, requested tokens) admitted in the last 60 s; guarded by LOCK
GROQ_UPGRADE = "Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing"


def log(message: str) -> None:
    sys.stderr.write(time.strftime("%H:%M:%S ") + message + "\n")
    sys.stderr.flush()


def flatten(messages):
    system, turns = [], []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if isinstance(content, list):  # OpenAI content parts
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        if role == "system":
            system.append(content)
        else:
            turns.append(f"[{role.upper()}]\n{content}")
    return "\n\n".join(system), "\n\n".join(turns)


def requested_tokens(payload) -> int:
    """GroqCloud-style TPM cost of a request: ceil(characters of all message contents / 4) + max_tokens."""
    chars = 0
    for message in payload.get("messages") or []:
        content = message.get("content") or ""
        if isinstance(content, list):  # OpenAI content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        chars += len(content)
    return math.ceil(chars / 4) + int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 8192)


def tpm_admit(limit: int, requested: int, model: str):
    """Sliding 60 s tokens-per-minute window like GroqCloud's free tier. Records the request and returns None
    when it fits; otherwise returns (status, body, headers) of Groq's 413 (never fits) or 429 (window full)."""
    where = f"for model `{model}` in organization `org_local` service tier `on_demand` on tokens per minute (TPM): Limit {limit}"
    if requested > limit:
        log(f"tpm 413: requested={requested} > limit={limit}")
        return 413, {"error": {"message": f"Request too large {where}, Requested {requested}, please reduce your message size "
                                          f"and try again. {GROQ_UPGRADE}", "type": "tokens", "code": "rate_limit_exceeded"}}, None
    with LOCK:
        now = time.monotonic()
        while TPM_WINDOW and TPM_WINDOW[0][0] <= now - 60:
            TPM_WINDOW.popleft()
        used = sum(tokens for _, tokens in TPM_WINDOW)
        if used + requested <= limit:
            TPM_WINDOW.append((now, requested))
            return None
        excess, wait = used + requested - limit, 0.0
        for stamp, tokens in TPM_WINDOW:  # oldest first: once these expire, enough of the window is free
            excess -= tokens
            if excess <= 0:
                wait = stamp + 60 - now
                break
    retry_after = max(1, math.ceil(wait))
    log(f"tpm 429: used={used} requested={requested} limit={limit} retry-after={retry_after}")
    return 429, {"error": {"message": f"Rate limit reached {where}, Used {used}, Requested {requested}. Please try again in "
                                      f"{wait:.2f}s. {GROQ_UPGRADE}", "type": "tokens", "code": "rate_limit_exceeded"}}, \
        {"retry-after": str(retry_after)}


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-openai-bridge/1.0"

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    def _send(self, status, payload, headers=None):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("x-request-id", f"bridge-{uuid.uuid4().hex[:16]}")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [{"id": self.server.args.model, "object": "model"}]})
        elif self.path == "/stats":
            with LOCK:
                self._send(200, dict(STATS))
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        args = self.server.args
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            return self._send(404, {"error": {"message": "unknown route"}})
        token = os.environ.get("AI_API_KEY") or args.token
        if token and self.headers.get("Authorization") != f"Bearer {token}":
            return self._send(401, {"error": {"message": "invalid api key"}})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8 * 1024 * 1024:
            return self._send(413, {"error": {"message": "request too large"}})
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send(400, {"error": {"message": "invalid json"}})
        response_format = (payload.get("response_format") or {}).get("type")
        system, prompt = flatten(payload.get("messages") or [])
        if args.emulate in ("deepseek", "groq"):
            if response_format == "json_schema":
                return self._send(400, {"error": {"message": "This response_format type is unavailable now", "type": "invalid_request_error"}})
            if response_format == "json_object" and "json" not in (system + prompt).lower():
                return self._send(400, {"error": {"message": "Prompt must contain the word 'json' in some form to use 'response_format' of type 'json_object'."}})
        if args.emulate == "groq" and response_format == "json_object" and payload.get("reasoning_format") == "raw":
            return self._send(400, {"error": {"message": "reasoning_format raw is not supported with JSON mode", "type": "invalid_request_error"}})
        if args.flaky_429 and random.random() < args.flaky_429:
            with LOCK:
                STATS["injected_429"] += 1
            return self._send(429, {"error": {"message": "rate limited (injected)"}}, {"Retry-After": "1"})
        model = payload.get("model") or args.model
        if args.tpm:
            rejection = tpm_admit(args.tpm, requested_tokens(payload), model)
            if rejection:
                return self._send(*rejection)
        if response_format in ("json_object", "json_schema"):
            system += "\n\nOutput format: respond with a single JSON object only."
        cli_model = args.cli_model or model
        started = time.monotonic()
        command = [
            args.claude_bin, "-p", "--output-format", "json", "--no-session-persistence",
            "--tools", "", "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--model", cli_model, "--system-prompt", system or "You are a helpful assistant.",
        ]
        try:
            proc = subprocess.run(
                command, input=prompt.encode("utf-8"), capture_output=True, timeout=args.timeout, cwd=self.server.workdir,
                env={k: v for k, v in os.environ.items() if k not in ("AI_API_KEY",)},
            )
        except subprocess.TimeoutExpired:
            with LOCK:
                STATS["errors"] += 1
            return self._send(504, {"error": {"message": "claude CLI timed out"}})
        try:
            result = json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError:
            result = None
        if proc.returncode != 0 or not result or result.get("is_error"):
            with LOCK:
                STATS["errors"] += 1
            log(f"claude error rc={proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[-300:]}")
            return self._send(502, {"error": {"message": "upstream claude CLI failure"}})
        usage = result.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
        completion_tokens = int(usage.get("output_tokens", 0))
        text = result.get("result") or ""
        with LOCK:
            STATS["calls"] += 1
            STATS["cost_usd"] += float(result.get("total_cost_usd") or 0.0)
            STATS["input_tokens"] += prompt_tokens
            STATS["output_tokens"] += completion_tokens
            cost = STATS["cost_usd"]
        elapsed = time.monotonic() - started
        log(f"call ok {elapsed:5.1f}s in={prompt_tokens} out={completion_tokens} fmt={response_format} total_cost=${cost:.2f}")
        if args.emulate == "groq" and response_format == "json_object":
            try:
                json.loads(text.strip())
            except ValueError:  # Groq validates JSON mode output server side and hands the text back
                log(f"groq json_validate_failed: {text[:80]!r}")
                return self._send(400, {"error": {"message": "Failed to generate JSON. Please adjust your prompt. See 'failed_generation' for more details.",
                                                  "type": "invalid_request_error", "code": "json_validate_failed", "failed_generation": text}})
        self._send(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "length" if result.get("stop_reason") == "max_tokens" else "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens},
        })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="claude-sonnet-4-6", help="model id reported to clients")
    parser.add_argument("--cli-model", default=None, help="override the model passed to the claude CLI")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--timeout", type=int, default=540)
    parser.add_argument("--token", default=None, help="required bearer token (defaults to $AI_API_KEY)")
    parser.add_argument("--emulate", choices=["none", "deepseek", "groq"], default="none")
    parser.add_argument("--flaky-429", type=float, default=0.0)
    parser.add_argument("--tpm", type=int, default=0, help="GroqCloud-style tokens-per-minute limit (0 = off)")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("the bridge binds to loopback only")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.args = args
    server.workdir = tempfile.mkdtemp(prefix="claude-bridge-")
    tpm = f", tpm={args.tpm}" if args.tpm else ""
    log(f"claude OpenAI bridge on http://{args.host}:{args.port}/v1 (emulate={args.emulate}, flaky_429={args.flaky_429}{tpm})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log(f"stats: {json.dumps(STATS)}")


if __name__ == "__main__":
    main()
