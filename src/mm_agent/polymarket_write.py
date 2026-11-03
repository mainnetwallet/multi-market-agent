"""Write-class Polymarket actions (class W). Everything here is OFF until an operator turns it on.

Gates, in the order the route applies them (``control_api``):

1. ``POLYMARKET_WRITE_API`` (global) and the family flag must both be true. Both default to false.
2. ``require_auth`` session, an ``Idempotency-Key`` header and a single-use step-up token
   (``X-Stepup-Token``, action ``polymarket_write``).
3. A typed acknowledgement for the actions that cannot be undone or that arm something
   (``confirm`` in the body, exact text in ``WriteAction.ack``).
4. An audit row for every attempt, before the venue is called.

What this module never does:

* It never reads a key, secret or auth header. It receives the SDK client from the adapter.
* It never retries. A call whose outcome is unknown is reported as ``UNKNOWN`` and stored by the
  route under the idempotency key, so a replay returns the stored answer and sends nothing.
* It is not a chat tool and the agent loop never calls it.
* It does not create or print secrets: new API keys are not built here (the secret would have to
  travel over HTTP).
* It does not move funds. Withdrawals, transfers, approvals and raw relayer submits stay in
  ``mm_agent.funds`` with its allowlist and caps.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .polymarket_account import plain
from .polymarket_data import DataAPIError, Validator, address, enum
from .polymarket_explorer import csv_of, ident

STEPUP_ACTION = "polymarket_write"
WRITE_API_FLAG = "polymarket_write_api"
FAMILY_FLAGS: dict[str, str] = {
    "orders": "polymarket_write_orders",
    "keys": "polymarket_write_keys",
    "combos_rfq": "polymarket_write_combos_rfq",
    "heartbeat": "polymarket_heartbeat_enabled",
}
FAMILY_ENV: dict[str, str] = {name: flag.upper() for name, flag in FAMILY_FLAGS.items()}

HEARTBEAT_ACK = "ENABLE HEARTBEAT: A RESTART CANCELS EVERY RESTING ORDER"
MAX_BATCH_ORDERS = 15
MAX_CANCEL_IDS = 1000
MAX_DROP_NOTIFICATIONS = 100
HEARTBEAT_SECONDS = 5.0


class WriteError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


_BIGINT = re.compile(r"[0-9]{1,100}")


def bigint(name: str, value: Any) -> str:
    text = str(value).strip()
    if not _BIGINT.fullmatch(text):
        raise ValueError(f"{name} must be a decimal id")
    return text


def amount(name: str, value: Any) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,8})?", text) or float(text) <= 0:
        raise ValueError(f"{name} must be a positive decimal")
    return text


def int_ids(name: str, value: Any) -> str:
    items = [part.strip() for part in str(value).split(",") if part.strip()]
    if not items or len(items) > MAX_DROP_NOTIFICATIONS:
        raise ValueError(f"{name} takes 1 to {MAX_DROP_NOTIFICATIONS} ids")
    return ",".join(dict.fromkeys(bigint(name, item) for item in items))


@dataclass(frozen=True)
class WriteAction:
    key: str
    family: str
    label: str
    params: Mapping[str, Validator] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    ack: str = ""  # exact text the body's ``confirm`` must equal; empty = no typed acknowledgement
    irreversible: bool = False
    note: str = ""

    def describe(self, settings: Any) -> dict[str, Any]:
        enabled, reason = gate_state(settings, self.family)
        return {
            "key": self.key,
            "family": self.family,
            "label": self.label,
            "class": "W",
            "method": "POST",
            "params": [{"name": n, "type": getattr(c, "__name__", "text")} for n, c in self.params.items()],
            "required": list(self.required),
            "ack": self.ack,
            "irreversible": self.irreversible,
            "enabled": enabled,
            "reason": reason,
            "env": [WRITE_API_FLAG.upper(), FAMILY_ENV[self.family]],
            "note": self.note,
        }


_SCOPES = csv_of(enum("ALL", "CLOB", "COMBOSRFQ"), 3)


def json_list(name: str, value: Any) -> Any:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON list")
    return value


ACTIONS: dict[str, WriteAction] = {
    a.key: a
    for a in (
        WriteAction(
            "orders.cancel_ids",
            "orders",
            "Cancel orders by id (DELETE /orders)",
            params={"order_ids": csv_of(ident, MAX_CANCEL_IDS)},
            required=("order_ids",),
            note="Cancels only; never places anything. Up to 1,000 ids.",
        ),
        WriteAction(
            "orders.batch",
            "orders",
            "Place up to 15 limit orders, one by one, each through the normal risk checks",
            params={"orders": json_list},  # each item is checked in ``_batch``
            required=("orders",),
            note=(
                "Not the venue's atomic POST /orders: every order goes through the same caps, ceilings and "
                "UNKNOWN handling as POST /v1/orders/limit, and the batch stops at the first failure."
            ),
        ),
        WriteAction(
            "notifications.drop",
            "orders",
            "Drop notifications (DELETE /notifications)",
            params={"ids": int_ids},
            required=("ids",),
        ),
        WriteAction(
            "keys.api.delete",
            "keys",
            "Delete the CLOB API key this app is using",
            ack="DELETE API KEY",
            irreversible=True,
            note="The app loses CLOB access until a new key is created and set in the environment.",
        ),
        WriteAction(
            "keys.builder.revoke",
            "keys",
            "Revoke the builder API key",
            ack="REVOKE BUILDER KEY",
            irreversible=True,
        ),
        WriteAction(
            "session.authorize",
            "keys",
            "Authorize a session signer",
            params={"address": address, "scopes": _SCOPES},
            required=("address",),
            ack="AUTHORIZE SESSION SIGNER",
            note="A session signer can act for the account inside its scopes. Default scope is CLOB only.",
        ),
        WriteAction(
            "session.revoke",
            "keys",
            "Revoke a session signer",
            params={"address": address},
            required=("address",),
        ),
        WriteAction(
            "combos.request_quote",
            "combos_rfq",
            "Request a combo quote (does not accept it)",
            params={
                "leg_position_ids": csv_of(bigint, 12),
                "direction": enum("BUY", "SELL"),
                "amount": amount,
                "size": amount,
                "side": enum("YES", "NO"),
            },
            required=("leg_position_ids", "direction"),
            note="Accepting a quote is not built: it would trade outside the per-trade cap.",
        ),
        WriteAction(
            "combos.collateral_plan",
            "combos_rfq",
            "Plan a collateral return (plan only, nothing is executed)",
        ),
        WriteAction(
            "heartbeat.send",
            "heartbeat",
            "Send one order heartbeat",
            params={"heartbeat_id": ident},
            ack=HEARTBEAT_ACK,
            irreversible=True,
            note="The first accepted send arms timeout cancellation of every open order and cannot be disarmed.",
        ),
    )
}


def gate_state(settings: Any, family: str) -> tuple[bool, str]:
    if not getattr(settings, WRITE_API_FLAG, False):
        return False, f"{WRITE_API_FLAG.upper()} is false"
    flag = FAMILY_FLAGS[family]
    if not getattr(settings, flag, False):
        return False, f"{flag.upper()} is false"
    return True, ""


def catalog(settings: Any) -> dict[str, Any]:
    return {
        "enabled": bool(getattr(settings, WRITE_API_FLAG, False)),
        "actions": [action.describe(settings) for action in ACTIONS.values()],
        "not_built": NOT_BUILT,
    }


# Official endpoints that are deliberately not routes here, with the reason (shown in the catalog).
NOT_BUILT: list[dict[str, str]] = [
    {
        "path": "POST /auth/api-key, /auth/derive-api-key, /auth/builder-api-key",
        "why": "the new secret would have to travel over HTTP; create keys with the SDK on a trusted machine",
    },
    {
        "path": "POST /submit, /relay-payload (raw relayer submit)",
        "why": "arbitrary on-chain calls; money movement stays in mm_agent.funds (allowlist, caps, step-up)",
    },
    {
        "path": "/v1/account/withdraw, transfer, approvals, execute_transaction",
        "why": "money out; use the /v1/funds routes with the allowlist",
    },
    {
        "path": "combo quote accept, /v1/collateral-return/submit",
        "why": "trades or moves funds outside the per-trade cap",
    },
    {
        "path": "Perps /v1/trade/*, /v1/account/* (orders, twaps, chases, auto-cancel)",
        "why": "separate product with leverage; outside the risk engine and caps",
    },
    {
        "path": "POST /balance-allowance/update",
        "why": "the SDK has no public method for it; the cache refresh happens inside order placement",
    },
]


async def _guard(call: Awaitable[Any]) -> dict[str, Any]:
    """Run one venue call. Any failure after the request left is UNKNOWN, never retried."""

    try:
        return {"outcome": "OK", "result": plain(await call)}
    except ValueError:
        raise
    except Exception as exc:
        status = getattr(exc, "status", None)
        if isinstance(status, int) and 400 <= status < 500:
            return {"outcome": "REJECTED", "status": status, "detail": str(exc)[:200]}
        return {"outcome": "UNKNOWN", "detail": type(exc).__name__}


PlaceOrder = Callable[..., Awaitable[dict[str, Any]]]


class PolymarketWriter:
    def __init__(
        self,
        get_client: Callable[[], Awaitable[Any]],
        place_order: PlaceOrder | None = None,
    ) -> None:
        self._get_client = get_client
        self._place_order = place_order

    async def execute(self, key: str, body: Mapping[str, Any], *, settings: Any, actor: str) -> dict[str, Any]:
        action = ACTIONS.get(key)
        if action is None:
            raise KeyError(key)
        enabled, reason = gate_state(settings, action.family)
        if not enabled:
            raise WriteError(403, "write_disabled", f"{key} is disabled: {reason}")
        args = self._clean(action, body)
        if action.ack and str(body.get("confirm", "")) != action.ack:
            raise WriteError(400, "ack_required", f'type the exact text in "confirm": {action.ack}')
        client = await self._get_client()
        if key == "orders.cancel_ids":
            return await _guard(client.cancel_orders(order_ids=args["order_ids"].split(",")))
        if key == "orders.batch":
            return await self._batch(args["orders"], actor)
        if key == "notifications.drop":
            return await _guard(client.drop_notifications(ids=args["ids"].split(",")))
        if key == "keys.api.delete":
            return await _guard(client.delete_api_key())
        if key == "keys.builder.revoke":
            return await _guard(client.revoke_builder_api_key())
        if key == "session.authorize":
            scopes = args.get("scopes", "CLOB").split(",")
            return await _guard(client.authorize_session_key(address=args["address"], scopes=scopes))
        if key == "session.revoke":
            return await _guard(client.revoke_session_key(address=args["address"]))
        if key == "combos.request_quote":
            if ("amount" in args) == ("size" in args):
                raise ValueError("combos.request_quote: give exactly one of amount, size")
            kwargs: dict[str, Any] = {
                "leg_position_ids": args["leg_position_ids"].split(","),
                "direction": args["direction"],
                "side": args.get("side", "YES"),
            }
            for name in ("amount", "size"):
                if name in args:
                    kwargs[name] = args[name]
            return await _guard(client.request_combo_quote(**kwargs))
        if key == "combos.collateral_plan":
            return await _guard(client.plan_collateral_return())
        if key == "heartbeat.send":
            return await _guard(client.send_order_heartbeat(heartbeat_id=args.get("heartbeat_id", "")))
        raise KeyError(key)

    @staticmethod
    def _clean(action: WriteAction, body: Mapping[str, Any]) -> dict[str, Any]:
        allowed = set(action.params) | {"confirm"}
        unknown = sorted(k for k in body if k not in allowed)
        if unknown:
            raise ValueError(f"{action.key}: unknown parameter {unknown[0][:40]!r}")
        out: dict[str, Any] = {}
        for name, check in action.params.items():
            value = body.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                if name in action.required:
                    raise ValueError(f"{action.key}: {name} is required")
                continue
            out[name] = check(name, value)
        return out

    async def _batch(self, orders: Any, actor: str) -> dict[str, Any]:
        if self._place_order is None:
            raise WriteError(503, "no_order_path", "the guarded order path is not available")
        if not isinstance(orders, list) or not 1 <= len(orders) <= MAX_BATCH_ORDERS:
            raise ValueError(f"orders must be a list of 1 to {MAX_BATCH_ORDERS} orders")
        keys = {"market_id", "outcome", "side", "price", "shares"}
        for index, item in enumerate(orders):
            if not isinstance(item, dict) or not keys <= set(item) or set(item) - keys - {"post_only", "client_id"}:
                raise ValueError(
                    f"orders[{index}] needs exactly: {', '.join(sorted(keys))} (+ optional post_only, client_id)"
                )
        results: list[dict[str, Any]] = []
        for item in orders:
            try:
                placed = await self._place_order(actor=actor, **item)
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
                results.append({"ok": False, "code": str(code), "detail": str(exc)[:200]})
                break  # stop at the first failure: a later order must not go out after an unknown one
            results.append({"ok": True, **plain(placed)})
        stopped = len(results) < len(orders) or any(not r.get("ok") for r in results)
        return {
            "outcome": "PARTIAL" if stopped else "OK",
            "placed": sum(1 for r in results if r.get("ok")),
            "results": results,
        }


class HeartbeatLoop:
    """Opt-in order heartbeat. Nothing starts it automatically; an operator starts it by hand.

    The venue arms timeout cancellation on the first accepted send. After that, a missed beat (a
    restart, a crash, a network outage) cancels every resting order. That is why starting needs the
    typed acknowledgement and a step-up token, and why there is no way to disarm it from here.
    """

    def __init__(
        self,
        get_client: Callable[[], Awaitable[Any]],
        *,
        interval: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._get_client = get_client
        self._interval = interval
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._heartbeat_id = ""
        self.sent = 0
        self.last_ok_at = 0.0
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self, settings: Any) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(settings, "polymarket_heartbeat_enabled", False)),
            "running": self.running,
            "sent": self.sent,
            "last_ok_at": self.last_ok_at or None,
            "last_error": self.last_error,
            "interval_seconds": self._interval,
            "warning": HEARTBEAT_ACK,
        }

    async def start(self, settings: Any, confirm: str) -> dict[str, Any]:
        if not (getattr(settings, WRITE_API_FLAG, False) and getattr(settings, "polymarket_heartbeat_enabled", False)):
            raise WriteError(
                403, "write_disabled", "POLYMARKET_WRITE_API and POLYMARKET_HEARTBEAT_ENABLED must be true"
            )
        if confirm != HEARTBEAT_ACK:
            raise WriteError(400, "ack_required", f'type the exact text in "confirm": {HEARTBEAT_ACK}')
        if self.running:
            return self.state(settings)
        self._task = asyncio.create_task(self._run(), name="polymarket-heartbeat")
        return self.state(settings)

    async def stop(self) -> None:
        """Stops sending. The venue keeps its timeout: orders are cancelled shortly after the last beat."""

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                client = await self._get_client()
                beat = await client.send_order_heartbeat(heartbeat_id=self._heartbeat_id)
                self._heartbeat_id = str(getattr(beat, "heartbeat_id", "") or self._heartbeat_id)
                self.sent += 1
                self.last_ok_at = self._clock()
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except DataAPIError as exc:
                self.last_error = f"http {exc.status}"
            except Exception as exc:
                expected = getattr(exc, "heartbeat_id", None)
                if isinstance(expected, str) and expected:
                    self._heartbeat_id = expected  # the venue told us the id it expects: use it next beat
                self.last_error = type(exc).__name__
            await asyncio.sleep(self._interval)
