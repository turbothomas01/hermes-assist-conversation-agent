#!/usr/bin/env python3
"""Small HTTP bridge from Home Assistant Assist to Hermes Agent.

Endpoints:
- GET /health
- POST /api/chat with an Authorization header containing the bridge API key

This bridge is optional. HACS installs only the Home Assistant custom
integration; run this bridge separately on the machine that has Hermes Agent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.environ.get("HERMES_ASSIST_HOST", "127.0.0.1")
PORT = int(os.environ.get("HERMES_ASSIST_PORT", "8765"))
KEY_FILE = Path(os.environ.get("HERMES_ASSIST_KEY_FILE", "./hermes-assist-bridge.key"))
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")
HERMES_REPO = os.environ.get("HERMES_REPO", "")
HERMES_ENV_FILE = os.environ.get("HERMES_ENV_FILE", "")
TIMEOUT = int(os.environ.get("HERMES_ASSIST_TIMEOUT", "120"))
MAX_PROMPT_CHARS = int(os.environ.get("HERMES_ASSIST_MAX_PROMPT_CHARS", "6000"))
MAX_HISTORY_MESSAGES = int(os.environ.get("HERMES_ASSIST_MAX_HISTORY_MESSAGES", "8"))
USE_DIRECT_AGENT = os.environ.get("HERMES_ASSIST_USE_DIRECT_AGENT", "0").lower() in {"1", "true", "yes"}
MODEL = os.environ.get("HERMES_ASSIST_MODEL", "")
PROVIDER = os.environ.get("HERMES_ASSIST_PROVIDER", "")
TOOLSETS = [x.strip() for x in os.environ.get("HERMES_ASSIST_TOOLSETS", "").split(",") if x.strip()]
MAX_TURNS = int(os.environ.get("HERMES_ASSIST_MAX_TURNS", "8"))
REASONING_EFFORT = os.environ.get("HERMES_ASSIST_REASONING", "minimal")

SYSTEM_PROMPT = """You are Hermes Agent answering through Home Assistant Assist.
Keep replies concise and speech-friendly. Avoid Markdown tables and visual-only instructions.
Use the recent conversation context to resolve pronouns and follow-up questions.
Safety: do not unlock doors, disable alarms, delete/send email, or make broad smart-home changes unless the spoken request is explicit and unambiguous. If unsure, ask a short clarifying question.
For ordinary device-control phrases that Home Assistant should handle directly, say briefly that this command should be routed to Home Assistant's built-in agent unless you can safely complete it with your available tools.
"""

_AGENT = None
_AGENT_LOCK = threading.Lock()
_AGENT_READY = False
_AGENT_ERROR: str | None = None


def load_env_file(path: str) -> None:
    """Load simple KEY=VALUE secrets into the bridge process environment."""
    if not path:
        return
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


load_env_file(HERMES_ENV_FILE)


def load_key() -> str:
    try:
        return KEY_FILE.expanduser().read_text().strip()
    except FileNotFoundError:
        return os.environ.get("HERMES_ASSIST_API_KEY", "").strip()


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _content_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                txt = part.get("text") or part.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
        return " ".join(parts).strip()
    return ""


def format_history(data: dict[str, Any], current_text: str) -> str:
    """Extract concise previous chat context from Home Assistant's Assist chat log."""
    chat_log = data.get("chat_log")
    if not isinstance(chat_log, dict):
        return ""
    items = chat_log.get("content")
    if not isinstance(items, list):
        return ""

    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _content_text(item)
        if not text:
            continue
        if role == "user" and text.strip().lower() == current_text.strip().lower():
            continue
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {text[:600]}")

    if not lines:
        return ""
    return "Recent conversation, oldest to newest:\n" + "\n".join(lines[-MAX_HISTORY_MESSAGES:]) + "\n"


def get_agent():
    """Create one in-process Hermes agent and reuse it across voice requests."""
    global _AGENT, _AGENT_READY, _AGENT_ERROR
    if _AGENT is not None:
        return _AGENT
    with _AGENT_LOCK:
        if _AGENT is not None:
            return _AGENT
        try:
            if HERMES_REPO:
                sys.path.insert(0, HERMES_REPO)
                os.chdir(HERMES_REPO)
            from hermes_constants import parse_reasoning_effort
            from run_agent import AIAgent

            kwargs: dict[str, Any] = {
                "enabled_toolsets": TOOLSETS or None,
                "max_iterations": MAX_TURNS,
                "quiet_mode": True,
                "skip_context_files": True,
                "skip_memory": True,
                "reasoning_config": parse_reasoning_effort(REASONING_EFFORT),
                "platform": "homeassistant",
            }
            if MODEL:
                kwargs["model"] = MODEL
            if PROVIDER:
                kwargs["provider"] = PROVIDER
            _AGENT = AIAgent(**kwargs)
            _AGENT_READY = True
            _AGENT_ERROR = None
            return _AGENT
        except Exception as exc:  # noqa: BLE001 - fallback to CLI path
            _AGENT_ERROR = str(exc)
            _AGENT_READY = False
            raise


def call_hermes_direct(prompt: str) -> tuple[bool, str]:
    """Call the in-process Hermes agent. Returns (ok, text)."""
    try:
        agent = get_agent()
        with _AGENT_LOCK:
            return True, (agent.chat(prompt) or "").strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def call_hermes_cli(prompt: str) -> tuple[bool, str]:
    """Fallback CLI call, slower but simple and robust."""
    cmd = [HERMES_BIN, "chat", "-q", prompt, "--source", "home-assistant-assist", "-Q", "--max-turns", str(MAX_TURNS)]
    if PROVIDER:
        cmd += ["--provider", PROVIDER]
    if MODEL:
        cmd += ["-m", MODEL]
    if TOOLSETS:
        cmd += ["--toolsets", ",".join(TOOLSETS)]
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "Hermes exited with an error")[-2000:]
    return True, (proc.stdout or "").strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesAssistBridge/1.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            json_response(self, 200, {"ok": True, "service": "hermes-assist-bridge", "time": time.time(), "direct_agent": USE_DIRECT_AGENT, "agent_ready": _AGENT_READY, "agent_error": _AGENT_ERROR, "model": MODEL, "provider": PROVIDER, "toolsets": TOOLSETS, "max_turns": MAX_TURNS, "reasoning": REASONING_EFFORT})
        else:
            json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/chat":
            json_response(self, 404, {"error": "not_found"})
            return

        expected = load_key()
        auth = self.headers.get("Authorization", "")
        if not expected or auth != f"Bearer {expected}":
            json_response(self, 401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(min(length, 1024 * 1024))
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            json_response(self, 400, {"error": "bad_json", "detail": str(exc)})
            return

        text = str(data.get("text", "")).strip()
        if not text:
            json_response(self, 400, {"error": "missing_text"})
            return

        conversation_id = str(data.get("conversation_id") or "")
        language = str(data.get("language") or "")
        device_id = str(data.get("device_id") or "")
        extra_system_prompt = str(data.get("extra_system_prompt") or "")
        history = format_history(data, text)

        prompt = (
            f"{SYSTEM_PROMPT}\n"
            f"Home Assistant context:\n"
            f"- conversation_id: {conversation_id or 'none'}\n"
            f"- language: {language or 'unknown'}\n"
            f"- device_id: {device_id or 'unknown'}\n"
        )
        if extra_system_prompt:
            prompt += f"- extra Home Assistant instruction: {extra_system_prompt[:1000]}\n"
        if history:
            prompt += f"\n{history}"
        prompt += f"\nUser said: {text}\n\nReply with only the answer the user should hear."
        prompt = prompt[:MAX_PROMPT_CHARS]

        ok, output = call_hermes_direct(prompt) if USE_DIRECT_AGENT else call_hermes_cli(prompt)
        if USE_DIRECT_AGENT and not ok:
            ok, output = call_hermes_cli(prompt)

        if not ok:
            if output == "timeout":
                json_response(self, 504, {"error": "timeout", "reply": "Sorry, Hermes took too long to answer."})
            else:
                json_response(self, 502, {"error": "hermes_failed", "detail": output[-2000:], "reply": "Sorry, Hermes failed."})
            return

        output = output.strip() or "Hermes did not return a response."
        json_response(self, 200, {"reply": output, "conversation_id": conversation_id or None})


def main() -> None:
    if USE_DIRECT_AGENT:
        try:
            get_agent()
        except Exception as exc:  # noqa: BLE001
            print(f"Direct Hermes agent unavailable, CLI fallback will be used: {exc}", file=sys.stderr, flush=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Hermes Assist Bridge listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
