"""Streaming model clients for the campaign copilot.

Two providers, both real and both tested from this repo:

  * Google Gemini  -- `generativelanguage.googleapis.com`, SSE streaming.
  * Moonshot Kimi  -- OpenAI-compatible `/chat/completions`, SSE streaming.

There is deliberately no OpenAI or Anthropic client here: I had no working key for
either while building this, so I did not write code I could not run.

Both clients normalise to the same chunk protocol so the agent loop is provider-agnostic:

    {"type": "text",      "delta": str}
    {"type": "tool_call", "id": str, "name": str, "args": dict}
    {"type": "turn",      "message": <provider-native assistant turn to append to history>}
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

GEMINI_MODELS = {"gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview",
                 "gemini-3-pro-preview", "gemini-2.5-pro"}
MOONSHOT_MODELS = {"kimi-k3", "kimi-k2.7-code"}


class LLMError(RuntimeError):
    pass


def provider_for(model: str) -> str:
    if model in GEMINI_MODELS:
        return "gemini"
    if model in MOONSHOT_MODELS:
        return "moonshot"
    raise LLMError(f"unknown model '{model}'")


# ---------------------------------------------------------------- Gemini

async def _stream_gemini(model: str, system: str, history: list[dict],
                         tool_schemas: list[dict]) -> AsyncIterator[dict[str, Any]]:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set")

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":streamGenerateContent?alt=sse&key={key}")
    body = {
        "contents": history,
        "tools": [{"functionDeclarations": tool_schemas}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {"temperature": 0.7},
    }

    parts_out: list[dict] = []
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream("POST", url, json=body) as resp:
            if resp.status_code != 200:
                raise LLMError(f"gemini {resp.status_code}: {(await resp.aread()).decode()[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                chunk = json.loads(line[6:])
                for cand in chunk.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if "text" in part and part["text"]:
                            parts_out.append(part)
                            yield {"type": "text", "delta": part["text"]}
                        elif "functionCall" in part:
                            # Gemini 3.x returns a thoughtSignature alongside function
                            # calls that MUST be echoed back in history or the next turn
                            # 400s. Keep the part verbatim.
                            parts_out.append(part)
                            fc = part["functionCall"]
                            yield {"type": "tool_call",
                                   "id": fc.get("id") or fc["name"],
                                   "name": fc["name"],
                                   "args": fc.get("args") or {}}

    yield {"type": "turn", "message": {"role": "model", "parts": parts_out or [{"text": ""}]}}


def _gemini_user_turn(text: str) -> dict:
    return {"role": "user", "parts": [{"text": text}]}


def _gemini_tool_result(name: str, result: Any) -> dict:
    return {"role": "user", "parts": [{"functionResponse": {"name": name, "response": {"result": result}}}]}


# ---------------------------------------------------------------- Moonshot / Kimi

async def _stream_moonshot(model: str, system: str, history: list[dict],
                           tool_schemas: list[dict]) -> AsyncIterator[dict[str, Any]]:
    key = os.environ.get("MOONSHOT_API_KEY")
    if not key:
        raise LLMError("MOONSHOT_API_KEY is not set")

    tools = [{"type": "function", "function": t} for t in tool_schemas]
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + history,
        "tools": tools,
        "stream": True,
    }
    # kimi-k3 rejects any temperature other than 1, so only send one where it is allowed.
    if model != "kimi-k3":
        body["temperature"] = 0.7

    text_acc: list[str] = []
    calls: dict[int, dict] = {}
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream("POST", "https://api.moonshot.ai/v1/chat/completions",
                                 json=body,
                                 headers={"Authorization": f"Bearer {key}"}) as resp:
            if resp.status_code != 200:
                raise LLMError(f"moonshot {resp.status_code}: {(await resp.aread()).decode()[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        text_acc.append(delta["content"])
                        yield {"type": "text", "delta": delta["content"]}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = calls.setdefault(idx, {"id": None, "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]

    tool_calls = []
    for idx in sorted(calls):
        slot = calls[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        call_id = slot["id"] or f"call_{idx}"
        tool_calls.append({"id": call_id, "type": "function",
                           "function": {"name": slot["name"], "arguments": slot["args"] or "{}"}})
        yield {"type": "tool_call", "id": call_id, "name": slot["name"], "args": args}

    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_acc) or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    yield {"type": "turn", "message": msg}


def _moonshot_user_turn(text: str) -> dict:
    return {"role": "user", "content": text}


def _moonshot_tool_result(call_id: str, result: Any) -> dict:
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(result) if not isinstance(result, str) else result}


# ---------------------------------------------------------------- facade

class LLM:
    """Provider-agnostic streaming facade used by the agent loop."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.provider = provider_for(model)

    def stream(self, system: str, history: list[dict],
               tool_schemas: list[dict]) -> AsyncIterator[dict[str, Any]]:
        if self.provider == "gemini":
            return _stream_gemini(self.model, system, history, tool_schemas)
        return _stream_moonshot(self.model, system, history, tool_schemas)

    def user_turn(self, text: str) -> dict:
        return _gemini_user_turn(text) if self.provider == "gemini" else _moonshot_user_turn(text)

    def tool_result_turn(self, call_id: str, name: str, result: Any) -> dict:
        if self.provider == "gemini":
            return _gemini_tool_result(name, result)
        return _moonshot_tool_result(call_id, result)
