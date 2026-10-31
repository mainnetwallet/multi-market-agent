"""Environment configuration and non-negotiable hard ceilings.

Every order is real money. A venue trades only when its credentials pass the
authenticated doctor checks; the risk caps below can never be raised by an API,
chat message, tool call or UI control.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
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
    bankroll_polymarket: Decimal | None = None
    bankroll_kalshi: Decimal | None = None
    bankroll_limitless: Decimal | None = None
    max_trade_usd: Decimal = HARD_MAX_TRADE_USD
    max_event_pct: Decimal = HARD_MAX_EVENT_PCT
    max_total_exposure_pct: Decimal = HARD_MAX_TOTAL_EXPOSURE_PCT
    max_daily_loss_pct: Decimal = HARD_MAX_DAILY_LOSS_PCT
    max_open_positions: int = HARD_MAX_OPEN_POSITIONS
    advisory_mode: bool = False

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
    # Context window of the configured model and the share kept free for its reply. Every request
    # is assembled to fit (window - reserve - tool schemas); older chat folds into a summary.
    llm_context_tokens: int = Field(default=32000, ge=2000, le=2_000_000)
    llm_reserve_tokens: int = Field(default=4000, ge=256, le=200_000)
    research_min_sources: int = 2
    research_max_age_minutes: int = 90
    llm_adjustment_max_pp: Decimal = Decimal("3")

    admin_username: str = "operator"
    admin_password: str = ""
    session_secret: str = ""
    session_ttl_hours: int = 720
    allowed_origins_raw: str = Field(default="http://127.0.0.1:8080,http://localhost:8080", alias="ALLOWED_ORIGINS")

    telegram_bot_token: str = ""
    telegram_allowed_user_id: int = 0
    telegram_heartbeat_seconds: int = 21600

    scan_interval_seconds: int = 60
    markets_per_scan: int = 40
    book_feed_markets: int = 5
    book_feed_interval_seconds: int = 5
    book_fresh_seconds: int = 30
    # Account sync: the venue, not the local database, is the truth for positions and resting orders.
    account_sync_enabled: bool = True
    account_sync_interval_seconds: float = 3.0
    account_sync_other_interval_seconds: float = 15.0
    proposal_ttl_seconds: int = 300
    auto_propose: bool = True
    proposals_per_cycle: int = 2

    # Full-control build. The $2 per-trade cap is no longer a code constant: it is a
    # server-side setting (stored, default 2) the operator can raise from the app
    # with a step-up token. This field is only the *default* for a fresh install.
    default_max_trade_usd: Decimal = HARD_MAX_TRADE_USD

    # Funds module: off unless explicitly enabled. Separate signer, separate key.
    funds_enabled: bool = False
    funds_signer_private_key: str = ""
    funds_signer_address: str = ""
    funds_rpc_url: str = "https://polygon-rpc.com"
    funds_relayer_api_key: str = ""
    funds_relayer_api_key_address: str = ""
    funds_withdrawal_daily_limit_usd: Decimal = Decimal("25")
    funds_withdrawal_hold_seconds: int = 5
    funds_allowlist_cooling_hours: int = 24
    # False: a withdrawal may name its receiver address, chain and token directly (the hold window and
    # daily limit still apply; no password is asked). True: only addresses already on the allowlist can
    # receive.
    funds_require_allowlist: bool = False
    funds_polymarket_bridge_url: str = "https://bridge.polymarket.com"
    funds_polymarket_builder_code: str = ""

    @field_validator("database_url")
    @classmethod
    def _postgres_driver(cls, v: str) -> str:
        # Render and most hosts hand out postgres:// or postgresql://, which SQLAlchemy
        # maps to psycopg2. The image installs psycopg 3, so name that driver.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v.removeprefix(prefix)
        return v

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

    @field_validator(
        "bankroll_usd",
        "bankroll_polymarket",
        "bankroll_kalshi",
        "bankroll_limitless",
        mode="before",
    )
    @classmethod
    def _blank_bankroll(cls, v: object, info: ValidationInfo) -> object:
        if isinstance(v, str) and not v.strip():
            # Hosting dashboards often deliver blank optional values as "".
            return Decimal("100") if info.field_name == "bankroll_usd" else None
        return v

    @field_validator(
        "bankroll_usd",
        "bankroll_polymarket",
        "bankroll_kalshi",
        "bankroll_limitless",
    )
    @classmethod
    def _bankroll_positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("bankroll values must be positive")
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

    def limit_bankroll(self, venue: VenueId) -> Decimal:
        """Per-venue sizing bankroll, falling back to the global one."""

        return {
            VenueId.POLYMARKET: self.bankroll_polymarket,
            VenueId.KALSHI: self.bankroll_kalshi,
            VenueId.LIMITLESS: self.bankroll_limitless,
        }.get(venue) or self.bankroll_usd

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
