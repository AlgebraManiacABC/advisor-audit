"""Bundled ETF price history, so the benchmark needs no live market data API.

The CSV has a ``date`` column plus one total-return price column per ETF
(dividends reinvested). Prices between rows are linearly interpolated.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from datetime import date
from functools import lru_cache
from pathlib import Path

from .config import settings


class PriceSeries:
    def __init__(self, rows: list[tuple[date, dict[str, float]]]):
        rows.sort(key=lambda r: r[0])
        self.dates = [r[0] for r in rows]
        self.prices = [r[1] for r in rows]

    @property
    def first(self) -> date:
        return self.dates[0]

    @property
    def last(self) -> date:
        return self.dates[-1]

    def covers(self, start: date, end: date) -> bool:
        return self.first <= start and end <= self.last

    def price(self, ticker: str, d: date) -> float:
        if not self.first <= d <= self.last:
            raise ValueError(f"No {ticker} price for {d}; bundled data covers {self.first} to {self.last}.")
        i = bisect_right(self.dates, d)
        if i == len(self.dates) or self.dates[i - 1] == d:
            return self.prices[i - 1][ticker]
        d0, d1 = self.dates[i - 1], self.dates[i]
        p0, p1 = self.prices[i - 1][ticker], self.prices[i][ticker]
        return p0 + (p1 - p0) * (d - d0).days / (d1 - d0).days

    def dates_between(self, start: date, end: date) -> list[date]:
        return [d for d in self.dates if start < d < end]


@lru_cache(maxsize=4)
def load_prices(path: str | None = None) -> PriceSeries:
    path = path or settings.prices_csv
    rows = []
    with Path(path).open(newline="") as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r.pop("date"))
            rows.append((d, {k: float(v) for k, v in r.items()}))
    return PriceSeries(rows)
