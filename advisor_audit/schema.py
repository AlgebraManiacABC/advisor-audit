"""Normalized statement schema.

This is the only statement data the app keeps, and only for the session.
It follows the brief, plus four optional fields that some statements
include and that the calculations can use:

- ``firm``: the advisory firm's name. Used to spot proprietary funds.
- ``balance_history``: periodic account values. Needed for volatility and
  max drawdown, since a single start/end balance can't show either.
- ``holding.load_pct``: a sales load stated on the holding.
- ``market_change``: the statement's own "change in market value" line.
  When present, ``reconcile`` checks the balance equation against it.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

AssetClass = Literal["us_stock", "intl_stock", "bond", "cash", "other"]
TxnType = Literal["deposit", "withdrawal", "buy", "sell", "dividend", "fee"]
FeeKind = Literal["advisory", "load", "other"]


class Period(BaseModel):
    start: date
    end: date


class Holding(BaseModel):
    ticker: str = ""
    name: str = ""
    quantity: float = 0.0
    value: float = 0.0
    asset_class: AssetClass = "other"
    expense_ratio: Optional[float] = Field(
        default=None, description="Annual expense ratio as a decimal, e.g. 0.0125 for 1.25%."
    )
    load_pct: Optional[float] = None


class Transaction(BaseModel):
    date: date
    type: TxnType
    ticker: Optional[str] = None
    amount: float = Field(description="Positive dollar amount.")

    @field_validator("amount")
    @classmethod
    def _abs(cls, v: float) -> float:
        return abs(v)


class Fee(BaseModel):
    date: date
    kind: FeeKind = "other"
    amount: float

    @field_validator("amount")
    @classmethod
    def _abs(cls, v: float) -> float:
        return abs(v)


class BalancePoint(BaseModel):
    date: date
    value: float


class Statement(BaseModel):
    period: Period
    starting_balance: float
    ending_balance: float
    holdings: list[Holding] = []
    transactions: list[Transaction] = []
    fees: list[Fee] = []
    firm: Optional[str] = None
    market_change: Optional[float] = None
    balance_history: list[BalancePoint] = []


RiskProfile = Literal["conservative", "balanced", "growth"]
