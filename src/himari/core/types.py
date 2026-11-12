"""Foundational types used across the system."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class AssetClass(str, enum.Enum):
    EQUITY = "equity"
    GOV_BOND = "gov_bond"
    GOLD = "gold"
    COMMODITY = "commodity"
    CURRENCY = "currency"


class Currency(str, enum.Enum):
    CAD = "CAD"
    USD = "USD"


class Exchange(str, enum.Enum):
    TSX = "TSX"
    NASDAQ = "NASDAQ"


class Instrument(BaseModel):
    symbol: str
    exchange: Exchange
    currency: Currency
    asset_class: AssetClass = Field(alias="class")

    model_config = {"populate_by_name": True}


class QualityFlag(str, enum.Enum):
    OK = "OK"
    STALE = "STALE"
    INVALID = "INVALID"
    ANOMALY = "ANOMALY"
    ACTION_MISMATCH = "ACTION_MISMATCH"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    FX_STALE = "FX_STALE"
    FX_FILLED = "FX_FILLED"  # latest earlier FX rate used, within the allowed age (§5.5)
    FX_BRIDGED = "FX_BRIDGED"  # inside an FX gap, or the session booking the bridged return (v1.2)
    FX_OUTAGE = "FX_OUTAGE"  # FX gap longer than the bridge cap: fault until confirmed (v1.2)
    RATE_STALE = "RATE_STALE"
    ZERO_VOLUME = "ZERO_VOLUME"


class SafetyState(str, enum.Enum):
    NORMAL = "NORMAL"
    PRE_EVENT = "PRE_EVENT"
    DATA_FAULT = "DATA_FAULT"
    HALVED = "HALVED"
    SUSPENDED = "SUSPENDED"
    VENUE_FAULT = "VENUE_FAULT"
    KILL = "KILL"


class DrawdownState(str, enum.Enum):
    NORMAL = "NORMAL"
    HALVED = "HALVED"
    SUSPENDED = "SUSPENDED"


class Session(BaseModel):
    date: date
    exchange: str


class Bar(BaseModel):
    symbol: str
    session_date: date
    open: float
    high: float
    low: float
    close: float  # unadjusted
    volume: int
    source: str
    adj_close: float | None = None  # vendor split+distribution adjusted close, for Q5
    version: int = 1
    observed_at_utc: datetime | None = None
    released_at_utc: datetime | None = None
    quality_flags: list[QualityFlag] = Field(default_factory=list)


class CorporateAction(BaseModel):
    symbol: str
    ex_date: date
    pay_date: date | None = None
    action_type: Literal["cash", "split"]
    amount: Decimal  # cash per post-split share, or split ratio (2.0 = two-for-one)
    currency: Currency | None = None
    source: str = ""
    version: int = 1
    observed_at_utc: datetime | None = None
    released_at_utc: datetime | None = None


class SeriesObservation(BaseModel):
    """One value of a daily scalar series: FXUSDCAD (CAD per USD) or AVG.INTWO (% per year)."""

    series: str
    obs_date: date
    value: float
    source: str
    version: int = 1
    observed_at_utc: datetime | None = None
    released_at_utc: datetime | None = None


class Order(BaseModel):
    decision_id: str
    symbol: str
    side: str  # "buy" or "sell"
    shares: int
    limit_price: Decimal | None = None
    order_type: str = "limit"
    idempotency_key: str = ""


class OrderAck(BaseModel):
    order_id: str
    decision_id: str
    symbol: str
    status: str  # "submitted", "rejected", "duplicate"


class Fill(BaseModel):
    order_id: str
    symbol: str
    side: str
    shares: int
    price: Decimal
    commission: Decimal = Decimal("0")
    fill_time_utc: datetime | None = None


class Position(BaseModel):
    symbol: str
    shares: int
    avg_cost: Decimal = Decimal("0")


class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    bid_size: int = 0
    ask_size: int = 0
    timestamp_utc: datetime | None = None


class GateLog(BaseModel):
    gate_name: str
    weights_in: dict[str, float]
    weights_out: dict[str, float]
    bound: bool = False
    scale_applied: float = 1.0
    details: dict[str, Any] = Field(default_factory=dict)
