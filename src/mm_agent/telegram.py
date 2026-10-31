"""Telegram operator bot with approval cards, alerts and a single poller."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import AgentService
from .chat import ChatService
from .config import Settings
from .domain import Proposal
from .events import Event, EventBus
from .store import Store
from .util import fmt_dhaka, get_logger

logger = get_logger(__name__)


class TelegramBot:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        agent: AgentService,
        chat: ChatService,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.store = store
        self.agent = agent
        self.chat = chat
        self.bus = bus
        self.application: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._lease_ok = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_allowed_user_id)

    async def start(self) -> None:
        if not self.enabled:
            logger.info("telegram_disabled")
            return
        if not self.store.try_poller_lease("telegram", self._owner):
            logger.error("telegram_poller_conflict", detail="another poller holds the lease; not starting")
            return
        self._lease_ok = True
        self._stop.clear()
        app = ApplicationBuilder().token(self.settings.telegram_bot_token).build()
        app.add_handler(CommandHandler("start", self._on_start))
        app.add_handler(MessageHandler(filters.COMMAND, self._on_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        self.application = app
        await app.initialize()
        await app.start()
        if app.updater is None:
            raise RuntimeError("Telegram updater unavailable")
        await app.updater.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
        self._tasks.append(asyncio.create_task(self._alert_loop(), name="telegram-alerts"))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="telegram-heartbeat"))
        self._tasks.append(asyncio.create_task(self._lease_loop(), name="telegram-lease"))
        await self._send("Multi-Market Agent online. Venue badge: " + self._badge())
        logger.info("telegram_started")

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.application is not None:
            try:
                if self.application.updater:
                    await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
            except Exception:
                logger.warning("telegram_shutdown_error")
            self.application = None
        if self._lease_ok:
            self.store.release_poller_lease("telegram", self._owner)
            self._lease_ok = False

    # --------------------------------------------------------------- handlers
    def _allowed(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id == self.settings.telegram_allowed_user_id)

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._allowed(update) or update.effective_message is None:
            return
        await update.effective_message.reply_text(
            "Multi-Market Agent\nSend /help for commands. All replies name the venue. "
            "Chat orders require CHAT_AUTOPILOT=true.",
        )

    async def _on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._allowed(update) or update.effective_message is None or not update.effective_message.text:
            return
        user = update.effective_user
        if user is None:
            return
        result = await self.chat.handle(
            update.effective_message.text,
            actor=f"telegram:{user.id}",
            source="telegram_chat",
        )
        await update.effective_message.reply_text(result.get("reply", "(no reply)")[:4000])

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._allowed(update) or update.effective_message is None or not update.effective_message.text:
            return
        user = update.effective_user
        if user is None:
            return
        result = await self.chat.handle(
            update.effective_message.text,
            actor=f"telegram:{user.id}",
            source="telegram_chat",
        )
        await update.effective_message.reply_text(result.get("reply", "(no reply)")[:4000])

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None or not self._allowed(update):
            return
        await query.answer()
        data = query.data or ""
        actor = f"telegram:{update.effective_user.id if update.effective_user else 'unknown'}"
        if data.startswith("approve:"):
            proposal_id = data.split(":", 1)[1]
            result = await self.agent.approve(proposal_id, actor=actor, via="telegram")
            if result.get("ok"):
                await query.edit_message_text(
                    f"{result.get('venue')} | status={result.get('status')} filled={result.get('filled')} "
                    f"remaining={result.get('remaining')}"
                )
            else:
                await query.edit_message_text(f"Approval failed: {result.get('error')}")
        elif data.startswith("reject:"):
            proposal_id = data.split(":", 1)[1]
            result = await self.agent.reject(proposal_id, actor=actor, via="telegram")
            await query.edit_message_text(
                "Proposal rejected." if result.get("ok") else f"Reject failed: {result.get('error')}"
            )
        elif data.startswith("panic:"):
            result = await self.agent.panic(actor=actor, confirm=True)
            await query.edit_message_text(f"PANIC executed. {len(result.get('cancellations', []))} venue result(s).")

    # ----------------------------------------------------------------- alerts
    async def _alert_loop(self) -> None:
        async with self.bus.subscribe() as stream:
            async for event in stream:
                try:
                    await self._maybe_alert(event)
                except Exception:
                    logger.warning("telegram_alert_failed", topic=event.topic)

    async def _maybe_alert(self, event: Event) -> None:
        if event.topic == "proposal.created":
            proposal = Proposal.model_validate(event.payload["proposal"])
            if not self.settings.chat_autopilot:
                await self._send_proposal(proposal)
        elif event.topic == "rule.blocked":
            await self._send(f"⚠ Exit rule blocked: {event.payload.get('reason')} on {event.payload.get('venue')}")
        elif event.topic == "risk.daily_loss_cap":
            await self._send(
                f"🚨 Daily loss cap reached on {event.payload.get('venue')} "
                f"(PnL {event.payload.get('pnl')}). New entries are paused; exits keep running."
            )
        elif event.topic == "venue.auth_failure":
            await self._send(
                f"🚨 Venue authentication failed on {event.payload.get('venue')}: "
                f"{event.payload.get('detail')}. No orders will be placed there."
            )
        elif event.topic == "venue.unavailable":
            await self._send(f"⚠ Venue unavailable: {event.payload.get('venue')} — {event.payload.get('reason')}")
        elif event.topic == "agent.panic":
            await self._send(
                "🚨 PANIC executed. All open orders cancelled; rules cancelled. Positions are not auto-sold."
            )
        elif event.topic == "venues.active_changed":
            await self._send(f"Venue badge: {self._badge()}")
        elif event.topic == "order.updated":
            order = event.payload.get("order", {})
            if order.get("status") == "UNKNOWN":
                await self._send(
                    f"🚨 UNKNOWN order on {order.get('venue')}. Do not retry; reconcile before new orders."
                )
        elif event.topic == "account.external_change":
            await self._send_external_change(event)
        elif event.topic == "scanner.tick":
            for venue, diag in (event.payload.get("feed_diag", {}).get("venues") or {}).items():
                if diag.get("error"):
                    await self._send(f"⚠ Feed error on {venue}: {diag['error']}")

    async def _send_external_change(self, event: Event) -> None:
        """Tell the operator about a trade made outside the agent, e.g. on the venue's own website."""

        venue = str(event.payload.get("venue") or "").upper()
        lines = [f"📱 {venue}: account changed outside the agent"]
        trades = event.payload.get("trades") or []
        for trade in trades[:8]:
            verb = "bought" if trade.get("side") == "BUY" else "sold/closed"
            lines.append(
                f"{verb} {trade.get('shares')} {trade.get('outcome')} @ ~{trade.get('price')} "
                f"(now {trade.get('position_after')}) · {str(trade.get('title'))[:90]}"
            )
        if len(trades) > 8:
            lines.append(f"…and {len(trades) - 8} more")
        lines.append("Price is derived from the position, not a venue fill. If this was not you, use /panic.")
        await self._send("\n".join(lines))

    async def _send_proposal(self, proposal: Proposal) -> None:
        text = (
            f"{proposal.venue.value.upper()} | LIVE\n"
            f"{proposal.outcome.value} {proposal.size} @ {proposal.price * 100:.1f}c = ${proposal.amount_usd}\n"
            f"edge {(proposal.edge * 100):.1f}pp · p_fair {(proposal.p_fair * 100):.1f}%\n"
            f"{proposal.market_title[:180]}\n"
            f"expires {fmt_dhaka(proposal.expires_at)} · id {proposal.short_id}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"approve:{proposal.id}"),
                    InlineKeyboardButton("Reject", callback_data=f"reject:{proposal.id}"),
                ],
                [InlineKeyboardButton("PANIC", callback_data="panic:now")],
            ]
        )
        await self._send(text, reply_markup=keyboard)

    async def _send(self, text: str, *, reply_markup: Any = None) -> None:
        if self.application is None:
            return
        await self.application.bot.send_message(
            chat_id=self.settings.telegram_allowed_user_id,
            text=text[:4000],
            reply_markup=reply_markup,
        )

    async def _heartbeat_loop(self) -> None:
        interval = max(300, self.settings.telegram_heartbeat_seconds)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                status = self.agent.status()
                await self._send(
                    f"heartbeat · {status['badge']} · paused={status['paused']} · "
                    f"positions={status['counts']['open_positions']} · unknown={status['counts']['unknown_orders']} · "
                    f"{status['server_time_dhaka']}"
                )

    async def _lease_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=120)
            except TimeoutError:
                if not self.store.try_poller_lease("telegram", self._owner, ttl_seconds=240):
                    logger.error("telegram_lease_lost")
                    await self._send("Telegram poller lease lost; stopping to avoid duplicate polling.")
                    await self.stop()
                    return

    def _badge(self) -> str:
        status = self.agent.status()
        return str(status["badge"])
