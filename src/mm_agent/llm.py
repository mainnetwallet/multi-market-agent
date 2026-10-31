"""OpenAI-compatible LLM client. No model name is hardcoded.

The model receives structured market data and public search results only. It
never receives API keys, auth headers, or raw venue responses.
"""

from __future__ import annotations

import json
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


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_seconds
        self.api_key = __import__("os").environ.get("LLM_API_KEY", "").strip()
        self.enabled = bool(self.base_url and self.model)

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
    ) -> LLMReply:
        if not self.enabled:
            raise LLMUnavailable("LLM_BASE_URL and LLM_MODEL are not configured")
        url = self.base_url + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if on_delta is not None:
            streamed = await self._chat_stream(url, payload, headers, on_delta)
            if streamed is not None:
                return streamed
            # The provider refused streaming before sending anything: use the plain call.
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise LLMUnavailable(f"LLM HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMUnavailable("LLM response missing choices[0].message") from exc
        calls: list[dict[str, Any]] = []
        for call in choice.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append(
                {
                    "id": call.get("id", ""),
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", "{}"),
                }
            )
        return LLMReply(content=str(choice.get("content") or ""), tool_calls=tuple(calls), raw=data)

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
        async with (
            httpx.AsyncClient(timeout=self.timeout) as client,
            client.stream("POST", url, json={**payload, "stream": True}, headers=headers) as response,
        ):
            if response.status_code in (400, 404, 405, 422):
                return None
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                raise LLMUnavailable(f"LLM HTTP {response.status_code}: {body[:200]}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                text = delta.get("content")
                if text:
                    content.append(str(text))
                    await on_delta(str(text))
                for call in delta.get("tool_calls") or []:
                    slot = calls.setdefault(int(call.get("index", 0)), {"id": "", "name": "", "arguments": ""})
                    function = call.get("function") or {}
                    slot["id"] = call.get("id") or slot["id"]
                    slot["name"] = function.get("name") or slot["name"]
                    slot["arguments"] += function.get("arguments") or ""
        tool_calls = tuple(
            {"id": c["id"], "name": c["name"], "arguments": c["arguments"] or "{}"}
            for _, c in sorted(calls.items())
            if c["name"]
        )
        return LLMReply(content="".join(content), tool_calls=tool_calls)


class WebSearchClient:
    """Small Tavily-compatible web-search tool used by research."""

    def __init__(self) -> None:
        import os

        self.api_key = os.environ.get("TAVILY_API_KEY", "").strip()
        self.endpoint = os.environ.get("WEB_SEARCH_URL", "https://api.tavily.com/search")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
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
            logger.warning("web_search_failed", status=response.status_code)
            return []
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
                    "content": str(row.get("content") or "")[:1500],
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
