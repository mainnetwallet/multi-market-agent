"""asyncio helpers.

``wait_for`` here replaces ``asyncio.wait_for``. On Python 3.11 (CI and the Docker image) the stdlib version can
swallow a cancellation that arrives just as the awaited future completes: the caller never sees ``CancelledError``
and its loop keeps running, so ``task.cancel()`` followed by ``await task`` hangs forever. ``asyncio.timeout`` does
not have that race, and on 3.12+ ``asyncio.wait_for`` is itself built on it, so behaviour there is identical.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def wait_for(aw: Awaitable[T], timeout: float | None) -> T:
    """Await ``aw`` for at most ``timeout`` seconds; raise ``TimeoutError`` after that, cancelling ``aw``."""
    async with asyncio.timeout(timeout):
        return await aw
