"""OpenAI-compatible LLM client. No model name is hardcoded.

The model receives structured market data and public search results only. It
never receives API keys, auth headers, or raw venue responses.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .util import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class LLMReply:
    content: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] | None = None
    finish_reason: str = ""  # "stop", "tool_calls", "length" ... as the provider reported it


class LLMUnavailable(RuntimeError):
    pass


class LLMTransient(LLMUnavailable):
    """A failure that is worth asking again: a timeout, a reset connection, HTTP 429 or 5xx.

    A completion has no side effect until the caller acts on it, so asking again is always safe."""


LLM_RETRIES = 2  # extra attempts after a transient failure (only while nothing was streamed to the app)
LLM_RETRY_DELAYS = (1.0, 3.0)
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524, 529}


class LLMContextOverflow(LLMUnavailable):
    """The provider refused the request because it is longer than the model's window.

    The request was rejected before anything was generated, so asking again with a smaller one is safe."""


_OVERFLOW_RE = re.compile(
    r"context[_ ]length|context window|maximum context|too many tokens|token limit|"
    r"prompt is too long|input is too long|reduce the length",
    re.IGNORECASE,
)


def http_error(status: int, body: str) -> LLMUnavailable:
    """The error for a failed LLM call; a 'too long' refusal gets its own type so callers can shrink and retry."""

    message = f"LLM HTTP {status}: {body[:200]}"
    if status == 413 or (status == 400 and _OVERFLOW_RE.search(body)):
        return LLMContextOverflow(message)
    if status in _TRANSIENT_STATUS:
        return LLMTransient(message)
    return LLMUnavailable(message)


def estimate_tokens(text: str) -> int:
    """Provider-neutral token estimate (no tokenizer dependency).

    ASCII costs about one token per four characters; anything else (Bengali, emoji, CJK) is
    counted at one token per character, which over-counts a little. Dense ASCII such as JSON with
    many digits and symbols costs more than four characters per token, so this can under-count it;
    the chat service covers that by compacting at 90% of the window, which leaves the margin.
    """

    if not text:
        return 0
    ascii_chars = len(text.encode("ascii", "ignore"))
    return -(-ascii_chars // 4) + (len(text) - ascii_chars)


def message_tokens(message: dict[str, Any]) -> int:
    """One chat message including its tool calls and the per-message framing overhead."""

    total = 4 + estimate_tokens(str(message.get("content") or ""))
    if message.get("tool_calls"):
        total += estimate_tokens(json.dumps(message["tool_calls"], default=str))
    return total


def messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(message_tokens(m) for m in messages)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_seconds
        self.api_key = __import__("os").environ.get("LLM_API_KEY", "").strip()
        self.enabled = bool(self.base_url and self.model)
        self.max_output_tokens = int(getattr(settings, "llm_max_output_tokens", 0) or 0)

    @property
    def name(self) -> str:
        return self.model or "unconfigured"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.1,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str = "auto",
        max_tokens: int | None = None,
    ) -> LLMReply:
        if not self.enabled:
            raise LLMUnavailable("LLM_BASE_URL and LLM_MODEL are not configured")
        url = self.base_url + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        cap = max_tokens if max_tokens is not None else self.max_output_tokens
        if cap:
            payload["max_tokens"] = cap
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        delivered = False  # once text reached the app, a retry would repeat it

        tracked: Callable[[str], Awaitable[None]] | None = None
        if on_delta is not None:
            sink = on_delta

            async def tracked(piece: str) -> None:
                nonlocal delivered
                delivered = True
                await sink(piece)

        attempt = 0
        while True:
            try:
                return await self._chat_once(url, payload, headers, tracked)
            except LLMTransient as exc:
                if delivered or attempt >= LLM_RETRIES:
                    raise
                delay = LLM_RETRY_DELAYS[min(attempt, len(LLM_RETRY_DELAYS) - 1)]
                attempt += 1
                logger.warning("llm_retry", attempt=attempt, error=str(exc)[:160], delay=delay)
                await asyncio.sleep(delay)

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.timeout, connect=min(15.0, float(self.timeout)))

    async def _chat_once(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_delta: Callable[[str], Awaitable[None]] | None,
    ) -> LLMReply:
        try:
            if on_delta is not None:
                streamed = await self._chat_stream(url, payload, headers, on_delta)
                if streamed is not None:
                    return streamed
                # The provider refused streaming before sending anything: use the plain call.
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TransportError as exc:  # timeout, reset connection, DNS, broken stream
            raise LLMTransient(f"LLM connection failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise http_error(response.status_code, response.text)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMTransient("LLM returned a body that is not JSON") from exc
        try:
            first = data["choices"][0]
            choice = first["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable("LLM response missing choices[0].message") from exc
        calls: list[dict[str, Any]] = []
        for n, call in enumerate(choice.get("tool_calls") or []):
            function = call.get("function") or {}
            calls.append(
                {
                    "id": call.get("id") or f"call_{n}",
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", "{}"),
                }
            )
        return LLMReply(
            content=str(choice.get("content") or ""),
            tool_calls=tuple(calls),
            raw=data,
            finish_reason=str(first.get("finish_reason") or ""),
        )

    async def _chat_stream(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_delta: Callable[[str], Awaitable[None]],
    ) -> LLMReply | None:
        """OpenAI-style SSE completion. Returns None if the provider rejects `stream` up front."""
        content: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason = ""
        async with (
            httpx.AsyncClient(timeout=self._timeout()) as client,
            client.stream("POST", url, json={**payload, "stream": True}, headers=headers) as response,
        ):
            if response.status_code in (400, 404, 405, 422):
                return None
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                raise http_error(response.status_code, body)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):  # the provider failed after it had already answered 200
                    err = chunk["error"]
                    detail = err.get("message") if isinstance(err, dict) else err
                    raise LLMTransient(f"LLM stream error: {str(detail)[:200]}")
                choices = chunk.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                text = delta.get("content")
                if text:
                    content.append(str(text))
                    await on_delta(str(text))
                for call in delta.get("tool_calls") or []:
                    # Providers number parallel calls with `index`; some omit it or repeat 0, so a new
                    # id on an occupied slot also starts a new call instead of gluing two together.
                    key = call.get("index")
                    key = key if isinstance(key, int) else (max(calls) if calls else 0)
                    new_id = call.get("id") or ""
                    if new_id and key in calls and calls[key]["id"] and calls[key]["id"] != new_id:
                        key = max(calls) + 1
                    slot = calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
                    function = call.get("function") or {}
                    slot["id"] = new_id or slot["id"]
                    slot["name"] = function.get("name") or slot["name"]
                    slot["arguments"] += function.get("arguments") or ""
        tool_calls = tuple(
            {"id": c["id"] or f"call_{n}", "name": c["name"], "arguments": c["arguments"] or "{}"}
            for n, (_, c) in enumerate(sorted(calls.items()))
            if c["name"]
        )
        return LLMReply(content="".join(content), tool_calls=tool_calls, finish_reason=finish_reason)


SEARCH_QUOTA_PAUSE = 900  # seconds to leave the search provider alone after a key/quota rejection
SEARCH_RATE_LIMIT_PAUSE = 60


class WebSearchClient:
    """Small Tavily-compatible web-search tool used by research."""

    def __init__(self) -> None:
        import os

        self.api_key = os.environ.get("TAVILY_API_KEY", "").strip()
        self.endpoint = os.environ.get("WEB_SEARCH_URL", "https://api.tavily.com/search")
        # When the provider rejects the key or the plan quota (401/403/432) or rate-limits us (429), stop
        # calling it for a while instead of failing once per market, every cycle.
        self._blocked_until = 0.0
        self.blocked_status: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def blocked_seconds(self) -> int:
        return max(0, int(self._blocked_until - time.monotonic()))

    @property
    def enabled(self) -> bool:
        return self.configured and self.blocked_seconds == 0

    def _block(self, status: int) -> None:
        self.blocked_status = status
        self._blocked_until = time.monotonic() + (SEARCH_RATE_LIMIT_PAUSE if status == 429 else SEARCH_QUOTA_PAUSE)

    async def search(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        if not self.enabled:
            return []
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                self.endpoint,
                json={
                    "api_key": self.api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                },
            )
        if response.status_code >= 400:
            if response.status_code in (401, 403, 429, 432):
                self._block(response.status_code)
                logger.warning(
                    "web_search_paused",
                    status=response.status_code,
                    pause_seconds=self.blocked_seconds,
                    hint="Tavily key invalid or plan/usage limit reached: check the Tavily dashboard",
                    body=response.text[:200],
                )
            else:
                logger.warning("web_search_failed", status=response.status_code)
            return []
        self.blocked_status = None
        data = response.json()
        out: list[dict[str, str]] = []
        for row in data.get("results") or []:
            url = str(row.get("url") or "")
            if not url.startswith(("https://", "http://")):
                continue
            out.append(
                {
                    "title": str(row.get("title") or ""),
                    "url": url,
                    "content": str(row.get("content") or ""),
                }
            )
        return out


def parse_json_block(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM did not return a JSON object") from None
        data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("LLM JSON is not an object")
    return data
