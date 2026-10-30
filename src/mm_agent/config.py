"""Environment configuration and non-negotiable hard ceilings.

Only an operator editing the environment and restarting can switch a venue to
LIVE. No API, chat message, tool call or UI control can do so.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain import VenueId

# Secrets such as POLYMARKET_PRIVATE_KEY are deliberately not Settings fields;
# the isolated signing modules read them from the process environment. Load a
# local .env into the environment once so those modules work in development.
load_dotenv(override=False)

# These are code-level ceilings. Configuration may lower them, never raise them.
HARD_MAX_TRADE_USD = Decimal("2")
HARD_MAX_EVENT_PCT = Decimal("4")
HARD_MAX_TOTAL_EXPOSURE_PCT = Decimal("15")
HARD_MAX_DAILY_LOSS_PCT = Decimal("5")
HARD_MAX_OPEN_POSITIONS = 8

HARD_MIN_EDGE = Decimal("0.03")
HARD_MAX_SPREAD = Decimal("0.06")
HARD_MIN_DEPTH_MULTIPLE = Decimal("3")
HARD_PROPOSAL_TTL_SECONDS = 300
HARD_BOOK_FRESH_SECONDS = 30


def _clamp(value: Decimal, ceiling: Decimal, floor: Decimal = Decimal("0")) -> Decimal:
    return max(floor, min(value, ceiling))


class Limits(BaseModel):
    """Frozen view of the effective limits shown to the operator."""

    model_config = ConfigDict(frozen=True)

    bankroll_usd: Decimal
    max_trade_usd: Decimal
    max_event_pct: Decimal
    max_total_exposure_pct: Decimal
    max_daily_loss_pct: Decimal
    max_open_positions: int
    min_edge: Decimal
    max_spread: Decimal
    min_depth_multiple: Decimal
    proposal_ttl_seconds: int
    book_fresh_seconds: int
    advisory_mode: bool


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    app_env: str = "dev"
    database_url: str = "sqlite:///./data/mm_agent.db"
    host: str = "127.0.0.1"
    port: int = 8080

    enabled_venues_raw: str = Field(default="polymarket,kalshi,limitless", alias="ENABLED_VENUES")
    active_venue: VenueId = VenueId.POLYMARKET
    parallel_scan: bool = False

    bankroll_usd: Decimal = Decimal("100")
    max_trade_usd: Decimal = HARD_MAX_TRADE_USD
    max_event_pct: Decimal = HARD_MAX_EVENT_PCT
    max_total_exposure_pct: Decimal = HARD_MAX_TOTAL_EXPOSURE_PCT
    max_daily_loss_pct: Decimal = HARD_MAX_DAILY_LOSS_PCT
    max_open_positions: int = HARD_MAX_OPEN_POSITIONS
    advisory_mode: bool = False

    live_polymarket: bool = False
    live_kalshi: bool = False
    live_limitless: bool = False
    # Test-only venue. It has no real money and is never enabled by default.
    live_fake: bool = False
    chat_autopilot: bool = False

    polymarket_wallet_address: str = ""
    polymarket_signature_type: int = 3
    polymarket_allow_real_test: bool = False
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_demo_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    kalshi_use_demo_for_doctor: bool = True
    lmts_token_id: str = ""
    lmts_owner_id: int = 0
    limitless_api_url: str = "https://api.limitless.exchange"
    base_rpc_url: str = "https://mainnet.base.org"
    limitless_usdc_address: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = 60
    research_min_sources: int = 2
    research_max_age_minutes: int = 90
    llm_adjustment_max_pp: Decimal = Decimal("3")

    admin_username: str = "operator"
    admin_password: str = ""
    session_secret: str = ""
    session_ttl_hours: int = 72
    allowed_origins_raw: str = Field(default="http://127.0.0.1:8080,http://localhost:8080", alias="ALLOWED_ORIGINS")

    telegram_bot_token: str = ""
    telegram_allowed_user_id: int = 0
    telegram_heartbeat_seconds: int = 21600

    scan_interval_seconds: int = 60
    markets_per_scan: int = 40
    book_fresh_seconds: int = 30
    proposal_ttl_seconds: int = 300
    auto_propose: bool = True
    proposals_per_cycle: int = 2

    @field_validator("max_trade_usd")
    @classmethod
    def _cap_trade(cls, v: Decimal) -> Decimal:
        return _clamp(v, HARD_MAX_TRADE_USD, Decimal("0.10"))

    @field_validator("max_event_pct")
    @classmethod
    def _cap_event(cls, v: Decimal) -> Decimal:
        return _clamp(v, HARD_MAX_EVENT_PCT, Decimal("0.1"))

    @field_validator("max_total_exposure_pct")
    @classmethod
    def _cap_exposure(cls, v: Decimal) -> Decimal:
        return _clamp(v, HARD_MAX_TOTAL_EXPOSURE_PCT, Decimal("0.1"))

    @field_validator("max_daily_loss_pct")
    @classmethod
    def _cap_loss(cls, v: Decimal) -> Decimal:
        return _clamp(v, HARD_MAX_DAILY_LOSS_PCT, Decimal("0.1"))

    @field_validator("max_open_positions")
    @classmethod
    def _cap_positions(cls, v: int) -> int:
        return max(1, min(v, HARD_MAX_OPEN_POSITIONS))

    @field_validator("proposal_ttl_seconds")
    @classmethod
    def _cap_ttl(cls, v: int) -> int:
        return max(30, min(v, HARD_PROPOSAL_TTL_SECONDS))

    @field_validator("book_fresh_seconds")
    @classmethod
    def _cap_book_age(cls, v: int) -> int:
        return max(5, min(v, HARD_BOOK_FRESH_SECONDS))

    @field_validator("llm_adjustment_max_pp")
    @classmethod
    def _cap_adjustment(cls, v: Decimal) -> Decimal:
        return _clamp(v, Decimal("3"), Decimal("0"))

    @field_validator("bankroll_usd")
    @classmethod
    def _bankroll_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("BANKROLL_USD must be positive")
        return v

    @property
    def enabled_venues(self) -> tuple[VenueId, ...]:
        out: list[VenueId] = []
        for raw in self.enabled_venues_raw.replace(";", ",").split(","):
            name = raw.strip().lower()
            if not name:
                continue
            venue = VenueId(name)
            if venue not in out:
                out.append(venue)
        return tuple(out)

    @property
    def allowed_origins(self) -> list[str]:
        return [x.strip() for x in self.allowed_origins_raw.split(",") if x.strip()]

    @property
    def live_venues(self) -> tuple[VenueId, ...]:
        return tuple(v for v in self.enabled_venues if self.live_for(v))

    def live_for(self, venue: VenueId) -> bool:
        return {
            VenueId.POLYMARKET: self.live_polymarket,
            VenueId.KALSHI: self.live_kalshi,
            VenueId.LIMITLESS: self.live_limitless,
            VenueId.FAKE: self.live_fake,
        }[venue]

    def limit_bankroll(self, venue: VenueId) -> Decimal:
        # Per-venue bankrolls are represented by the global bankroll in v1;
        # venue-specific capital can be lowered with LIMITS but never raised
        # beyond the hard global ceiling.
        del venue
        return self.bankroll_usd

    @property
    def limits(self) -> Limits:
        return Limits(
            bankroll_usd=self.bankroll_usd,
            max_trade_usd=self.max_trade_usd,
            max_event_pct=self.max_event_pct,
            max_total_exposure_pct=self.max_total_exposure_pct,
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_open_positions=self.max_open_positions,
            min_edge=HARD_MIN_EDGE,
            max_spread=HARD_MAX_SPREAD,
            min_depth_multiple=HARD_MIN_DEPTH_MULTIPLE,
            proposal_ttl_seconds=self.proposal_ttl_seconds,
            book_fresh_seconds=self.book_fresh_seconds,
            advisory_mode=self.advisory_mode,
        )

    def effective_min_edge(self) -> Decimal:
        return HARD_MIN_EDGE * (Decimal("2") if self.advisory_mode else Decimal("1"))

    def credentials_present(self, venue: VenueId) -> bool:
        if venue is VenueId.POLYMARKET:
            return bool(os.environ.get("POLYMARKET_PRIVATE_KEY"))
        if venue is VenueId.KALSHI:
            return bool(self.kalshi_api_key_id and self.kalshi_private_key_path)
        if venue is VenueId.LIMITLESS:
            return bool(
                os.environ.get("LMTS_TOKEN_ID")
                and os.environ.get("LMTS_TOKEN_SECRET")
                and os.environ.get("LIMITLESS_PRIVATE_KEY")
            )
        return True

    def ensure_data_dir(self) -> None:
        if self.database_url.startswith("sqlite:///"):
            path = Path(self.database_url.removeprefix("sqlite:///"))
            if path.parent and str(path.parent) not in {"", "."}:
                path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def mode_for(settings: Settings, venue: VenueId) -> Literal["PAPER", "LIVE"]:
    return "LIVE" if settings.live_for(venue) else "PAPER"
