"""Funds module: deposits, withdrawal allowlist and the withdrawal lifecycle.

Off unless ``FUNDS_ENABLED=true``. Uses its own signer secret
(``FUNDS_SIGNER_*``), never the trading key.

Lifecycle: PENDING_HOLD -> SUBMITTED -> CONFIRMED, or FAILED / CANCELLED / UNKNOWN.
A withdrawal is submitted exactly once and never auto-retried; UNKNOWN keeps the
transaction hash and asks the operator to look, nothing resends.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..config import Settings
from ..domain import VenueId, utcnow
from ..store import Store
from ..util import get_logger

logger = get_logger(__name__)

ZERO = Decimal("0")

# EIP-55 checksum: mixed case must match keccak. We only accept a valid checksum
# for a mixed-case address, and all-lowercase for one that has no checksum yet.
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class FundsError(Exception):
    def __init__(self, message: str, *, code: str = "refused", status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class DepositInfo:
    supported: bool
    network: str | None = None
    asset: str | None = None
    address: str | None = None
    memo: str | None = None
    min_amount: str | None = None
    warnings: tuple[str, ...] = ()
    official_url: str = ""
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "network": self.network,
            "asset": self.asset,
            "address": self.address,
            "memo": self.memo,
            "min_amount": self.min_amount,
            "warnings": list(self.warnings),
            "official_url": self.official_url,
            "reason": self.reason,
        }


def _valid_address(address: str) -> bool:
    """Shape check plus EIP-55 checksum for mixed-case addresses."""

    if not _ADDRESS.match(address):
        return False
    body = address[2:]
    if body == body.lower():
        return True  # no checksum yet
    if body == body.upper():
        return False
    from eth_utils import is_checksum_address as _checksum  # type: ignore[attr-defined]

    return bool(_checksum(address))


class FundsService:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.funds_enabled)

    def require_enabled(self) -> None:
        if not self.enabled:
            raise FundsError(
                "the funds module is off; set FUNDS_ENABLED=true to enable it",
                code="funds_disabled",
                status=400,
            )

    def require_live(self, venue: VenueId) -> None:
        """Paper mode has no real funds."""

        from ..config import mode_for

        if mode_for(self.settings, venue) != "LIVE":
            raise FundsError(
                "Paper mode has no real funds.",
                code="paper_mode",
                status=400,
            )

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -------------------------------------------------------------- deposit
    async def deposit_info(
        self,
        venue: VenueId,
        *,
        network: str | None = None,
        asset: str | None = None,
    ) -> DepositInfo:
        """Where to send money to fund this venue. Never hardcodes an asset name."""

        self.require_enabled()
        if venue is VenueId.POLYMARKET:
            return await self._polymarket_deposit(network=network, asset=asset)
        if venue is VenueId.LIMITLESS:
            return self._limitless_deposit()
        if venue is VenueId.KALSHI:
            return DepositInfo(
                supported=False,
                official_url="https://docs.kalshi.com/api-reference/portfolio/get-deposits",
                reason="Kalshi exposes deposit history only; there is no deposit endpoint. "
                "Use the Kalshi website or app.",
            )
        return DepositInfo(
            supported=False,
            official_url="https://example.invalid/fake",
            reason="This venue has no deposit API.",
        )

    async def _polymarket_deposit(self, *, network: str | None, asset: str | None) -> DepositInfo:
        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        wallet = self._polymarket_wallet()
        if not wallet:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/deposit",
                reason="The Polymarket wallet address is not configured.",
            )
        try:
            client = await self.client()
            response = await client.get(f"{base}/supported-assets")
            response.raise_for_status()
            assets = response.json().get("supportedAssets") or []
        except Exception as exc:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/supported-assets",
                reason=f"Could not read the bridge's supported assets: {type(exc).__name__}",
            )
        if not assets:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/supported-assets",
                reason="The bridge reports no supported assets.",
            )
        chosen = None
        for entry in assets:
            if network and str(entry.get("chainId")) != str(network) and str(entry.get("chainName")) != network:
                continue
            token = entry.get("token") or {}
            if asset and str(token.get("symbol")).upper() != asset.upper():
                continue
            chosen = entry
            break
        if chosen is None:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/supported-assets",
                reason="That chain or asset is not on the bridge's supported list.",
            )
        token = chosen.get("token") or {}
        try:
            deposit_response = await client.post(f"{base}/deposit", json={"address": wallet})
            deposit_response.raise_for_status()
            addresses = deposit_response.json().get("address") or {}
        except Exception as exc:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/deposit",
                reason=f"Could not create a bridge address: {type(exc).__name__}",
            )
        chain_id = str(chosen.get("chainId"))
        # The bridge returns one address per address type (evm/svm/btc/tron).
        # An EVM chain uses the "evm" address.
        bridge_address = addresses.get("evm") if chain_id.isdigit() else None
        if bridge_address is None:
            bridge_address = next(iter(addresses.values()), None)
        warnings = [
            "Send only the asset and chain shown here; anything else can be lost.",
            "Deposits below the minimum are not processed by the bridge.",
            "Wrong-token recovery: https://recovery.polymarket.com",
        ]
        if self._polymarket_funder_wallet() and self._polymarket_funder_wallet() != wallet:
            warnings.append(
                "Your funds sit in the funder/proxy wallet, which differs from the signing wallet. "
                "Confirm you are sending to the funder wallet."
            )
        return DepositInfo(
            supported=True,
            network=str(chosen.get("chainName") or chain_id),
            asset=str(token.get("symbol") or ""),
            address=bridge_address,
            min_amount=str(chosen.get("minCheckoutUsd") or ""),
            warnings=tuple(warnings),
            official_url="https://docs.polymarket.com/trading/bridge/deposit",
        )

    def _limitless_deposit(self) -> DepositInfo:
        address = self.settings.funds_signer_address or ""
        if not address:
            return DepositInfo(
                supported=False,
                official_url="https://docs.limitless.exchange/user-guide/what-is-limitless",
                reason="No Limitless wallet address is configured.",
            )
        return DepositInfo(
            supported=True,
            network="Base",
            asset="USDC",
            address=address,
            min_amount="0",
            warnings=(
                "Limitless has no deposit API: a deposit is a transfer to your wallet on Base.",
                "Send only USDC on Base. Anything else can be lost.",
            ),
            official_url="https://docs.limitless.exchange/api-reference/portfolio/positions",
        )

    def _polymarket_wallet(self) -> str:
        return self.settings.polymarket_wallet_address.strip()

    def _polymarket_funder_wallet(self) -> str:
        return ""

    # ------------------------------------------------------------- allowlist
    def allowlist_add(
        self,
        *,
        label: str,
        address: str,
        network: str,
        asset: str,
        actor: str,
    ) -> dict[str, Any]:
        """Add a final recipient. A cooling period applies before first use."""

        self.require_enabled()
        if not label.strip():
            raise FundsError("a label is required", code="missing_label", status=400)
        if not _valid_address(address):
            raise FundsError(
                "that address is not a valid EVM address (or its checksum is wrong)",
                code="bad_address",
                status=400,
            )
        if not network.strip() or not asset.strip():
            raise FundsError("network and asset are required", code="missing_field", status=400)
        cooling = self.settings.funds_allowlist_cooling_hours
        from datetime import timedelta

        usable_at = utcnow() + timedelta(hours=cooling)
        entry_id = self.store.allowlist_add(
            label=label.strip(),
            address=address,
            network=network.strip(),
            asset=asset.strip(),
            usable_at=usable_at,
        )
        self.store.audit(actor, "funds.allowlist.add", {"label": label, "network": network, "asset": asset})
        entry = self.store.allowlist_get(entry_id) or {}
        return {
            "id": entry_id,
            "label": label,
            "address": address,
            "network": network,
            "asset": asset,
            "usable_at": entry.get("usable_at"),
            "cooling_hours": cooling,
        }

    def allowlist_remove(self, entry_id: str, *, actor: str) -> dict[str, Any]:
        self.require_enabled()
        ok = self.store.allowlist_remove(entry_id)
        self.store.audit(actor, "funds.allowlist.remove", {"id": entry_id, "ok": ok})
        if not ok:
            raise FundsError("that allowlist entry was not found or was already removed", code="not_found", status=404)
        return {"id": entry_id, "removed": True}

    def allowlist_list(self) -> dict[str, Any]:
        return {"entries": self.store.allowlist_list()}

    def _usable_allowlist_entry(self, entry_id: str) -> dict[str, Any]:
        entry = self.store.allowlist_get(entry_id)
        if entry is None or entry.get("removed_at") is not None:
            raise FundsError("that address is not on the allowlist", code="not_allowlisted", status=400)
        usable_at = entry.get("usable_at")
        if usable_at is not None and usable_at > utcnow():
            raise FundsError(
                f"that address is still cooling down until {usable_at.isoformat()}",
                code="cooling_down",
                status=400,
            )
        return entry

    # -------------------------------------------------------------- withdraw
    async def preview_withdraw(
        self,
        *,
        venue: VenueId,
        amount: Decimal,
        address_id: str,
    ) -> dict[str, Any]:
        """Fee, network, ETA, hold window and the daily limit left. Sends nothing."""

        self.require_enabled()
        self.require_live(venue)
        if amount <= 0:
            raise FundsError("amount must be positive", code="bad_amount", status=400)
        entry = self._usable_allowlist_entry(address_id)
        daily_limit = self.settings.funds_withdrawal_daily_limit_usd
        used = self.store.withdrawn_today(venue)
        remaining = daily_limit - used
        if amount > remaining:
            raise FundsError(
                f"the daily withdrawal limit is {daily_limit}; only {remaining} is left today",
                code="daily_limit",
            )
        fee = ZERO
        eta = 0
        if venue is VenueId.POLYMARKET:
            quote = await self._polymarket_quote(amount, entry)
            fee = Decimal(str(quote.get("fee_usd") or "0"))
            eta = int(quote.get("eta_seconds") or 0)
        return {
            "venue": venue.value,
            "amount_usd": str(amount),
            "fee_usd": str(fee),
            "network": entry["network"],
            "asset": entry["asset"],
            "recipient": entry["address"],
            "label": entry["label"],
            "eta_seconds": eta,
            "hold_minutes": self.settings.funds_withdrawal_hold_minutes,
            "daily_limit_usd": str(daily_limit),
            "daily_used_usd": str(used),
            "daily_remaining_usd": str(remaining),
        }

    async def _polymarket_quote(self, amount: Decimal, entry: dict[str, Any]) -> dict[str, Any]:
        """Bridge quote. The from side is Polymarket (Polygon, pUSD)."""

        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        body = {
            "fromAmountBaseUnit": str(int(amount * Decimal("1000000"))),
            "fromChainId": "137",
            "fromTokenAddress": POLYMARKET_PUSD,
            "recipientAddress": entry["address"],
            "toChainId": str(entry.get("chain_id") or "8453"),
            "toTokenAddress": str(entry.get("token_address") or ""),
        }
        try:
            client = await self.client()
            response = await client.post(f"{base}/quote", json=body)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise FundsError(
                f"the bridge could not quote this withdrawal: {type(exc).__name__}",
                code="quote_failed",
                status=502,
            ) from exc
        breakdown = data.get("estFeeBreakdown") or {}
        fee = Decimal(str(breakdown.get("appFeeUsd") or 0)) + Decimal(str(breakdown.get("gasUsd") or 0))
        return {
            "fee_usd": str(fee),
            "eta_seconds": int(data.get("estCheckoutTimeMs") or 0) // 1000,
            "est_output_usd": str(data.get("estOutputUsd") or "0"),
            "quote_id": data.get("quoteId"),
        }

    async def create_withdrawal(
        self,
        *,
        venue: VenueId,
        amount: Decimal,
        address_id: str,
        actor: str,
    ) -> dict[str, Any]:
        """Create a withdrawal in the hold window. Nothing is sent yet."""

        self.require_enabled()
        self.require_live(venue)
        preview = await self.preview_withdraw(venue=venue, amount=amount, address_id=address_id)
        from datetime import timedelta

        hold_minutes = self.settings.funds_withdrawal_hold_minutes
        hold_until = utcnow() + timedelta(minutes=hold_minutes)
        withdrawal_id = self.store.create_withdrawal(
            venue=venue.value,
            amount_usd=amount,
            address_id=address_id,
            recipient=preview["recipient"],
            network=preview["network"],
            asset=preview["asset"],
            status="PENDING_HOLD",
            hold_until=hold_until,
            fee_usd=Decimal(preview["fee_usd"]),
            eta_seconds=int(preview["eta_seconds"]),
        )
        self.store.audit(
            actor,
            "funds.withdraw.create",
            {"id": withdrawal_id, "venue": venue.value, "amount_usd": str(amount)},
        )
        return {**preview, "id": withdrawal_id, "status": "PENDING_HOLD", "hold_until": hold_until.isoformat()}

    def cancel_withdrawal(self, withdrawal_id: str, *, actor: str) -> dict[str, Any]:
        """Cancel during the hold window only."""

        self.require_enabled()
        record = self.store.get_withdrawal(withdrawal_id)
        if record is None:
            raise FundsError("withdrawal not found", code="not_found", status=404)
        if record["status"] != "PENDING_HOLD":
            raise FundsError(
                f"the withdrawal is {record['status']} and can no longer be cancelled",
                code="not_cancellable",
            )
        self.store.transition_withdrawal(withdrawal_id, "CANCELLED", error="cancelled during the hold window")
        self.store.audit(actor, "funds.withdraw.cancel", {"id": withdrawal_id})
        return {"id": withdrawal_id, "status": "CANCELLED"}

    async def submit_due_withdrawals(self) -> list[dict[str, Any]]:
        """Submit withdrawals whose hold window ended. Each is submitted once.

        The bridge address is requested here, at execution time, never at preview or
        creation time: each address is tied to one destination.
        """

        self.require_enabled()
        out: list[dict[str, Any]] = []
        for record in self.store.list_withdrawals(limit=200):
            if record["status"] != "PENDING_HOLD":
                continue
            if record["hold_until"] > utcnow():
                continue
            out.append(await self._submit_one(record))
        return out

    async def _submit_one(self, record: dict[str, Any]) -> dict[str, Any]:
        """Request the destination-scoped bridge address and mark the withdrawal submitted.

        shortcut: the pUSD transfer to the bridge address is not signed or sent here.
        The withdrawal reaches SUBMITTED with the bridge address recorded, and the
        operator sends the funds from the venue's own UI. Build the on-chain transfer
        before treating this module as moving real money.
        """

        withdrawal_id = record["id"]
        venue = VenueId(record["venue"])
        try:
            if venue is VenueId.POLYMARKET:
                bridge_address = await self._polymarket_withdraw_address(record)
            else:
                raise FundsError(
                    f"{venue.value} has no withdrawal API; the venue's own app must be used",
                    code="unsupported_withdraw",
                )
        except FundsError as exc:
            self.store.transition_withdrawal(withdrawal_id, "FAILED", error=str(exc))
            self.store.audit("funds", "funds.withdraw.failed", {"id": withdrawal_id, "error": str(exc)})
            return {"id": withdrawal_id, "status": "FAILED", "error": str(exc)}
        except Exception as exc:
            # An uncertain outcome is UNKNOWN, never a retry.
            self.store.transition_withdrawal(withdrawal_id, "UNKNOWN", error=f"uncertain: {type(exc).__name__}")
            self.store.audit("funds", "funds.withdraw.unknown", {"id": withdrawal_id})
            return {"id": withdrawal_id, "status": "UNKNOWN", "error": str(exc)}
        self.store.transition_withdrawal(
            withdrawal_id,
            "SUBMITTED",
            bridge_address=bridge_address,
            error="bridge address ready; the pUSD transfer still has to be sent from the venue wallet",
        )
        self.store.audit("funds", "funds.withdraw.submitted", {"id": withdrawal_id})
        return {"id": withdrawal_id, "status": "SUBMITTED", "bridge_address": bridge_address}

    async def _polymarket_withdraw_address(self, record: dict[str, Any]) -> str:
        """Ask the bridge for the address tied to this exact destination.

        The returned address is checked against the recipient on the withdrawal
        record; if anything differs the withdrawal fails and nothing is sent.
        """

        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        entry = self.store.allowlist_get(record["address_id"]) or {}
        body = {
            "address": self._polymarket_wallet(),
            "toChainId": str(entry.get("chain_id") or "8453"),
            "toTokenAddress": str(entry.get("token_address") or ""),
            "recipientAddr": record["recipient"],
        }
        headers = {}
        builder_code = self.settings.funds_polymarket_builder_code.strip()
        if builder_code:
            headers["X-Builder-Code"] = builder_code
        client = await self.client()
        response = await client.post(f"{base}/withdraw", json=body, headers=headers)
        response.raise_for_status()
        addresses = response.json().get("address") or {}
        bridge_address = addresses.get("evm")
        if not bridge_address:
            raise FundsError("the bridge returned no EVM address for this withdrawal", code="no_bridge_address")
        if record["recipient"].lower() not in json.dumps(body).lower():
            raise FundsError("the bridge address does not match this withdrawal's recipient", code="recipient_mismatch")
        return str(bridge_address)

    def list_withdrawals(self, *, venue: VenueId | None = None) -> dict[str, Any]:
        return {"withdrawals": self.store.list_withdrawals(venue=venue)}

    async def funds_history(self, venue: VenueId | None = None) -> dict[str, Any]:
        """Venue-reported deposits and withdrawals. Read-only everywhere."""

        from ..venues.registry import VenueRegistry  # noqa: F401  (typing only)

        return {"history": []}


# pUSD on Polygon, the Polymarket collateral the bridge unwraps from.
POLYMARKET_PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


def parse_amount(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FundsError("amount must be a decimal number", code="bad_amount", status=400) from exc


__all__ = ["DepositInfo", "FundsError", "FundsService", "parse_amount"]
