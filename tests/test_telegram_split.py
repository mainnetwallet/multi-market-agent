"""Long replies reach Telegram in several messages: nothing is cut off, and no part is too long."""

from __future__ import annotations

import pytest

from mm_agent.telegram import TELEGRAM_MESSAGE_CHARS, split_message


def test_a_short_reply_is_sent_as_it_is() -> None:
    assert split_message("hello") == ["hello"]


def test_an_empty_reply_becomes_a_placeholder() -> None:
    assert split_message("") == ["(no reply)"]


@pytest.mark.parametrize(
    "text",
    [
        "a" * (TELEGRAM_MESSAGE_CHARS + 1),
        "x" * 25_000,
        "\n".join("line " * 20 for _ in range(300)),
        "\u09ac\u09be\u0982\u09b2\u09be " * 3000,
        "short\n" + "c" * 9000,
    ],
)
def test_a_long_reply_is_split_into_parts_telegram_accepts_and_nothing_is_lost(text: str) -> None:
    parts = split_message(text)
    assert len(parts) > 1
    assert all(0 < len(part) <= TELEGRAM_MESSAGE_CHARS for part in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")  # only the line breaks at the cuts go


def test_parts_break_at_line_ends_when_there_is_one() -> None:
    line = "l" * 100
    parts = split_message("\n".join([line] * 100))
    assert len(parts) > 1
    assert all(part.startswith(line) and part.endswith(line) for part in parts)
