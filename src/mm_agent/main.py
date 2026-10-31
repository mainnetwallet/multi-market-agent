"""Application entrypoint: one asyncio process for scanner, books, rules, API and bot."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import make_url

from .agent import AgentService
from .books import BookCache, UnitGuard
from .chat import ChatService
from .config import Settings, get_settings
from .control_api import create_app
from .domain import VenueId
from .events import EventBus
from .rules import RulesEngine
from .scanner import Scanner
from .store import Store
from .telegram import TelegramBot
from .util import configure_logging, get_logger
from .venues.base import MarketQuery, Venue
from .venues.registry import VenueRegistry

logger = get_logger(__name__)


@dataclass
class Runtime:
    settings: Settings
    store: Store
    bus: EventBus
    registry: VenueRegistry
    scanner: Scanner
    agent: AgentService
    rules: RulesEngine
    chat: ChatService
    telegram: TelegramBot
    app: FastAPI
    started: bool = False

    async def serve(self) -> None:
        await _serve_runtime(self)


def _run_migrations(settings: Settings) -> None:
    """Apply Alembic migrations. Source checkouts carry alembic.ini."""

    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    config_path = Path.cwd() / "alembic.ini"
    if not config_path.exists():
        raise RuntimeError("alembic.ini not found; run the agent from the repository root")
    cfg = Config(str(config_path))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


def build_runtime(
    settings: Settings,
    *,
    bind_database: bool = True,
    test_adapters: dict[VenueId, Venue] | None = None,
) -> Runtime:
    settings.ensure_data_dir()
    store = Store(settings.database_url)
    if bind_database:
        _run_migrations(settings)
    bus = EventBus()
    registry = VenueRegistry(settings, store, bus, test_adapters=test_adapters)
    books = BookCache(settings.book_fresh_seconds)
    guard = UnitGuard()
    scanner = Scanner(settings, store, registry, bus)
    agent = AgentService(settings, store, registry, bus, scanner, books, guard, None)
    rules = RulesEngine(settings, store, registry, books, agent, bus)
    chat = ChatService(settings, agent)
    telegram = TelegramBot(settings, store, agent, chat, bus)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        await startup()
        try:
            yield
        finally:
            await shutdown()

    async def startup() -> None:
        logger.info(
            "startup",
            env=settings.app_env,
            database=make_url(settings.database_url).render_as_string(hide_password=True).split("?")[0],
        )
        # An answer that was running when the server stopped did not finish; say so when it is reopened.
        interrupted = store.chat_interrupt_stale_runs()
        if interrupted:
            logger.warning("chat_runs_interrupted", count=interrupted)
        await registry.start()
        try:
            await registry.refresh_all()
        except Exception:
            logger.exception("doctor_refresh_failed")
        try:
            guard.check_math()
            checked = 0
            for venue in registry.enabled():
                if not registry.is_ready(venue):
                    continue
                adapter = registry.get(venue)
                markets = await adapter.list_markets(MarketQuery(limit=1))
                if not markets.items:
                    guard.halt(f"{venue.value}: no market available for startup unit check")
                    break
                await guard.run(adapter, markets.items[0])
                checked += 1
            if checked == 0:
                guard.halt("no ready venue for startup unit check")
        except Exception as exc:
            guard.halt(f"{type(exc).__name__}: {str(exc)[:200]}")
            await bus.publish("feed.down", reason=guard.reason)
        try:
            await scanner.scan_once()
        except Exception:
            logger.exception("initial_scan_failed")
        scanner.start()
        await agent.start()
        rules.start()
        await telegram.start()
        status = agent.status()
        logger.info(
            "agent_ready",
            badge=status["badge"],
            unit_guard=guard.ok,
            venues=status["venues"],
        )
        if not guard.ok:
            logger.error("unit_guard_halted_trading", reason=guard.reason)

    async def shutdown() -> None:
        logger.info("shutdown_begin")
        await telegram.stop()
        await rules.stop()
        await agent.stop()
        await scanner.stop()
        await registry.close()
        store.close()
        logger.info("shutdown_complete")

    app = create_app(settings, agent, scanner, registry, chat, bus, lifespan=lifespan)
    return Runtime(settings, store, bus, registry, scanner, agent, rules, chat, telegram, app)


async def serve(settings: Settings) -> None:
    if not settings.admin_password or not settings.session_secret:
        logger.error(
            "missing_admin_config",
            detail="Set ADMIN_PASSWORD and SESSION_SECRET in .env before starting the Control API.",
        )
        raise SystemExit(2)
    runtime = build_runtime(settings)
    await runtime.serve()


async def _serve_runtime(runtime: Runtime) -> None:
    config = uvicorn.Config(
        runtime.app,
        host=runtime.settings.host,
        port=runtime.settings.port,
        log_level="info",
        proxy_headers=False,
        server_header=False,
    )
    server = uvicorn.Server(config)
    runtime.started = True
    await server.serve()


def run() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.admin_password or not settings.session_secret:
        print("Refusing to start: set ADMIN_PASSWORD and SESSION_SECRET in .env", file=sys.stderr)
        raise SystemExit(2)
    if settings.session_secret in {"change-me-to-a-long-random-stable-string", "unset-secret"}:
        print("Refusing to start: SESSION_SECRET is still the example value", file=sys.stderr)
        raise SystemExit(2)
    if settings.admin_password == "change-me-now":
        print("Refusing to start: ADMIN_PASSWORD is still the example value", file=sys.stderr)
        raise SystemExit(2)
    print(f"Starting multi-market-agent on http://{settings.host}:{settings.port}")
    print("Every order is real money. Use a burner wallet and a small balance.")
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    run()
