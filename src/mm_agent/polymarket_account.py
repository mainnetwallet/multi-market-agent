"""Read-only views of the signed-in Polymarket account (CLOB L2 routes, class R1).

Every call goes through the SDK secure client the adapter already owns. This module never reads
the environment, never sees the private key, and never returns a credential: API key ids are
masked and every field whose name looks like a secret is dropped before the answer leaves.

Nothing here changes state. Writes (orders, key management, heartbeat) are not part of this file.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .polymarket_data import DataAPIError, Validator, boolean, enum, integer, one_token
from .polymarket_explorer import csv_of, date, ident, text200

_SECRET_KEY = re.compile(r"secret|passphrase|signature|private|authorization|api[_-]?key|cookie|token_hash", re.I)
MAX_ITEMS = 500


def mask(value: str) -> str:
    text = str(value)
    return text[:4] + "…" + text[-4:] if len(text) > 10 else "…"


def plain(value: Any, depth: int = 0) -> Any:
    """Turn SDK models into JSON-safe data and drop anything that looks like a credential."""

    if depth > 8:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(k): plain(v, depth + 1) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, (list, tuple, set)):
        return [plain(v, depth + 1) for v in value][:MAX_ITEMS]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def drain(paginator: Any, cap: int = 200) -> dict[str, Any]:
    items: list[Any] = []
    complete = True
    async for page in paginator:
        items.extend(page.items)
        if page.limit_reached:
            complete = False
            break
        if len(items) >= cap:
            complete = not page.has_more and len(items) == cap  # cutting a longer page is not complete
            items = items[:cap]
            break
    return {"items": plain(items), "complete": complete}


@dataclass(frozen=True)
class AccountRoute:
    key: str
    label: str
    group: str
    params: Mapping[str, Validator] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    result: str = "object"
    evidence: str = "V"
    note: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "key": "account." + self.key,
            "group": self.group,
            "label": self.label,
            "host": "clob",
            "method": "GET",
            "path": "",
            "class": "R1",
            "path_params": [],
            "params": [{"name": n, "type": getattr(c, "__name__", "text"), "hint": ""} for n, c in self.params.items()],
            "needs_any": list(self.required),
            "needs_one": [],
            "result": self.result,
            "kind": "json",
            "rate_limit": 0,
            "evidence": self.evidence,
            "paging": "none",
            "note": self.note or "needs credentials",
            "needs_credentials": True,
        }


_ASSET = enum("COLLATERAL", "CONDITIONAL", "CONDITIONAL-V2")

ACCOUNT_ROUTES: dict[str, AccountRoute] = {
    r.key: r
    for r in (
        AccountRoute("order", "One of my orders", "wallet", {"order_id": ident}, ("order_id",)),
        AccountRoute(
            "balance",
            "Balance and allowance",
            "wallet",
            {"asset_type": _ASSET, "token_id": one_token},
            ("asset_type",),
            note="CONDITIONAL needs token_id",
        ),
        AccountRoute("rewards.day", "My rewards earnings for a day", "rewards", {"date": date}, ("date",), "table"),
        AccountRoute(
            "rewards.total",
            "My total rewards for a day",
            "rewards",
            {"date": date, "sponsored": boolean},
            ("date",),
            "table",
        ),
        AccountRoute("rewards.percentages", "My reward percentage per market", "rewards", result="object"),
        AccountRoute(
            "rewards.markets",
            "My earnings with market config",
            "rewards",
            {
                "date": date,
                "no_competition": boolean,
                "order_by": text200,
                "position": text200,
                "page_size": integer(1, 500),
            },
            ("date",),
            "table",
        ),
        AccountRoute(
            "scoring",
            "Are my orders scoring (liquidity rewards)",
            "rewards",
            {"order_ids": csv_of(ident, 100)},
            ("order_ids",),
            "object",
        ),
        AccountRoute("notifications", "My notifications", "wallet", result="table"),
        AccountRoute("api_keys", "My API keys (masked, list only)", "system", result="table"),
        AccountRoute("closed_only", "Closed-only mode status", "system", result="object"),
        AccountRoute(
            "builder_trades",
            "Builder trades",
            "rewards",
            {"builder_code": ident, "market": text200, "token_id": one_token},
            ("builder_code",),
            "table",
        ),
        AccountRoute("builder_fees", "Builder fee rates", "rewards", {"builder_code": ident}, ("builder_code",)),
    )
}


def _clean(route: AccountRoute, raw: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(k for k in raw if k not in route.params and raw[k] not in (None, ""))
    if unknown:
        raise ValueError(f"account.{route.key}: unknown parameter {unknown[0][:40]!r}")
    out: dict[str, Any] = {}
    for key, check in route.params.items():
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        out[key] = check(key, value)
    for key in route.required:
        if key not in out:
            raise ValueError(f"account.{route.key}: {key} is required")
    return out


class PolymarketAccount:
    """``get_client`` returns the SDK secure client or raises (the adapter's ``_secure``)."""

    def __init__(self, get_client: Callable[[], Awaitable[Any]]) -> None:
        self._get_client = get_client

    @staticmethod
    def catalog() -> list[dict[str, Any]]:
        return [route.describe() for route in ACCOUNT_ROUTES.values()]

    async def call(self, key: str, params: Mapping[str, Any] | None = None) -> Any:
        route = ACCOUNT_ROUTES.get(key)
        if route is None:
            raise KeyError(key)
        args = _clean(route, params or {})
        client = await self._get_client()
        if key == "order":
            return plain(await client.get_order(order_id=args["order_id"]))
        if key == "balance":
            asset = args["asset_type"]
            if asset != "COLLATERAL" and "token_id" not in args:
                raise ValueError("account.balance: token_id is required for CONDITIONAL")
            kwargs = {"asset_type": asset}
            if "token_id" in args:
                kwargs["token_id"] = args["token_id"]
            return plain(await client.get_balance_allowance(**kwargs))
        if key == "rewards.day":
            return await drain(client.list_user_earnings_for_day(date=args["date"]))
        if key == "rewards.total":
            sponsored = None if "sponsored" not in args else args["sponsored"] == "true"
            return {
                "items": plain(await client.get_total_earnings_for_user_for_day(date=args["date"], sponsored=sponsored))
            }
        if key == "rewards.percentages":
            return plain(await client.get_reward_percentages())
        if key == "rewards.markets":
            kwargs2: dict[str, Any] = {"date": args["date"]}
            if "no_competition" in args:
                kwargs2["no_competition"] = args["no_competition"] == "true"
            for name in ("order_by", "position"):
                if name in args:
                    kwargs2[name] = args[name]
            if "page_size" in args:
                kwargs2["page_size"] = int(args["page_size"])
            return await drain(client.list_user_earnings_and_markets_config(**kwargs2))
        if key == "scoring":
            ids = args["order_ids"].split(",")
            if len(ids) == 1:
                return {ids[0]: bool(await client.get_order_scoring(order_id=ids[0]))}
            return plain(await client.get_orders_scoring(order_ids=ids))
        if key == "notifications":
            return {"items": plain(await client.get_notifications())}
        if key == "api_keys":
            keys = await client.fetch_api_keys()
            return {"items": [{"id": mask(k)} for k in keys], "count": len(keys)}
        if key == "closed_only":
            return {"closed_only": bool(await client.get_closed_only_mode())}
        if key == "builder_trades":
            kwargs3 = {k: v for k, v in args.items() if k in ("builder_code", "market", "token_id")}
            return await drain(client.list_builder_trades(**kwargs3))
        if key == "builder_fees":
            return plain(await client.get_builder_fee_rates(args["builder_code"]))
        raise DataAPIError(404, "unknown account route")
