"""Persistent conversations: every chat is isolated from every other chat, in the data layer."""

from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.exc import IntegrityError

from mm_agent.chat import CONTEXT_RECENT_MESSAGES, SUMMARIZE_AFTER_MESSAGES
from mm_agent.llm import LLMReply, LLMUnavailable
from mm_agent.store import Base, ConversationBusy, ConversationNotFound, Store
from mm_agent.store.models import ChatMessageRow, ConversationRow
from tests.conftest import Harness
from tests.test_api import _sse, make_client

OWNER = "web:operator"
OTHER = "web:mallory"
ROOT = Path(__file__).resolve().parent.parent


def _store(tmp_path: Path, name: str = "c.db") -> Store:
    store = Store(f"sqlite:///{tmp_path / name}")
    store.create_all()
    return store


def _say(store: Store, owner: str, cid: str, *texts: str, role: str = "user") -> list[int]:
    return store.chat_append(owner, cid, [{"role": role, "text": t} for t in texts])


# --------------------------------------------------------------------- data layer
def test_every_conversation_has_its_own_uuid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = [store.chat_create(OWNER)["id"] for _ in range(50)]
    assert len(set(ids)) == 50
    for cid in ids:
        assert str(uuid.UUID(cid)) == cid
        assert uuid.UUID(cid).version == 4


def test_a_message_cannot_exist_outside_a_conversation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.chat_create(OWNER)["id"]
    b = store.chat_create(OWNER)["id"]
    assert _say(store, OWNER, a, "one", "two") == [1, 2]
    assert _say(store, OWNER, b, "uno") == [1]  # numbering belongs to the chat, not the table
    assert [m["text"] for m in store.chat_messages(OWNER, a)] == ["one", "two"]
    assert [m["text"] for m in store.chat_messages(OWNER, b)] == ["uno"]
    # The database itself refuses a message that points at no conversation.
    with pytest.raises(IntegrityError), store.session() as s:
        s.execute(insert(ChatMessageRow).values(conversation_id=str(uuid.uuid4()), seq=1, role="user", content="x"))
    with store.session() as s:
        orphans = s.scalar(
            select(func.count())
            .select_from(ChatMessageRow)
            .where(ChatMessageRow.conversation_id.not_in(select(ConversationRow.id)))
        )
    assert orphans == 0


def test_another_owner_cannot_touch_a_conversation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cid = store.chat_create(OWNER, "private plan")["id"]
    _say(store, OWNER, cid, "my secret strategy: buy the dip")
    store.chat_set_summary(OWNER, cid, "summary of the secret strategy", 1)
    before = store.chat_get(OWNER, cid)

    attacks: list[Any] = [
        lambda: store.chat_get(OTHER, cid),
        lambda: store.chat_messages(OTHER, cid),
        lambda: store.chat_messages(OTHER, cid, after=0),
        lambda: store.chat_messages(OTHER, cid, before=99),
        lambda: store.chat_append(OTHER, cid, [{"role": "user", "text": "injected"}]),
        lambda: store.chat_rename(OTHER, cid, "stolen"),
        lambda: store.chat_set_summary(OTHER, cid, "poisoned", 1),
        lambda: store.chat_begin_run(OTHER, cid),
        lambda: store.chat_finish_run(OTHER, cid, "idle", {}),
        lambda: store.chat_delete(OTHER, cid),
    ]
    for attack in attacks:
        with pytest.raises(ConversationNotFound):
            attack()

    assert store.chat_list(OTHER) == []
    assert store.chat_list(OTHER, q="secret") == []  # search never crosses owners either
    assert store.chat_list(OWNER, q="secret")[0]["id"] == cid
    after = store.chat_get(OWNER, cid)
    assert after == before  # nothing changed: title, summary, state, count, timestamps
    assert [m["text"] for m in store.chat_messages(OWNER, cid)] == ["my secret strategy: buy the dip"]


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "' OR '1'='1", "../../etc/passwd", str(uuid.uuid4())])
def test_unknown_or_malformed_ids_are_simply_not_found(tmp_path: Path, bad: str) -> None:
    store = _store(tmp_path)
    store.chat_create(OWNER)
    with pytest.raises(ConversationNotFound):
        store.chat_get(OWNER, bad)
    with pytest.raises(ConversationNotFound):
        store.chat_messages(OWNER, bad)


def test_an_empty_owner_never_matches_anything(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cid = store.chat_create(OWNER)["id"]
    with pytest.raises(ConversationNotFound):
        store.chat_get("", cid)
    with pytest.raises(ValueError):
        store.chat_create("")


def test_conversations_and_their_context_survive_a_restart(tmp_path: Path) -> None:
    first = _store(tmp_path)
    cid = first.chat_create(OWNER, "Fed cut")["id"]
    _say(first, OWNER, cid, "will the Fed cut?")
    first.chat_append(
        OWNER,
        cid,
        [{"role": "agent", "text": "Likely.", "tools": [{"id": "c1", "name": "get_market", "state": "done"}]}],
    )
    _say(first, OWNER, cid, "and in June?")
    assert first.chat_set_summary(OWNER, cid, "asked about Fed cuts", 2)
    first.chat_begin_run(OWNER, cid)
    first.chat_finish_run(OWNER, cid, "stopped", {"tools": [{"id": "c1", "name": "get_market", "state": "done"}]})
    seen = (first.chat_list(OWNER), first.chat_get(OWNER, cid), first.chat_messages(OWNER, cid))
    first.close()

    second = Store(f"sqlite:///{tmp_path / 'c.db'}")  # a new process opening the same file
    assert (second.chat_list(OWNER), second.chat_get(OWNER, cid), second.chat_messages(OWNER, cid)) == seen
    got = second.chat_get(OWNER, cid)
    assert got["title"] == "Fed cut" and got["summary"] == "asked about Fed cuts" and got["summary_upto"] == 2
    assert got["status"] == "stopped" and got["state"]["tools"][0]["name"] == "get_market"
    assert second.chat_messages(OWNER, cid)[1]["tools"][0]["state"] == "done"
    assert _say(second, OWNER, cid, "thanks") == [4]  # numbering continues where it stopped
    second.close()


def test_an_answer_cut_off_by_a_restart_is_reported_as_interrupted(tmp_path: Path) -> None:
    first = _store(tmp_path)
    running = first.chat_create(OWNER)["id"]
    idle = first.chat_create(OWNER)["id"]
    first.chat_begin_run(OWNER, running)
    first.close()  # the server dies mid-answer

    second = Store(f"sqlite:///{tmp_path / 'c.db'}")
    assert second.chat_get(OWNER, running)["status"] == "running"  # stale until the sweep
    assert second.chat_interrupt_stale_runs() == 1
    assert second.chat_get(OWNER, running)["status"] == "interrupted"
    assert second.chat_get(OWNER, idle)["status"] == "idle"
    second.chat_begin_run(OWNER, running)  # an interrupted chat can be used again
    second.close()


def test_a_chat_answers_one_message_at_a_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cid = store.chat_create(OWNER)["id"]
    store.chat_begin_run(OWNER, cid)
    with pytest.raises(ConversationBusy):
        store.chat_begin_run(OWNER, cid)
    other = store.chat_create(OWNER)["id"]
    store.chat_begin_run(OWNER, other)  # a different chat is not blocked
    store.chat_finish_run(OWNER, cid, "idle", {})
    store.chat_begin_run(OWNER, cid)


def test_deleting_one_chat_removes_its_messages_and_nothing_else(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.chat_create(OWNER, "a")["id"]
    b = store.chat_create(OWNER, "b")["id"]
    _say(store, OWNER, a, "a1", "a2")
    _say(store, OWNER, b, "b1")
    store.chat_delete(OWNER, a)
    with pytest.raises(ConversationNotFound):
        store.chat_get(OWNER, a)
    with store.session() as s:
        left = s.scalars(select(ChatMessageRow.conversation_id)).all()
    assert left == [b]
    assert [m["text"] for m in store.chat_messages(OWNER, b)] == ["b1"]
    assert [c["id"] for c in store.chat_list(OWNER)] == [b]


def test_search_matches_titles_and_message_text_literally(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sale = store.chat_create(OWNER, "Weekend plan")["id"]
    other = store.chat_create(OWNER, "Something else")["id"]
    _say(store, OWNER, sale, "everything is 50% off")
    _say(store, OWNER, other, "500 dollars, 5_0 apples")
    assert [c["id"] for c in store.chat_list(OWNER, q="50%")] == [sale]  # % is not a wildcard
    assert [c["id"] for c in store.chat_list(OWNER, q="5_0")] == [other]  # _ is not a wildcard
    assert [c["id"] for c in store.chat_list(OWNER, q="WEEKEND")] == [sale]  # titles, any case
    assert {c["id"] for c in store.chat_list(OWNER, q="  ")} == {sale, other}  # blank = everything
    assert store.chat_list(OWNER, q="nothing like this") == []


def test_long_history_is_loaded_in_pages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cid = store.chat_create(OWNER)["id"]
    for start in range(0, 120, 40):
        _say(store, OWNER, cid, *[f"m{n}" for n in range(start + 1, start + 41)])
    seqs = lambda **kw: [m["seq"] for m in store.chat_messages(OWNER, cid, **kw)]  # noqa: E731
    assert seqs() == list(range(71, 121))  # the latest page, oldest first
    assert seqs(before=71) == list(range(21, 71))
    assert seqs(before=21) == list(range(1, 21))
    assert seqs(after=100) == list(range(101, 121))
    assert seqs(limit=3) == [118, 119, 120]
    assert seqs(before=1) == []


def test_the_summary_only_moves_forward(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cid = store.chat_create(OWNER)["id"]
    _say(store, OWNER, cid, *[f"m{n}" for n in range(10)])
    assert store.chat_set_summary(OWNER, cid, "first", 6)
    assert not store.chat_set_summary(OWNER, cid, "older", 4)  # would go backwards
    assert not store.chat_set_summary(OWNER, cid, "future", 11)  # beyond the last message
    got = store.chat_get(OWNER, cid)
    assert (got["summary"], got["summary_upto"]) == ("first", 6)


def test_stored_text_is_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cid = store.chat_create(OWNER)["id"]
    key = "0x" + "ab" * 32
    _say(store, OWNER, cid, f"my key is {key}")
    assert key not in store.chat_messages(OWNER, cid)[0]["text"]


def test_concurrent_writers_never_mix_chats_or_reuse_numbers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chats = [store.chat_create(OWNER, f"chat {i}")["id"] for i in range(6)]

    def write(i: int, count: int) -> None:
        for k in range(count):
            _say(store, OWNER, chats[i], f"chat{i}-msg{k}")

    with ThreadPoolExecutor(max_workers=10) as pool:
        # Chat 0 is written by three threads at once; the others by one thread each.
        jobs = [pool.submit(write, i, 20) for i in range(6)] + [pool.submit(write, 0, 20) for _ in range(2)]
        for job in jobs:
            job.result()

    for i, cid in enumerate(chats):
        messages = store.chat_messages(OWNER, cid, limit=500)
        assert [m["seq"] for m in messages] == list(range(1, len(messages) + 1))  # unique and gapless
        assert len(messages) == (60 if i == 0 else 20)
        assert all(m["text"].startswith(f"chat{i}-") for m in messages)  # nothing from another chat
        assert store.chat_get(OWNER, cid)["message_count"] == len(messages)


def test_migrations_build_the_same_schema_as_the_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    engine.dispose()
    store = Store(url)  # and the migrated database really works
    cid = store.chat_create(OWNER)["id"]
    _say(store, OWNER, cid, "hello")
    assert store.chat_messages(OWNER, cid)[0]["text"] == "hello"
    store.close()


# ------------------------------------------------------------------ chat service
class _ModelDouble:
    """Stands in for the LLM and remembers exactly what it was sent."""

    enabled = True

    def __init__(self, *, summary: str | None = "SUMMARY-OF-OLD-MESSAGES", tool: str | None = None) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.summary_calls: list[list[dict[str, Any]]] = []
        self.summary = summary
        self.tool = tool
        self.gate: Any = None
        self.on_first_call: Any = None

    async def chat(self, messages, tools=None, temperature=0.1, on_delta=None):  # type: ignore[no-untyped-def]
        if tools is None:  # a summarization request
            self.summary_calls.append(messages)
            if self.summary is None:
                raise LLMUnavailable("down")
            return LLMReply(content=self.summary)
        self.calls.append(messages)
        if self.gate is not None:
            await self.gate()
        if self.tool and not any(m["role"] == "tool" for m in messages):
            if self.on_first_call is not None:
                await self.on_first_call(on_delta)
            return LLMReply(content="", tool_calls=({"id": "c1", "name": self.tool, "arguments": "{}"},))
        return LLMReply(content="re: " + str(messages[-1]["content"]))


def _everything_sent(calls: list[list[dict[str, Any]]]) -> str:
    return "\n".join(str(m.get("content")) for call in calls for m in call)


async def _noop(_kind: str, _payload: dict[str, Any]) -> None:
    return None


async def test_the_model_sees_only_its_own_conversation(harness: Harness) -> None:
    model = _ModelDouble()
    harness.chat.llm = model  # type: ignore[assignment]
    store = harness.agent.store
    a = store.chat_create(OWNER)["id"]
    b = store.chat_create(OWNER)["id"]
    await harness.chat.handle("alpha-secret first", actor=OWNER, conversation_id=a)
    await harness.chat.handle("bravo-secret first", actor=OWNER, conversation_id=b)
    model.calls.clear()

    await harness.chat.handle("alpha follow-up", actor=OWNER, conversation_id=a)
    sent_a = _everything_sent(model.calls)
    assert "alpha-secret first" in sent_a and "re: alpha-secret first" in sent_a  # its own context is back
    assert "bravo" not in sent_a
    model.calls.clear()

    await harness.chat.handle("bravo follow-up", actor=OWNER, conversation_id=b)
    sent_b = _everything_sent(model.calls)
    assert "bravo-secret first" in sent_b and "alpha" not in sent_b


async def test_another_owner_cannot_chat_into_a_conversation(harness: Harness) -> None:
    harness.chat.llm = _ModelDouble()  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    await harness.chat.handle("private question", actor=OWNER, conversation_id=cid)
    with pytest.raises(ConversationNotFound):
        await harness.chat.handle("what did they ask?", actor=OTHER, conversation_id=cid)
    assert [m["text"] for m in store.chat_messages(OWNER, cid)] == ["private question", "re: private question"]
    assert store.chat_get(OWNER, cid)["status"] == "idle"  # the refused attempt did not claim the chat


async def test_reopening_after_a_restart_restores_the_context(harness: Harness) -> None:
    model = _ModelDouble()
    harness.chat.llm = model  # type: ignore[assignment]
    cid = harness.agent.store.chat_create(OWNER)["id"]
    await harness.chat.handle("remember the code word kiwi", actor=OWNER, conversation_id=cid)

    harness.agent.store = Store(harness.settings.database_url)  # the server restarts: a new store, same file
    model.calls.clear()
    await harness.chat.handle("what was the code word?", actor=OWNER, conversation_id=cid)
    sent = _everything_sent(model.calls)
    assert "remember the code word kiwi" in sent and "re: remember the code word kiwi" in sent
    texts = [m["text"] for m in harness.agent.store.chat_messages(OWNER, cid)]
    assert texts[0] == "remember the code word kiwi" and len(texts) == 4


async def test_a_long_chat_is_summarized_and_the_context_stays_bounded(harness: Harness) -> None:
    model = _ModelDouble()
    harness.chat.llm = model  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    for n in range(SUMMARIZE_AFTER_MESSAGES // 2 + 3):
        _say(store, OWNER, cid, f"question {n}")
        _say(store, OWNER, cid, f"answer {n}", role="agent")
    total = SUMMARIZE_AFTER_MESSAGES + 6

    await harness.chat.handle("one more thing", actor=OWNER, conversation_id=cid)

    assert len(model.summary_calls) == 1
    system = model.calls[0][0]["content"]
    assert "SUMMARY-OF-OLD-MESSAGES" in system and "out of date" in system
    history = model.calls[0][1:-1]
    assert len(history) == CONTEXT_RECENT_MESSAGES  # old messages left the context ...
    assert history[-1]["content"] == f"answer {total // 2 - 1}"
    assert "question 0" not in _everything_sent(model.calls)
    chat = store.chat_get(OWNER, cid)
    assert chat["summary"] == "SUMMARY-OF-OLD-MESSAGES" and chat["summary_upto"] == total - CONTEXT_RECENT_MESSAGES
    # ... but nothing was lost: the whole history can still be paged back in.
    everything = store.chat_messages(OWNER, cid, limit=500)
    assert len(everything) == total + 2 and everything[0]["text"] == "question 0"

    await harness.chat.handle("and another", actor=OWNER, conversation_id=cid)
    assert len(model.summary_calls) == 1  # not summarized again until it grows long again
    assert "SUMMARY-OF-OLD-MESSAGES" in model.calls[-1][0]["content"]


async def test_summarizing_still_works_when_the_model_cannot(harness: Harness) -> None:
    model = _ModelDouble(summary=None)
    harness.chat.llm = model  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    for n in range(SUMMARIZE_AFTER_MESSAGES + 5):
        _say(store, OWNER, cid, f"fact {n}")
    result = await harness.chat.handle("still there?", actor=OWNER, conversation_id=cid)
    assert result["reply"].startswith("re:")
    summary = store.chat_get(OWNER, cid)["summary"]
    assert "fact 0" in summary and len(summary) <= 3000


async def test_chats_can_answer_at_the_same_time_without_mixing(harness: Harness) -> None:
    model = _ModelDouble()
    harness.chat.llm = model  # type: ignore[assignment]
    store = harness.agent.store
    a = store.chat_create(OWNER)["id"]
    b = store.chat_create(OWNER)["id"]
    await harness.chat.handle("alpha history", actor=OWNER, conversation_id=a)
    await harness.chat.handle("bravo history", actor=OWNER, conversation_id=b)
    model.calls.clear()

    arrived = 0
    both_in_flight = asyncio.Event()
    statuses: list[tuple[str, str]] = []

    async def gate() -> None:
        nonlocal arrived
        arrived += 1
        statuses.append((store.chat_get(OWNER, a)["status"], store.chat_get(OWNER, b)["status"]))
        if arrived == 2:
            both_in_flight.set()
        await asyncio.wait_for(both_in_flight.wait(), timeout=5)  # only passes if both run together

    model.gate = gate
    ra, rb = await asyncio.gather(
        harness.chat.handle("alpha again", actor=OWNER, conversation_id=a),
        harness.chat.handle("bravo again", actor=OWNER, conversation_id=b),
    )
    assert ra["reply"] == "re: alpha again" and rb["reply"] == "re: bravo again"
    assert statuses[-1] == ("running", "running")
    for call in model.calls:
        text = _everything_sent([call])
        assert ("alpha" in text) != ("bravo" in text)  # each request carries exactly one chat's history
    assert [m["text"] for m in store.chat_messages(OWNER, a)][-2:] == ["alpha again", "re: alpha again"]
    assert [m["text"] for m in store.chat_messages(OWNER, b)][-2:] == ["bravo again", "re: bravo again"]
    assert store.chat_get(OWNER, a)["status"] == store.chat_get(OWNER, b)["status"] == "idle"


async def test_a_second_message_is_refused_while_the_chat_is_answering(harness: Harness) -> None:
    model = _ModelDouble()
    harness.chat.llm = model  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    started, release = asyncio.Event(), asyncio.Event()

    async def gate() -> None:
        started.set()
        await asyncio.wait_for(release.wait(), timeout=5)

    model.gate = gate
    first = asyncio.create_task(harness.chat.handle("first", actor=OWNER, conversation_id=cid))
    await asyncio.wait_for(started.wait(), timeout=5)
    with pytest.raises(ConversationBusy):
        await harness.chat.handle("second", actor=OWNER, conversation_id=cid)
    release.set()
    await first
    assert [m["text"] for m in store.chat_messages(OWNER, cid)] == ["first", "re: first"]  # "second" left no trace
    await harness.chat.handle("third", actor=OWNER, conversation_id=cid)  # and the chat is free again


async def test_stopping_keeps_what_had_arrived_and_the_run_state(harness: Harness) -> None:
    stop = {"now": False}
    model = _ModelDouble(tool="pause_agent")

    async def partial(on_delta: Any) -> None:
        await on_delta("Looking into it. ")
        stop["now"] = True  # Stop is pressed before the model's tool call is carried out

    model.on_first_call = partial
    harness.chat.llm = model  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    result = await harness.chat.handle(
        "pause everything", actor=OWNER, on_event=_noop, should_stop=lambda: stop["now"], conversation_id=cid
    )
    assert result["data"]["stopped"] is True and harness.agent.status()["paused"] is False
    last = store.chat_messages(OWNER, cid)[-1]
    assert last["role"] == "agent" and last["text"] == "Looking into it.\n\nStopped."
    assert store.chat_get(OWNER, cid)["status"] == "stopped"


async def test_tool_steps_and_failures_are_stored(harness: Harness) -> None:
    harness.chat.llm = _ModelDouble(tool="get_status")  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    await harness.chat.handle("how are we doing", actor=OWNER, conversation_id=cid)
    reply = store.chat_messages(OWNER, cid)[-1]
    assert reply["tools"] == [{"id": "c1", "name": "get_status", "state": "done"}]
    got = store.chat_get(OWNER, cid)
    assert got["status"] == "idle" and got["state"]["tools"] == reply["tools"] and got["state"]["finished_at"]

    await harness.chat.handle("/nosuchcommand", actor=OWNER, conversation_id=cid)
    failed = store.chat_messages(OWNER, cid)[-1]
    assert failed["role"] == "error"
    assert store.chat_get(OWNER, cid)["status"] == "error"


async def test_the_title_comes_from_the_first_message_and_a_rename_sticks(harness: Harness) -> None:
    harness.chat.llm = _ModelDouble()  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    await harness.chat.handle("Will the Fed cut rates in December?\nSecond line", actor=OWNER, conversation_id=cid)
    assert store.chat_get(OWNER, cid)["title"] == "Will the Fed cut rates in December?"
    store.chat_rename(OWNER, cid, "Fed")
    await harness.chat.handle("another question", actor=OWNER, conversation_id=cid)
    assert store.chat_get(OWNER, cid)["title"] == "Fed"


async def test_messages_without_a_conversation_are_not_stored(harness: Harness) -> None:
    harness.chat.llm = _ModelDouble()  # type: ignore[assignment]
    result = await harness.chat.handle("quick question", actor=OWNER)  # the Telegram / legacy path
    assert result["reply"] == "re: quick question" and "conversation_id" not in result
    with harness.agent.store.session() as s:
        assert s.scalar(select(func.count()).select_from(ChatMessageRow)) == 0
        assert s.scalar(select(func.count()).select_from(ConversationRow)) == 0


async def test_deleting_a_chat_while_it_answers_is_safe(harness: Harness) -> None:
    model = _ModelDouble()
    harness.chat.llm = model  # type: ignore[assignment]
    store = harness.agent.store
    cid = store.chat_create(OWNER)["id"]
    started, release = asyncio.Event(), asyncio.Event()

    async def gate() -> None:
        started.set()
        await asyncio.wait_for(release.wait(), timeout=5)

    model.gate = gate
    running = asyncio.create_task(harness.chat.handle("hello", actor=OWNER, conversation_id=cid))
    await asyncio.wait_for(started.wait(), timeout=5)
    store.chat_delete(OWNER, cid)
    release.set()
    with pytest.raises(ConversationNotFound):  # the reply has nowhere to go, and nothing is resurrected
        await running
    assert store.chat_list(OWNER) == []
    with store.session() as s:
        assert s.scalar(select(func.count()).select_from(ChatMessageRow)) == 0


# --------------------------------------------------------------------------- HTTP
def _login(client: TestClient) -> None:
    assert client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"}).status_code == 200


def _as_user(harness: Harness, username: str) -> TestClient:
    """A client signed in as a different user (a session token signed with the server's secret)."""

    signer = URLSafeTimedSerializer(harness.settings.session_secret, salt="mm-session")
    token = signer.dumps({"u": username, "v": "1", "o": int(time.time())})
    client = make_client(harness)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def test_api_conversation_lifecycle(harness: Harness) -> None:
    client = make_client(harness)
    _login(client)
    created = client.post("/v1/conversations", json={}).json()["conversation"]
    cid = created["id"]
    assert str(uuid.UUID(cid)) == cid and created["title"] == "" and created["status"] == "idle"

    reply = client.post("/v1/chat", json={"message": "/status", "conversation_id": cid}).json()
    assert reply["conversation_id"] == cid and "FAKE" in reply["reply"]

    opened = client.get(f"/v1/conversations/{cid}").json()
    assert opened["conversation"]["title"] == "/status" and opened["has_more"] is False
    assert [(m["seq"], m["role"]) for m in opened["messages"]] == [(1, "user"), (2, "agent")]

    renamed = client.post(f"/v1/conversations/{cid}/rename", json={"title": "Daily check"}).json()
    assert renamed["conversation"]["title"] == "Daily check"
    assert client.post(f"/v1/conversations/{cid}/rename", json={"title": "  "}).status_code == 422

    assert [c["id"] for c in client.get("/v1/conversations", params={"q": "daily"}).json()["conversations"]] == [cid]
    assert [c["id"] for c in client.get("/v1/conversations", params={"q": "FAKE"}).json()["conversations"]] == [cid]
    assert client.get("/v1/conversations", params={"q": "zzz"}).json()["conversations"] == []

    assert client.delete(f"/v1/conversations/{cid}").json() == {"deleted": True}
    assert client.get(f"/v1/conversations/{cid}").status_code == 404
    assert client.get("/v1/conversations").json()["conversations"] == []


def test_api_streams_into_a_conversation(harness: Harness) -> None:
    client = make_client(harness)
    _login(client)
    cid = client.post("/v1/conversations", json={}).json()["conversation"]["id"]
    events = _sse(client.post("/v1/chat/stream", json={"message": "/status", "conversation_id": cid}).text)
    assert [n for n, _ in events][-2:] == ["message", "done"]
    events = _sse(client.get("/v1/chat/stream", params={"message": "/help", "conversation_id": cid}).text)
    assert [n for n, _ in events][-2:] == ["message", "done"]
    assert [m["role"] for m in client.get(f"/v1/conversations/{cid}").json()["messages"]] == ["user", "agent"] * 2


def test_api_users_cannot_reach_each_others_conversations(harness: Harness) -> None:
    owner = make_client(harness)
    _login(owner)
    cid = owner.post("/v1/conversations", json={"title": "private"}).json()["conversation"]["id"]
    owner.post("/v1/chat", json={"message": "/status", "conversation_id": cid})
    before = owner.get(f"/v1/conversations/{cid}").json()

    mallory = _as_user(harness, "mallory")
    assert mallory.get("/v1/status").status_code == 200  # a valid, signed-in user
    missing = str(uuid.uuid4())
    for target in (cid, missing):  # someone else's chat must look exactly like a chat that does not exist
        responses = [
            mallory.get(f"/v1/conversations/{target}"),
            mallory.get(f"/v1/conversations/{target}/messages"),
            mallory.post(f"/v1/conversations/{target}/rename", json={"title": "mine now"}),
            mallory.delete(f"/v1/conversations/{target}"),
            mallory.post("/v1/chat", json={"message": "/status", "conversation_id": target}),
            mallory.get("/v1/chat/stream", params={"message": "/status", "conversation_id": target}),
            mallory.post("/v1/chat/stream", json={"message": "/status", "conversation_id": target}),
        ]
        assert [r.status_code for r in responses] == [404] * 7
        assert {r.json()["detail"] for r in responses} == {"conversation not found"}
    assert mallory.get("/v1/conversations").json()["conversations"] == []
    assert mallory.get("/v1/conversations", params={"q": "FAKE"}).json()["conversations"] == []

    assert owner.get(f"/v1/conversations/{cid}").json() == before  # untouched


def test_api_restores_a_conversation_after_the_server_restarts(harness: Harness) -> None:
    client = make_client(harness)
    _login(client)
    cid = client.post("/v1/conversations", json={}).json()["conversation"]["id"]
    client.post("/v1/chat", json={"message": "/status", "conversation_id": cid})
    store = harness.agent.store
    store.chat_set_summary(OWNER, cid, "checked the status", 1)
    before = client.get(f"/v1/conversations/{cid}").json()
    token = client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"}).json()["token"]

    harness.agent.store = Store(harness.settings.database_url)  # restart: new process, same database
    harness.agent.store.chat_interrupt_stale_runs()
    restarted = make_client(harness)
    restarted.headers["Authorization"] = f"Bearer {token}"
    after = restarted.get(f"/v1/conversations/{cid}").json()
    assert after == before
    assert after["conversation"]["summary"] == "checked the status"
    assert [c["id"] for c in restarted.get("/v1/conversations").json()["conversations"]] == [cid]
    restarted.post("/v1/chat", json={"message": "/help", "conversation_id": cid})
    assert [m["seq"] for m in restarted.get(f"/v1/conversations/{cid}").json()["messages"]] == [1, 2, 3, 4]


def test_api_pages_through_a_long_history(harness: Harness) -> None:
    client = make_client(harness)
    _login(client)
    cid = client.post("/v1/conversations", json={}).json()["conversation"]["id"]
    for start in range(0, 120, 40):
        _say(harness.agent.store, OWNER, cid, *[f"m{n}" for n in range(start + 1, start + 41)])
    opened = client.get(f"/v1/conversations/{cid}").json()
    assert [m["seq"] for m in opened["messages"]] == list(range(71, 121)) and opened["has_more"] is True
    collected = opened["messages"]
    while True:
        page = client.get(
            f"/v1/conversations/{cid}/messages", params={"before": collected[0]["seq"], "limit": 25}
        ).json()
        collected = page["messages"] + collected
        if not page["has_more"]:
            break
    assert [m["seq"] for m in collected] == list(range(1, 121))
    assert [m["text"] for m in collected] == [f"m{n}" for n in range(1, 121)]


def test_api_refuses_a_second_message_while_the_chat_is_answering(harness: Harness) -> None:
    client = make_client(harness)
    _login(client)
    cid = client.post("/v1/conversations", json={}).json()["conversation"]["id"]
    harness.agent.store.chat_begin_run(OWNER, cid)  # an answer is in progress
    assert client.post("/v1/chat", json={"message": "/status", "conversation_id": cid}).status_code == 409
    assert client.get("/v1/chat/stream", params={"message": "/status", "conversation_id": cid}).status_code == 409
    assert client.post("/v1/chat/stream", json={"message": "/status", "conversation_id": cid}).status_code == 409
    assert client.get(f"/v1/conversations/{cid}").json()["messages"] == []  # nothing was stored
    harness.agent.store.chat_finish_run(OWNER, cid, "idle", {})
    assert client.post("/v1/chat", json={"message": "/status", "conversation_id": cid}).status_code == 200
