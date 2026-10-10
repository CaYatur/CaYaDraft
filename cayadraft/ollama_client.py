"""Local Ollama chat client. The issue text never leaves this machine.

A total request timeout kills a slow but working model and looks like a dropped
connection. The read budget is the gap between streamed tokens, the same idea as
the idle timeout in cayadev-ai-hub.
"""

from __future__ import annotations

import json
import re

import httpx

from cayadraft.textutil import strip_think

_TOOL_BLOCK = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL | re.IGNORECASE)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str, model: str, timeout: float, num_ctx: int) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx

    def complete(self, *, system: str, user: str, temperature: float, num_predict: int) -> str:
        payload = {
            "model": self.model,
            "stream": True,
            "think": False,
            "messages": [
                {"role": "system", "content": f"{system.rstrip()}\n/nothink"},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
                "num_ctx": self.num_ctx,
            },
        }
        timeout = httpx.Timeout(connect=10.0, read=self.timeout, write=60.0, pool=10.0)
        parts: list[str] = []
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream("POST", f"{self.host}/api/chat", json=payload) as response:
                    if response.status_code != 200:
                        detail = response.read().decode("utf-8", "replace")[:300]
                        raise OllamaError(f"Ollama returned HTTP {response.status_code}. {detail}".strip())
                    for line in response.iter_lines():
                        piece = _stream_piece(line)
                        if piece:
                            parts.append(piece)
        except httpx.ConnectError as exc:
            raise OllamaError(f"Ollama is not reachable at {self.host}.") from exc
        except httpx.TimeoutException as exc:
            partial = strip_think("".join(parts))
            if partial:
                return partial
            raise OllamaError(
                f"Ollama sent nothing for {int(self.timeout)} seconds. The model stalled. It is still at {self.host}."
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama is not reachable at {self.host}.") from exc
        content = strip_think("".join(parts))
        if not content:
            raise OllamaError("Ollama returned an empty response.")
        return content

    def status(self) -> tuple[bool, str]:
        try:
            response = httpx.get(f"{self.host}/api/tags", timeout=5)
        except httpx.HTTPError as exc:
            return False, f"Ollama is not reachable ({exc.__class__.__name__})."
        if response.status_code != 200:
            return False, f"Ollama /api/tags returned HTTP {response.status_code}."
        names = [item.get("name", "") for item in response.json().get("models", [])]
        bare = self.model.split(":")[0]
        found = any(name == self.model or name.split(":")[0] == bare for name in names)
        if not found:
            return False, f"Model `{self.model}` is not pulled. Run: ollama pull {self.model}"
        return True, f"Model `{self.model}` is available."

    def chat(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        temperature: float,
        num_predict: int,
    ) -> dict:
        """One model turn. Tool calls are returned separately from the visible text."""
        payload = {
            "model": self.model,
            "stream": True,
            "think": False,
            "messages": [{"role": "system", "content": f"{system.rstrip()}\n/nothink"}, *messages],
            "tools": tools,
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
                "num_ctx": self.num_ctx,
            },
        }
        timeout = httpx.Timeout(connect=10.0, read=self.timeout, write=60.0, pool=10.0)
        raw_lines: list[str] = []
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream("POST", f"{self.host}/api/chat", json=payload) as response:
                    if response.status_code != 200:
                        detail = response.read().decode("utf-8", "replace")[:300]
                        raise OllamaError(f"Ollama returned HTTP {response.status_code}. {detail}".strip())
                    for line in response.iter_lines():
                        if (line or "").strip():
                            raw_lines.append(line)
        except httpx.ConnectError as exc:
            raise OllamaError(f"Ollama is not reachable at {self.host}.") from exc
        except httpx.TimeoutException as exc:
            partial = collect_chat(raw_lines)
            if partial["content"] or partial["tool_calls"]:
                return partial
            raise OllamaError(
                f"Ollama sent nothing for {int(self.timeout)} seconds. The model stalled. It is still at {self.host}."
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama is not reachable at {self.host}.") from exc
        result = collect_chat(raw_lines)
        if not result["content"] and not result["tool_calls"]:
            raise OllamaError("Ollama returned an empty response.")
        return result


def collect_chat(lines: list[str]) -> dict:
    """Join streamed chat chunks, including a tool call that arrives in pieces."""
    content: list[str] = []
    calls: list[dict] = []
    for line in lines:
        text = (line or "").strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        message = item.get("message") if isinstance(item, dict) else None
        if not isinstance(message, dict):
            continue
        piece = message.get("content") or ""
        if piece:
            content.append(str(piece))
        for index, call in enumerate(message.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            while len(calls) <= index:
                calls.append({"name": "", "arguments": ""})
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or "")
            if name:
                calls[index]["name"] = name
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                calls[index]["arguments"] = arguments
            elif isinstance(arguments, str) and arguments:
                current = calls[index]["arguments"]
                calls[index]["arguments"] = arguments if isinstance(current, dict) else f"{current}{arguments}"
    visible = strip_think("".join(content)).strip()
    parsed = [_normalize_call(call) for call in calls]
    parsed = [call for call in parsed if call["name"]]
    if not parsed:
        parsed = tool_calls_in_text(visible)
        if parsed:
            visible = _TOOL_BLOCK.sub("", visible).strip()
    return {"content": visible, "tool_calls": parsed}


def tool_calls_in_text(content: str) -> list[dict]:
    """Read a `<tool_call>{...}</tool_call>` block when the model writes the call as text."""
    found = []
    for raw in _TOOL_BLOCK.findall(content or ""):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(item.get("name") or function.get("name") or "")
        arguments = item.get("arguments")
        if arguments is None:
            arguments = function.get("arguments")
        found.append(_normalize_call({"name": name, "arguments": arguments if arguments is not None else {}}))
    return [call for call in found if call["name"]]


def _normalize_call(call: dict) -> dict:
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return {"name": str(call.get("name") or ""), "arguments": arguments}


def _stream_piece(line: str) -> str:
    text = (line or "").strip()
    if not text:
        return ""
    try:
        item = json.loads(text)
    except json.JSONDecodeError:
        return ""
    message = item.get("message") if isinstance(item, dict) else None
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")
