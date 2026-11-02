"""The chat tool loop must always end in an answer, stay fast, and survive a dropped connection."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from mm_agent import llm as llm_module
from mm_agent.chat import clip_chars
from mm_agent.llm import LLMClient, LLMReply, LLMTransient, LLMUnavailable
from tests.conftest import Harness, make_settings
from tests.test_api import _sse, make_client

OWNER = "web:operator"


class _Script:
    """A model double: `plan(round_number, messages, tool_choice)` decides each reply."""

    enabled = True

    def __init__(self, plan: Callable[[int, list[dict[str, Any]], str], LLMReply]) -> None:
        self.plan = plan
        self.requests: list[dict[str, Any]] = []

    async def chat(  # type: ignore[no-untyped-def]
        self, messages, tools=None, temperature=0.1, on_delta=None, tool_choice="auto"
    ):
        self.requests.append({"messages": [dict(m) for m in messages], "tool_choice": tool_choice})
        return self.plan(len(self.requests), messages, tool_choice)


def _set(harness: Harness, **values: Any) -> None:
    """Settings are frozen: give the chat service a modified copy."""

    harness.chat.settings = harness.chat.settings.model_copy(update=values)


def _call(name: str = "get_status", ident: str = "c1") -> dict[str, str]:
    return {"id": ident, "name": name, "arguments": "{}"}


async def _ask(harness: Harness, text: str = "which market is good?", **kw: Any) -> dict[str, Any]:
    cid = harness.agent.store.chat_create(OWNER)["id"]
    return await harness.chat.handle(text, actor=OWNER, conversation_id=cid, **kw)


# ----------------------------------------------------------------- the loop always ends in an answer
async def test_the_round_budget_ends_with_a_final_answer(harness: Harness) -> None:
    _set(harness, llm_max_tool_rounds=4, llm_stall_rounds=0)

    def plan(n: int, _m: list[dict[str, Any]], tool_choice: str) -> LLMReply:
        if tool_choice == "none":
            return LLMReply(content="Top 3: A, B, C.")
        return LLMReply(content="", tool_calls=(_call(ident=f"c{n}"),))

    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    result = await _ask(harness)
    assert result["reply"] == "Top 3: A, B, C."
    assert [r["tool_choice"] for r in model.requests] == ["auto"] * 4 + ["none"]  # exactly 4 rounds, then forced
    system_before_limit = model.requests[3]["messages"][0]["content"]
    assert "ROUND BUDGET" in system_before_limit and "FINAL ANSWER REQUIRED" not in system_before_limit
    assert "FINAL ANSWER REQUIRED" in model.requests[4]["messages"][0]["content"]


async def test_tool_rounds_are_dynamic_when_every_call_is_new(harness: Harness) -> None:
    """No fixed cap: a job that keeps asking for new information runs as many rounds as it needs."""

    assert harness.chat.settings.llm_max_tool_rounds == 0
    needed = 30  # well above the old fixed limit of 14

    def plan(n: int, _m: list[dict[str, Any]], tool_choice: str) -> LLMReply:
        if n > needed:
            return LLMReply(content="Done after many rounds.")
        call = {"id": f"c{n}", "name": "get_status", "arguments": json.dumps({"i": n})}
        return LLMReply(content="", tool_calls=(call,))

    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    result = await _ask(harness)
    assert result["reply"] == "Done after many rounds."
    assert len(model.requests) == needed + 1
    assert all(r["tool_choice"] == "auto" for r in model.requests)


async def test_a_loop_of_repeated_calls_is_warned_then_forced_to_answer(harness: Harness) -> None:
    _set(harness, llm_stall_rounds=3)

    def plan(n: int, _m: list[dict[str, Any]], tool_choice: str) -> LLMReply:
        if tool_choice == "none":
            return LLMReply(content="Best guess from what I have.")
        return LLMReply(content="", tool_calls=(_call(ident=f"c{n}"),))  # same call every round

    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    result = await _ask(harness)
    assert result["reply"] == "Best guess from what I have."
    # round 1 is new; rounds 2, 3, 4 repeat it, so the 5th request is the forced answer
    assert [r["tool_choice"] for r in model.requests] == ["auto"] * 4 + ["none"]
    assert "LOOP WARNING" in model.requests[3]["messages"][0]["content"]
    assert "FINAL ANSWER REQUIRED" in model.requests[4]["messages"][0]["content"]


async def test_a_new_call_after_a_repeat_clears_the_loop_warning(harness: Harness) -> None:
    _set(harness, llm_stall_rounds=3)
    script = [
        {"id": "a", "name": "get_status", "arguments": "{}"},
        {"id": "b", "name": "get_status", "arguments": "{}"},  # repeat: stalled = 1
        {"id": "c", "name": "get_status", "arguments": "{}"},  # repeat: stalled = 2 -> warned
        {"id": "d", "name": "get_status", "arguments": json.dumps({"new": 1})},  # new: warning cleared
    ]

    def plan(n: int, _m: list[dict[str, Any]], tool_choice: str) -> LLMReply:
        if n > len(script):
            return LLMReply(content="Finished.")
        return LLMReply(content="", tool_calls=(script[n - 1],))

    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    result = await _ask(harness)
    assert result["reply"] == "Finished."
    assert "LOOP WARNING" in model.requests[3]["messages"][0]["content"]
    assert "LOOP WARNING" not in model.requests[4]["messages"][0]["content"]
    assert all(r["tool_choice"] == "auto" for r in model.requests)


async def test_a_model_that_ignores_tool_choice_none_still_gets_an_answer(harness: Harness) -> None:
    _set(harness, llm_max_tool_rounds=2, llm_stall_rounds=0)
    model = _Script(lambda n, _m, _t: LLMReply(content="", tool_calls=(_call(ident=f"c{n}"),)))
    harness.chat.llm = model  # type: ignore[assignment]
    result = await _ask(harness)
    assert "no answer" in result["reply"] and result["data"] is not None  # a message, not a hang
    assert len(model.requests) <= 5


async def test_an_empty_reply_is_asked_again_once_with_tools_off(harness: Harness) -> None:
    def plan(_n: int, _m: list[dict[str, Any]], tool_choice: str) -> LLMReply:
        if tool_choice == "none":
            return LLMReply(content="Here is the answer.")
        return LLMReply(content="", finish_reason="length")

    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    result = await _ask(harness)
    assert result["reply"] == "Here is the answer."
    assert [r["tool_choice"] for r in model.requests] == ["auto", "none"]


async def test_an_empty_reply_that_stays_empty_says_so(harness: Harness) -> None:
    model = _Script(lambda *_: LLMReply(content="", finish_reason="length"))
    harness.chat.llm = model  # type: ignore[assignment]
    result = await _ask(harness)
    assert "no answer" in result["reply"] and "output limit" in result["reply"]
    assert len(model.requests) == 2  # one retry, no endless loop


# --------------------------------------------------------------------------- speed
async def test_independent_reads_run_side_by_side_and_keep_their_order(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = {"now": 0, "peak": 0}

    async def slow(name: str, args: Any, **_: Any) -> dict[str, Any]:
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.05)
        running["now"] -= 1
        return {"ok": True, "tool": name}

    monkeypatch.setattr(harness.chat, "_execute_tool", slow)

    def plan(n: int, _m: list[dict[str, Any]], _t: str) -> LLMReply:
        if n == 1:
            return LLMReply(
                content="",
                tool_calls=(_call("get_status", "a"), _call("get_positions", "b"), _call("get_orders", "c")),
            )
        return LLMReply(content="done")

    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    await _ask(harness)
    assert running["peak"] == 3
    tool_messages = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["a", "b", "c"]  # results keep the call order


async def test_money_and_control_calls_never_run_in_parallel(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    running = {"now": 0, "peak": 0}

    async def slow(name: str, args: Any, **_: Any) -> dict[str, Any]:
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.03)
        running["now"] -= 1
        return {"ok": True}

    monkeypatch.setattr(harness.chat, "_execute_tool", slow)
    plan = lambda n, _m, _t: (  # noqa: E731
        LLMReply(content="", tool_calls=(_call("get_status", "a"), _call("pause_agent", "b")))
        if n == 1
        else LLMReply(content="done")
    )
    harness.chat.llm = _Script(plan)  # type: ignore[assignment]
    await _ask(harness)
    assert running["peak"] == 1


async def test_a_huge_tool_result_is_cut_before_it_goes_back_to_the_model(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set(harness, llm_tool_result_chars=2000)

    async def big(*_: Any, **__: Any) -> dict[str, Any]:
        return {"ok": True, "rows": ["x" * 50 for _ in range(2000)]}

    monkeypatch.setattr(harness.chat, "_run_tool", big)
    plan = lambda n, _m, _t: (  # noqa: E731
        LLMReply(content="", tool_calls=(_call(),)) if n == 1 else LLMReply(content="ok")
    )
    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    await _ask(harness)
    sent = next(m for m in model.requests[1]["messages"] if m["role"] == "tool")["content"]
    assert len(sent) < 2400 and "omitted" in sent and sent.startswith("DATA: ")


def test_clip_chars_keeps_head_and_tail_and_zero_means_unlimited() -> None:
    text = "A" * 500 + "B" * 500
    clipped = clip_chars(text, 300)
    assert len(clipped) <= 400 and clipped.startswith("A") and clipped.endswith("B") and "omitted" in clipped
    assert clip_chars(text, 0) == text and clip_chars("short", 300) == "short"


async def test_read_tools_are_not_saved_to_the_database_one_by_one(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    saves = {"n": 0}
    real = harness.chat._save_checkpoint

    def counting(scope: Any) -> None:
        saves["n"] += 1
        real(scope)

    monkeypatch.setattr(harness.chat, "_save_checkpoint", counting)
    reads = tuple(_call(n, f"r{i}") for i, n in enumerate(["get_status", "get_positions", "get_orders"] * 3))
    plan = lambda n, _m, _t: LLMReply(content="", tool_calls=reads) if n == 1 else LLMReply(content="done")  # noqa: E731
    harness.chat.llm = _Script(plan)  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    await harness.chat.handle("how are we?", actor=OWNER, conversation_id=cid)
    assert saves["n"] <= 2  # the start and the end of the answer, not one per read (was 9 more)
    calls = store.chat_get(OWNER, cid)["state"]["checkpoint"]["calls"]
    assert len(calls) == 9 and all(c["result"] == "" for c in calls)  # noted, but no result payload is stored


# ---------------------------------------------------------------- a dropped app is not a Stop
async def test_a_disconnected_app_still_gets_a_finished_read_only_answer(harness: Harness) -> None:
    plan = lambda n, _m, _t: (  # noqa: E731
        LLMReply(content="", tool_calls=(_call("pause_agent", "p"), _call("get_status", "s")))
        if n == 1
        else LLMReply(content="Read-only answer.")
    )
    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    result = await harness.chat.handle(
        "pause and tell me", actor=OWNER, conversation_id=cid, should_stop=lambda: False, is_detached=lambda: True
    )
    assert result["reply"] == "Read-only answer."
    assert harness.agent.status()["paused"] is False  # the control call was refused while nobody watched
    tool_replies = {m["tool_call_id"]: m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool"}
    assert "disconnected" in tool_replies["p"] and "disconnected" not in tool_replies["s"]
    assert store.chat_messages(OWNER, cid)[-1]["text"] == "Read-only answer."  # stored for the app to show


async def test_text_from_each_round_starts_a_new_paragraph(harness: Harness) -> None:
    class Talker(_Script):
        async def chat(self, messages, tools=None, temperature=0.1, on_delta=None, tool_choice="auto"):  # type: ignore[no-untyped-def]
            n = len(self.requests) + 1
            if on_delta is not None:
                await on_delta("Checking status." if n == 1 else "Final answer.")
            return await super().chat(messages, tools, temperature, on_delta, tool_choice)

    plan = lambda n, _m, _t: (  # noqa: E731
        LLMReply(content="Checking status.", tool_calls=(_call(),)) if n == 1 else LLMReply(content="Final answer.")
    )
    harness.chat.llm = Talker(plan)  # type: ignore[assignment]
    seen: list[str] = []

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        if kind == "delta":
            seen.append(str(payload["text"]))

    await _ask(harness, on_event=on_event)
    assert "".join(seen) == "Checking status.\n\nFinal answer."


def test_explicit_stop_endpoint_stops_a_running_answer_and_is_private(harness: Harness) -> None:
    started, release = threading.Event(), threading.Event()

    class Blocking(_Script):
        async def chat(self, messages, tools=None, temperature=0.1, on_delta=None, tool_choice="auto"):  # type: ignore[no-untyped-def]
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            return await super().chat(messages, tools, temperature, on_delta, tool_choice)

    harness.chat.llm = Blocking(lambda *_: LLMReply(content="", tool_calls=(_call("pause_agent"),)))  # type: ignore[assignment]
    client = make_client(harness)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    cid = client.post("/v1/conversations", json={}).json()["conversation"]["id"]
    assert client.post(f"/v1/conversations/{cid}/stop").json()["stopped"] is False  # idle: nothing to stop
    assert client.post("/v1/conversations/not-a-chat/stop").status_code == 404

    out: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: out.update(
            events=_sse(client.post("/v1/chat/stream", json={"message": "pause it", "conversation_id": cid}).text)
        )
    )
    worker.start()
    assert started.wait(5)
    assert client.post(f"/v1/conversations/{cid}/stop").json() == {"stopped": True}
    release.set()
    worker.join(10)
    reply = [p for name, p in out["events"] if name == "message"][-1]["reply"]
    assert "Stopped" in reply and harness.agent.status()["paused"] is False  # the pause never started


# --------------------------------------------------------------------- the LLM client itself
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]):
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", factory)
    monkeypatch.setattr(llm_module, "LLM_RETRY_DELAYS", (0.0, 0.0))
    return LLMClient(make_settings(tmp_path, llm_base_url="http://llm.test/v1", llm_model="m"))


def _sse_body(*chunks: dict[str, Any]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


async def _noop(_piece: str) -> None:
    return None


async def test_llm_retries_a_503_and_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})

    reply = await _client(tmp_path, monkeypatch, handler).chat([{"role": "user", "content": "x"}])
    assert reply.content == "hi" and reply.finish_reason == "stop" and attempts["n"] == 3


async def test_llm_gives_up_after_the_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def always_503(_req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="down")

    with pytest.raises(LLMUnavailable):
        await _client(tmp_path, monkeypatch, always_503).chat([{"role": "user", "content": "x"}])
    assert attempts["n"] == 3  # the first try plus two retries


async def test_llm_does_not_retry_a_rejected_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def bad_request(_req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(LLMUnavailable):
        await _client(tmp_path, monkeypatch, bad_request).chat([{"role": "user", "content": "x"}])
    assert attempts["n"] == 1


class _BreaksMidStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self.first = first

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self.first
        raise httpx.ReadError("connection reset")


async def test_a_stream_that_breaks_is_an_llm_error_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, stream=_BreaksMidStream(b"data: " + b"{" + b"}\n\n"))

    client = _client(tmp_path, monkeypatch, handler)
    with pytest.raises(LLMTransient):  # was an uncaught httpx error ("Chat failed: ReadError")
        await client.chat([{"role": "user", "content": "x"}], on_delta=_noop)
    assert attempts["n"] == 3  # nothing had reached the app yet, so it was safe to ask again


async def test_no_retry_once_text_has_reached_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}
    first = b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'

    def handler(_req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, stream=_BreaksMidStream(first))

    pieces: list[str] = []

    async def collect(piece: str) -> None:
        pieces.append(piece)

    with pytest.raises(LLMTransient):
        await _client(tmp_path, monkeypatch, handler).chat([{"role": "user", "content": "x"}], on_delta=collect)
    assert attempts["n"] == 1 and pieces == ["Hel"]  # a retry would have shown "Hel" twice


async def test_an_error_chunk_inside_a_200_stream_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body({"error": {"message": "upstream exploded"}}))

    with pytest.raises(LLMUnavailable, match="upstream exploded"):
        await _client(tmp_path, monkeypatch, handler).chat([{"role": "user", "content": "x"}], on_delta=_noop)


async def test_parallel_tool_calls_that_all_say_index_zero_stay_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tc(ident: str, name: str, args: str) -> dict[str, Any]:
        return {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "id": ident, "function": {"name": name, "arguments": args}}]}}
            ]
        }

    def handler(_req: httpx.Request) -> httpx.Response:
        done = {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        return httpx.Response(
            200, content=_sse_body(tc("a", "get_book", '{"m":1}'), tc("b", "crypto_spot", '{"s":"BTC"}'), done)
        )

    reply = await _client(tmp_path, monkeypatch, handler).chat([{"role": "user", "content": "x"}], on_delta=_noop)
    assert [(c["id"], c["name"], c["arguments"]) for c in reply.tool_calls] == [
        ("a", "get_book", '{"m":1}'),
        ("b", "crypto_spot", '{"s":"BTC"}'),
    ]
    assert reply.finish_reason == "tool_calls"


async def test_tool_choice_none_is_sent_only_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client(tmp_path, monkeypatch, handler)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {}}}}]
    await client.chat([{"role": "user", "content": "x"}], tools=tools)
    await client.chat([{"role": "user", "content": "x"}], tools=tools, tool_choice="none")
    assert [b["tool_choice"] for b in bodies] == ["auto", "none"]


# ------------------------------------------------- the app can see what each tool was sent and returned
async def test_tool_steps_carry_their_input_and_output_with_secrets_redacted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(name: str, args: Any, **_: Any) -> dict[str, Any]:
        return {"ok": False, "error": "venue geoblocked", "api_key": "sk-live-123456789"}

    monkeypatch.setattr(harness.chat, "_run_tool", run)
    call = {"id": "c1", "name": "place_limit_order", "arguments": json.dumps({"price": "0.72", "private_key": "0xabc"})}
    model = _Script(lambda n, _m, _t: LLMReply(content="", tool_calls=(call,)) if n == 1 else LLMReply(content="done"))
    harness.chat.llm = model  # type: ignore[assignment]
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(name: str, data: dict[str, Any]) -> None:
        events.append((name, dict(data)))

    await _ask(harness, on_event=on_event)
    steps = [d for n, d in events if n == "tool"]
    assert [s["state"] for s in steps] == ["running", "error"]
    assert "0.72" in steps[0]["input"] and "0xabc" not in steps[0]["input"]
    assert steps[1]["input"] == steps[0]["input"]  # the finished event still carries what was sent
    assert "geoblocked" in steps[1]["output"] and "sk-live-123456789" not in steps[1]["output"]


async def test_step_detail_can_be_switched_off(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(harness, chat_step_detail_chars=0)

    async def run(*_: Any, **__: Any) -> dict[str, Any]:
        return {"ok": True, "x": 1}

    monkeypatch.setattr(harness.chat, "_run_tool", run)
    model = _Script(lambda n, _m, _t: LLMReply(content="", tool_calls=(_call(),)) if n == 1 else LLMReply(content="ok"))
    harness.chat.llm = model  # type: ignore[assignment]
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(name: str, data: dict[str, Any]) -> None:
        events.append((name, dict(data)))

    await _ask(harness, on_event=on_event)
    assert all(not d.get("input") and not d.get("output") for n, d in events if n == "tool")


# ------------------------------------------------- the same failure twice stops blind retries
async def test_the_same_failure_twice_tells_the_model_to_stop_retrying(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set(harness, llm_stall_rounds=0)

    async def run(*_: Any, **__: Any) -> dict[str, Any]:
        return {"ok": False, "error": "region blocked"}

    monkeypatch.setattr(harness.chat, "_run_tool", run)

    def plan(n: int, _m: list[dict[str, Any]], _t: str) -> LLMReply:
        if n <= 3:
            args = json.dumps({"price": f"0.7{n}"})  # different arguments each time: not a stall
            return LLMReply(content="", tool_calls=({"id": f"c{n}", "name": "place_limit_order", "arguments": args},))
        return LLMReply(content="It is blocked.")

    model = _Script(plan)
    harness.chat.llm = model  # type: ignore[assignment]
    await _ask(harness)
    sent = [m["content"] for m in model.requests[-1]["messages"] if m["role"] == "tool"]
    assert "stop_retrying" not in sent[0]
    assert "stop_retrying" in sent[1] and "stop_retrying" in sent[2]


# ------------------------------------------------- the output cap is sent only when configured
async def test_max_tokens_is_sent_only_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client(tmp_path, monkeypatch, handler)
    await client.chat([{"role": "user", "content": "x"}])
    client.max_output_tokens = 16000
    await client.chat([{"role": "user", "content": "x"}])
    assert "max_tokens" not in bodies[0] and bodies[1]["max_tokens"] == 16000
