"""Funds module: deposits, withdrawal allowlist and the withdrawal lifecycle.

Off unless ``FUNDS_ENABLED=true``. Uses its own signer secret
(``FUNDS_SIGNER_*``), never the trading key.

Lifecycle: PENDING_HOLD -> SUBMITTED -> CONFIRMED, or FAILED / CANCELLED / UNKNOWN.
A withdrawal is submitted exactly once and never auto-retried; UNKNOWN keeps the
transaction hash and asks the operator to look, nothing resends.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from ..config import Settings
from ..domain import VenueId, utcnow
from ..store import Store
from ..util import get_logger
from ..venues import limitless_signing, polymarket_signing
from .signer import (
    POLYGON_CHAIN_ID,
    SignerError,
    send_erc20_transfer,
    to_base_units,
)

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
    chain_id: str | None = None
    address_type: str | None = None
    token_name: str | None = None
    token_address: str | None = None
    wallet: str | None = None

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
            "chain_id": self.chain_id,
            "address_type": self.address_type,
            "token_name": self.token_name,
            "token_address": self.token_address,
            "wallet": self.wallet,
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
        self._bridge_addresses: dict[str, dict[str, str]] = {}

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
        token: str | None = None,
    ) -> DepositInfo:
        """Where to send money to fund this venue. Never hardcodes an asset name."""

        self.require_enabled()
        if venue is VenueId.POLYMARKET:
            return await self._polymarket_deposit(network=network, asset=asset, token=token)
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
            reason="This venue has no deposit API.",
        )

    async def _bridge_addresses_for(self, wallet: str) -> dict[str, str]:
        """The wallet's bridge addresses by type (evm, svm, btc, tron), asked for once and remembered.

        `POST /deposit` returns one address per address type. The bridge creates them per
        request, so asking on every refresh would leave a trail of unused addresses behind.
        """
        if wallet in self._bridge_addresses:
            return self._bridge_addresses[wallet]
        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        client = await self.client()
        response = await client.post(f"{base}/deposit", json={"address": wallet})
        response.raise_for_status()
        raw = response.json().get("address") or {}
        addresses = {str(kind): str(value) for kind, value in raw.items() if value}
        if addresses:
            self._bridge_addresses[wallet] = addresses
        return addresses

    async def _polymarket_assets(self) -> list[dict[str, Any]]:
        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        client = await self.client()
        response = await client.get(f"{base}/supported-assets")
        response.raise_for_status()
        return list(response.json().get("supportedAssets") or [])

    @classmethod
    def _group_assets(cls, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The bridge's flat asset list as chains with their tokens.

        Nothing here names a chain or a token. The order comes from the data alone: chains with the lowest
        minimum deposit first, then the chains offering more tokens, then the bridge's own order; tokens that
        the most chains list first (the widely available ones), then the bridge's own order.
        """
        chains: dict[str, dict[str, Any]] = {}
        for entry in assets:
            chain_id = str(entry.get("chainId") or "")
            chain_name = str(entry.get("chainName") or chain_id)
            token = entry.get("token") or {}
            symbol = str(token.get("symbol") or "")
            if not chain_id or not symbol:
                continue
            chain = chains.setdefault(
                chain_id,
                {"chain_id": chain_id, "chain_name": chain_name, "min_usd": None, "tokens": []},
            )
            min_usd = entry.get("minCheckoutUsd")
            chain["tokens"].append(
                {
                    "symbol": symbol,
                    "name": str(token.get("name") or symbol),
                    "address": str(token.get("address") or ""),
                    "decimals": token.get("decimals"),
                    "min_usd": None if min_usd is None else str(min_usd),
                }
            )
            if min_usd is not None and (chain["min_usd"] is None or float(min_usd) < float(chain["min_usd"])):
                chain["min_usd"] = str(min_usd)

        listed = list(chains.values())
        offered_on: dict[str, int] = {}
        for chain in listed:
            for symbol in {str(t["symbol"]).upper() for t in chain["tokens"]}:
                offered_on[symbol] = offered_on.get(symbol, 0) + 1
        for chain in listed:
            chain["tokens"] = [
                token
                for _, token in sorted(
                    enumerate(chain["tokens"]),
                    key=lambda pair: (-offered_on[str(pair[1]["symbol"]).upper()], pair[0]),
                )
            ]
        inf = float("inf")
        return [
            chain
            for _, chain in sorted(
                enumerate(listed),
                key=lambda pair: (
                    float(pair[1]["min_usd"]) if pair[1]["min_usd"] is not None else inf,
                    -len(pair[1]["tokens"]),
                    pair[0],
                ),
            )
        ]

    _EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
    _BASE58_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
    _BECH32_ADDRESS = re.compile(r"(?:bc1|tb1)[02-9ac-hj-np-z]{11,71}", re.IGNORECASE)
    # The bridge lists a chain's native asset under `0xEeee...` and a pseudo-chain with no token contract
    # (a perps account) under an all-zero address. Such a marker looks like an EVM address on every chain,
    # so it says nothing about which kind of address the chain really uses.
    _MARKER_ADDRESS = re.compile(r"0x(?:[eE]{40}|0+)")

    @classmethod
    def _address_families(cls, token_address: str) -> set[str]:
        """Which bridge address types a token contract address can belong to, from its syntax alone.

        The bridge's asset list says nothing about a chain's address type, so it is read from the token
        address: `0x` plus 40 hex is an EVM address; `bc1`/`tb1` plus bech32 characters is a Bitcoin address;
        Base58 of 32 to 44 characters is a Solana address, and a 34-character one starting with `T` can also
        be a Tron address. An unrecognised format gives no answer.
        """
        if cls._EVM_ADDRESS.fullmatch(token_address):
            return {"evm"}
        if cls._BECH32_ADDRESS.fullmatch(token_address):
            return {"btc"}
        if cls._BASE58_ADDRESS.fullmatch(token_address):
            return {"svm", "tron"} if len(token_address) == 34 and token_address.startswith("T") else {"svm"}
        return set()

    @classmethod
    def _chain_families(cls, chain: dict[str, Any]) -> tuple[str, set[str]]:
        """What a chain's token addresses say about its bridge address type.

        `("known", families)`: the readable tokens agree on at least one address type. `("blank", set())`: no
        token has a recognisable format. `("refuse", set())`: the evidence is missing or contradicts itself, which
        is a chain with only marker addresses, a readable token next to an unreadable one, or tokens that
        disagree. An unreadable token is never skipped, because skipping it can put a chain on the wrong type.
        """
        families: set[str] | None = None
        unreadable = 0
        for token in chain["tokens"]:
            address = str(token["address"])
            if cls._MARKER_ADDRESS.fullmatch(address):
                continue
            found = cls._address_families(address)
            if not found:
                unreadable += 1
                continue
            families = found if families is None else families & found
        if families is None:
            return ("blank", set()) if unreadable else ("refuse", set())
        if unreadable or not families:
            return "refuse", set()
        return "known", families

    @classmethod
    def _resolve_address_types(cls, chains: list[dict[str, Any]], keys: set[str]) -> dict[str, str]:
        """chain id -> the bridge address key (one of the keys `POST /deposit` returned) its deposits go to.

        A chain is resolved only when that is certain: its tokens' address syntax leaves one key, or every key
        it could use but one is already taken by a chain whose syntax was certain. Anything else is left out, and
        the deposit is refused for it rather than guessed, because a wrong address loses money.

        Only the `evm` key serves many chains. Any other key claimed by two chains (Bitcoin and Lightning list
        the same token address, and the list cannot tell them apart) is refused for both.
        """
        certain: dict[str, str] = {}
        candidates: dict[str, set[str]] = {}
        recognised: set[str] = set()
        for chain in chains:
            cid = str(chain["chain_id"])
            state, families = cls._chain_families(chain)
            if state == "refuse":
                continue
            if state == "blank":
                candidates[cid] = set(keys)
                continue
            recognised.add(cid)
            if len(families) == 1:
                # The syntax alone leaves one type. If the bridge did not return that key, there is no address.
                only = next(iter(families))
                if only in keys:
                    certain[cid] = only
                continue
            candidates[cid] = families & keys
        resolved = dict(certain)
        progress = True
        while progress:
            progress = False
            taken = set(resolved.values())
            free = {cid: options - taken for cid, options in candidates.items() if cid not in resolved}
            # A chain whose token addresses have a known syntax is placed first; one with no recognisable
            # syntax only gets what is left, and only if no other open chain could claim it as well.
            for syntax_known in (True, False):
                for cid, options in free.items():
                    if (cid in recognised) != syntax_known or len(options) != 1:
                        continue
                    key = next(iter(options))
                    rivals = [
                        o for other, o in free.items() if (syntax_known and other in recognised) or not syntax_known
                    ]
                    if sum(key in o for o in rivals) == 1:
                        resolved[cid] = key
                        progress = True
                        break
                if progress:
                    break
        claims = Counter(key for key in resolved.values() if key != "evm")
        return {cid: key for cid, key in resolved.items() if key == "evm" or claims[key] == 1}

    async def deposit_options(self, venue: VenueId) -> dict[str, Any]:
        """Every chain and token the venue accepts, as the venue's own list says. Polymarket only."""
        self.require_enabled()
        if venue is not VenueId.POLYMARKET:
            return {"supported": False, "chains": [], "reason": "This venue has a single deposit route."}
        try:
            assets = await self._polymarket_assets()
        except Exception as exc:
            return {
                "supported": False,
                "chains": [],
                "official_url": "https://docs.polymarket.com/trading/bridge/supported-assets",
                "reason": f"Could not read the bridge's supported assets: {type(exc).__name__}",
            }
        # Offer only chains whose deposit address can be placed with certainty (see _resolve_address_types), so
        # the app does not list a chain that would then be refused. Placement here assumes every address type
        # exists; the deposit route still checks against the keys the bridge actually returns.
        chains = self._group_assets(assets)
        placeable = self._resolve_address_types(chains, {"evm", "svm", "btc", "tron"})
        chains = [chain for chain in chains if str(chain["chain_id"]) in placeable]
        default = None
        if chains:
            default = {"chain_id": chains[0]["chain_id"], "token": chains[0]["tokens"][0]["address"]}
        return {
            "supported": bool(chains),
            "default": default,
            "chains": chains,
            "official_url": "https://docs.polymarket.com/trading/bridge/supported-assets",
            "reason": None if chains else "The bridge reports no supported assets.",
        }

    async def _polymarket_deposit(
        self, *, network: str | None, asset: str | None, token: str | None = None
    ) -> DepositInfo:
        wallet = await self._polymarket_wallet()
        if not wallet:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/deposit",
                reason="The Polymarket wallet address is not configured.",
            )
        try:
            chains = self._group_assets(await self._polymarket_assets())
        except Exception as exc:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/supported-assets",
                reason=f"Could not read the bridge's supported assets: {type(exc).__name__}",
            )
        if not chains:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/supported-assets",
                reason="The bridge reports no supported assets.",
            )
        chain: dict[str, Any] | None = chains[0]
        if network:
            wanted = str(network).strip().lower()
            chain = next(
                (c for c in chains if str(c["chain_id"]) == wanted or str(c["chain_name"]).lower() == wanted),
                None,
            )
        picked = None
        if chain is not None:
            tokens = chain["tokens"]
            if token:
                picked = next((t for t in tokens if t["address"].lower() == str(token).strip().lower()), None)
            elif asset:
                picked = next((t for t in tokens if t["symbol"].upper() == str(asset).strip().upper()), None)
            else:
                picked = tokens[0]
        if chain is None or picked is None:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/supported-assets",
                reason="That chain or asset is not on the bridge's supported list.",
            )
        try:
            addresses = await self._bridge_addresses_for(wallet)
        except Exception as exc:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/deposit",
                reason=f"Could not create a bridge address: {type(exc).__name__}",
            )
        kind = self._resolve_address_types(chains, set(addresses)).get(str(chain["chain_id"]))
        bridge_address = addresses.get(kind) if kind else None
        if not kind or not bridge_address:
            return DepositInfo(
                supported=False,
                official_url="https://docs.polymarket.com/trading/bridge/deposit",
                reason=f"Could not tell with certainty which bridge address {chain['chain_name']} deposits "
                "go to, so none is shown. Use polymarket.com to deposit from this chain.",
            )
        warnings = [
            f"Send {picked['symbol']} from {chain['chain_name']} to this {kind.upper()} address only.",
            "Send only the asset and chain shown here; anything else can be lost.",
            "Deposits below the minimum are not processed by the bridge.",
            "The bridge converts what you send and credits it to your Polymarket wallet.",
            "Wrong-token recovery: https://recovery.polymarket.com",
            "Polymarket funds sit in the funder/proxy wallet, which can differ from the "
            "signing wallet. Confirm you are sending to the funder wallet.",
            f"This address credits the Polymarket wallet {wallet}. Check it matches the wallet address on "
            "your polymarket.com profile before sending; if it does not, set POLYMARKET_WALLET_ADDRESS.",
        ]
        return DepositInfo(
            supported=True,
            network=str(chain["chain_name"]),
            asset=str(picked["symbol"]),
            address=bridge_address,
            min_amount=str(picked["min_usd"] or chain["min_usd"] or ""),
            warnings=tuple(warnings),
            official_url="https://docs.polymarket.com/trading/bridge/deposit",
            chain_id=str(chain["chain_id"]),
            address_type=kind,
            token_name=str(picked["name"]),
            token_address=str(picked["address"]),
            wallet=wallet,
        )

    def _limitless_deposit(self) -> DepositInfo:
        # Limitless has no deposit API, so a deposit is a transfer to the wallet the agent trades from. That
        # address comes from the trading key itself (only the public address leaves limitless_signing), not
        # from a separate setting that could name a different wallet.
        address = limitless_signing.wallet_address_or_empty()
        if not address:
            return DepositInfo(
                supported=False,
                official_url="https://docs.limitless.exchange/user-guide/what-is-limitless",
                reason="No Limitless wallet key is configured (LIMITLESS_PRIVATE_KEY), so there is no address to show.",
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

    async def _polymarket_wallet(self) -> str:
        # POLYMARKET_WALLET_ADDRESS is optional: without it the signer's own Deposit Wallet is used.
        return await polymarket_signing.resolve_wallet(self.settings.polymarket_wallet_address)

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

    async def _bridge_asset(self, network: str, asset: str) -> dict[str, str]:
        """Resolve a chain id and token address from the bridge's own list.

        The bridge is the only authority on which chain and token are valid, so
        the operator's choice is looked up there instead of being trusted.
        """
        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        try:
            client = await self.client()
            response = await client.get(f"{base}/supported-assets")
            response.raise_for_status()
            assets = response.json().get("supportedAssets") or []
        except Exception as exc:
            raise FundsError(
                f"could not read the bridge's supported assets: {type(exc).__name__}",
                code="supported_assets_failed",
                status=502,
            ) from exc
        for entry in assets:
            token = entry.get("token") or {}
            chain_id = str(entry.get("chainId") or "")
            chain_name = str(entry.get("chainName") or "")
            # The operator types "base"; the bridge says "Base".
            wanted = network.strip().lower()
            if wanted and wanted not in (chain_id.lower(), chain_name.lower()):
                continue
            if asset and str(token.get("symbol") or "").upper() != asset.upper():
                continue
            return {
                "chain_id": chain_id,
                "token_address": str(token.get("address") or ""),
                "chain_name": chain_name or chain_id,
            }
        raise FundsError(
            f"the bridge does not support {network}/{asset}",
            code="unsupported_destination",
            status=400,
        )

    async def _polymarket_quote(self, amount: Decimal, entry: dict[str, Any]) -> dict[str, Any]:
        """Bridge quote. The from side is Polymarket (Polygon, pUSD)."""

        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        destination = await self._bridge_asset(entry["network"], entry["asset"])
        body = {
            "fromAmountBaseUnit": str(int(amount * Decimal("1000000"))),
            "fromChainId": "137",
            "fromTokenAddress": POLYMARKET_PUSD,
            "recipientAddress": entry["address"],
            "toChainId": destination["chain_id"],
            "toTokenAddress": destination["token_address"],
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
        """Request the destination-scoped bridge address, then send the collateral to it.

        Exactly one transfer is sent. An uncertain outcome is UNKNOWN and is never
        retried; the transaction hash is kept so the operator can look.
        """

        withdrawal_id = record["id"]
        venue = VenueId(record["venue"])
        # The limit is checked again here: the money leaves now, not when the
        # withdrawal was created, and the hold window may have crossed midnight.
        # Only what has already moved counts, so this withdrawal does not block
        # itself.
        daily_limit = self.settings.funds_withdrawal_daily_limit_usd
        remaining = daily_limit - self.store.withdrawn_today(venue, sent_only=True)
        if Decimal(str(record["amount_usd"])) > remaining:
            reason = f"the daily withdrawal limit is {daily_limit}; only {remaining} is left today"
            self.store.transition_withdrawal(withdrawal_id, "FAILED", error=reason)
            self.store.audit("funds", "funds.withdraw.failed", {"id": withdrawal_id, "error": reason})
            return {"id": withdrawal_id, "status": "FAILED", "error": reason}
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

        # The bridge address is now known. Send the collateral exactly once.
        try:
            receipt = await send_erc20_transfer(
                rpc_url=self.settings.funds_rpc_url,
                to_address=bridge_address,
                amount_base_units=to_base_units(Decimal(str(record["amount_usd"]))),
                token_address=POLYMARKET_PUSD,
                chain_id=POLYGON_CHAIN_ID,
            )
        except SignerError as exc:
            # The transfer was never accepted: nothing moved, so it is safe to retry
            # by hand once the operator has read the reason.
            self.store.transition_withdrawal(
                withdrawal_id,
                "FAILED",
                bridge_address=bridge_address,
                error=str(exc),
            )
            self.store.audit("funds", "funds.withdraw.failed", {"id": withdrawal_id, "error": str(exc)})
            return {"id": withdrawal_id, "status": "FAILED", "bridge_address": bridge_address, "error": str(exc)}
        except Exception as exc:
            # The RPC may have accepted it. UNKNOWN keeps the address for the operator.
            self.store.transition_withdrawal(
                withdrawal_id,
                "UNKNOWN",
                bridge_address=bridge_address,
                error=f"transfer outcome uncertain: {type(exc).__name__}",
            )
            self.store.audit("funds", "funds.withdraw.unknown", {"id": withdrawal_id})
            return {"id": withdrawal_id, "status": "UNKNOWN", "bridge_address": bridge_address, "error": str(exc)}

        self.store.transition_withdrawal(
            withdrawal_id,
            "SUBMITTED",
            bridge_address=bridge_address,
            tx_hash=receipt.tx_hash,
            error=None,
        )
        self.store.audit(
            "funds",
            "funds.withdraw.submitted",
            {"id": withdrawal_id, "tx_hash": receipt.tx_hash, "bridge_address": bridge_address},
        )
        return {
            "id": withdrawal_id,
            "status": "SUBMITTED",
            "bridge_address": bridge_address,
            "tx_hash": receipt.tx_hash,
        }

    async def _polymarket_withdraw_address(self, record: dict[str, Any]) -> str:
        """Ask the bridge for the address tied to this exact destination.

        The chain and the token come from the bridge's own supported list, and
        the recipient from the withdrawal record, so the address is requested for
        exactly one destination. The answer is checked before it is used.
        """

        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        entry = self.store.allowlist_get(record["address_id"]) or {}
        destination = await self._bridge_asset(entry.get("network") or "", entry.get("asset") or "")
        body = {
            "address": await self._polymarket_wallet(),
            "toChainId": destination["chain_id"],
            "toTokenAddress": destination["token_address"],
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
        # The bridge's reply carries only the address, so the destination is
        # confirmed later, when its status reports what it is actually routing.
        return str(bridge_address)

    def list_withdrawals(self, *, venue: VenueId | None = None) -> dict[str, Any]:
        return {"withdrawals": self.store.list_withdrawals(venue=venue)}

    async def polymarket_history(self, wallet: str) -> list[dict[str, Any]]:
        """Polymarket's bridge reports the wallet's deposits and withdrawals."""

        return await self._polymarket_funds_history(wallet)

    async def refresh_submitted(self) -> list[dict[str, Any]]:
        """Poll the bridge so a submitted withdrawal can reach CONFIRMED.

        The bridge tracks a withdrawal by its address, so the bridge address
        recorded at submission time is what is asked about.
        """
        self.require_enabled()
        out: list[dict[str, Any]] = []
        for record in self.store.list_withdrawals(limit=200):
            if record["status"] != "SUBMITTED":
                continue
            venue = VenueId(record["venue"])
            if venue is not VenueId.POLYMARKET:
                continue
            bridge_address = record.get("bridge_address")
            if not bridge_address:
                continue
            try:
                status, detail = await self._bridge_status(bridge_address, record)
            except Exception as exc:
                # A failed poll leaves the withdrawal SUBMITTED; it is not a failure.
                logger.warning("withdrawal status poll failed", withdrawal_id=record["id"], error=str(exc)[:120])
                continue
            if status == "MISMATCH":
                # The bridge is routing this somewhere else than the operator chose.
                self.store.transition_withdrawal(record["id"], "FAILED", error=detail)
                self.store.audit("funds", "funds.withdraw.mismatch", {"id": record["id"], "error": detail})
                out.append({"id": record["id"], "status": "FAILED", "error": detail})
                continue
            if status == "COMPLETED":
                self.store.transition_withdrawal(record["id"], "CONFIRMED", error=None)
                self.store.audit("funds", "funds.withdraw.confirmed", {"id": record["id"]})
                out.append({"id": record["id"], "status": "CONFIRMED"})
            else:
                out.append({"id": record["id"], "status": "SUBMITTED"})
        return out

    async def _bridge_status(self, bridge_address: str, record: dict[str, Any]) -> tuple[str, str]:
        """The bridge's state for this withdrawal, and whether it is ours.

        The bridge's documented status values are DEPOSIT_DETECTED, PROCESSING,
        ORIGIN_TX_CONFIRMED, SUBMITTED, COMPLETED and FAILED. Its rows also carry
        the destination, which is the only place the routing can be confirmed.
        """

        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        client = await self.client()
        response = await client.get(f"{base}/status/{bridge_address}")
        response.raise_for_status()
        body = response.json()
        rows = body.get("transactions") or []
        entry = self.store.allowlist_get(record["address_id"]) or {}
        destination = await self._bridge_asset(entry.get("network") or "", entry.get("asset") or "")
        wanted_amount = int(Decimal(str(record["amount_usd"])) * Decimal("1000000"))
        for row in rows:
            if not isinstance(row, dict):
                continue
            # The bridge may hold other transactions at this address, so only a
            # row for our destination and amount counts as ours.
            to_chain = str(row.get("toChainId") or "")
            to_token = str(row.get("toTokenAddress") or "")
            amount = int(str(row.get("fromAmountBaseUnit") or 0))
            if to_chain != destination["chain_id"] or to_token.lower() != destination["token_address"].lower():
                return "MISMATCH", (f"the bridge is routing this to chain {to_chain}, not {destination['chain_id']}")
            if amount != wanted_amount:
                return "MISMATCH", (
                    f"the bridge is routing {amount} base units, not the {wanted_amount} that was requested"
                )
            status = str(row.get("status") or "").upper()
            if status == "COMPLETED":
                return "COMPLETED", ""
            if status == "FAILED":
                return "FAILED", "the bridge reported the withdrawal as failed"
        return "PENDING", ""

    async def _polymarket_funds_history(self, wallet: str) -> list[dict[str, Any]]:
        """Deposits the bridge saw at the wallet's bridge addresses (every address type it returned).

        Amounts are in the token's own units, read with the decimals the bridge lists for that token; a
        token the bridge no longer lists is shown in base units.
        """

        base = self.settings.funds_polymarket_bridge_url.rstrip("/")
        rows: list[Any] = []
        known: dict[tuple[str, str], tuple[str, int]] = {}
        try:
            client = await self.client()
            addresses = await self._bridge_addresses_for(wallet)
            try:
                for entry in await self._polymarket_assets():
                    token = entry.get("token") or {}
                    key = (str(entry.get("chainId")), str(token.get("address") or "").lower())
                    if token.get("decimals") is not None:
                        known[key] = (str(token.get("symbol") or ""), int(token["decimals"]))
            except Exception:
                known = {}
            for bridge_address in dict.fromkeys(addresses.values()):
                response = await client.get(f"{base}/status/{bridge_address}")
                response.raise_for_status()
                rows.extend(response.json().get("transactions") or [])
        except Exception as exc:
            logger.warning("bridge history failed", error=str(exc)[:120])
            if not rows:
                return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            token_address = str(row.get("fromTokenAddress") or "")
            raw = Decimal(str(row.get("fromAmountBaseUnit") or 0))
            found = known.get((str(row.get("fromChainId")), token_address.lower()))
            amount = raw / (Decimal(10) ** found[1]) if found else raw
            asset = found[0] if found else "base units"
            out.append(
                {
                    # These rows come from the wallet's deposit addresses, so they are deposits.
                    "kind": "deposit",
                    "id": str(row.get("txHash") or row.get("createdTimeMs") or ""),
                    "status": str(row.get("status") or ""),
                    "type": "crypto",
                    # A dollar value is not in the status answer; the amount is shown in the token's own units.
                    "amount_usd": "",
                    "amount": str(amount),
                    "asset": asset,
                    "fee_usd": "0",
                    "at": row.get("createdTimeMs"),
                }
            )
        out.sort(key=lambda item: item.get("at") or 0, reverse=True)
        return out


# pUSD on Polygon, the Polymarket collateral the bridge unwraps from.
POLYMARKET_PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


__all__ = ["DepositInfo", "FundsError", "FundsService"]
