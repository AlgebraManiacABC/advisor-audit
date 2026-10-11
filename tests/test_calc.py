"""Calculations checked against values worked out by hand (shown in comments)."""

from datetime import date

import pytest

from advisor_audit import calc
from advisor_audit.prices import PriceSeries
from advisor_audit.schema import Statement


def stmt(**kw) -> Statement:
    base = {
        "period": {"start": "2025-01-01", "end": "2025-12-31"},
        "starting_balance": 100_000, "ending_balance": 110_000,
        "transactions": [
            {"date": "2025-07-02", "type": "deposit", "amount": 10_000},
            {"date": "2025-09-01", "type": "withdrawal", "amount": 5_000},
        ],
        "fees": [{"date": "2025-12-31", "kind": "advisory", "amount": 1_000}],
        "market_change": 6_000,
        "holdings": [
            {"ticker": "AAA", "name": "A Fund", "value": 60_000, "asset_class": "us_stock", "expense_ratio": 0.01},
            {"ticker": "BBB", "name": "B Fund", "value": 50_000, "asset_class": "bond", "expense_ratio": 0.001},
        ],
    }
    base.update(kw)
    return Statement.model_validate(base)


# ---------------------------------------------------------------- reconcile

def test_reconcile_balances():
    # 100,000 + 10,000 - 5,000 - 1,000 + 6,000 = 110,000
    r = calc.reconcile(stmt())
    assert r["ok"], r["discrepancies"]
    assert r["figures"]["implied_market_change"] == 6_000


def test_reconcile_flags_bad_ending_balance():
    s = stmt(ending_balance=110_500, holdings=[])
    r = calc.reconcile(s)
    assert not r["ok"]
    assert "off by 500.00" in r["discrepancies"][0]


def test_reconcile_flags_holdings_mismatch_and_dates():
    s = stmt(market_change=None,
             fees=[{"date": "2026-02-01", "kind": "advisory", "amount": 1_000}],
             holdings=[{"ticker": "A", "value": 90_000}])
    r = calc.reconcile(s)
    text = " ".join(r["discrepancies"])
    assert "Holdings add up to 90,000.00" in text
    assert "outside the statement period" in text


def test_reconcile_flags_implausible_gain():
    s = stmt(ending_balance=400_000, market_change=None, holdings=[])
    assert "implausible" in " ".join(calc.reconcile(s)["discrepancies"])


def test_tolerance_allows_cents():
    assert calc.reconcile(stmt(ending_balance=110_000.40, market_change=None, holdings=[])).get("ok")


# ---------------------------------------------------------------- returns

def test_modified_dietz():
    # 364-day period; deposit with 182 days left has weight 0.5.
    # (115,000 - 100,000 - 10,000) / (100,000 + 0.5 * 10,000) = 5,000 / 105,000
    r = calc.modified_dietz(100_000, 115_000, [(date(2025, 7, 2), 10_000)], date(2025, 1, 1), date(2025, 12, 31))
    assert r == pytest.approx(5_000 / 105_000)


def test_compute_performance_fees_and_returns():
    s = stmt()
    p = calc.compute_performance(s)
    assert p["fees_paid"] == {"advisory": 1_000}
    assert p["fees_paid_total"] == 1_000
    # Weighted ER = (60k * 1% + 50k * 0.1%) / 110k = 650 / 110,000
    assert p["weighted_expense_ratio"] == pytest.approx(650 / 110_000, abs=1e-5)
    # Average balance (100k + 110k) / 2 = 105k; fund expenses = 105k * ER * 364/365.25 years
    assert p["estimated_fund_expenses"] == pytest.approx(105_000 * 650 / 110_000 * 364 / 365.25, abs=0.01)
    # Weights: deposit 182/364 = 0.5, withdrawal 121/364.
    denom = 100_000 + 10_000 * 0.5 - 5_000 * 121 / 364
    assert p["return_after_fees"] == pytest.approx((110_000 - 100_000 - 5_000) / denom, abs=1e-5)
    assert p["return_before_fees"] == pytest.approx((111_000 - 100_000 - 5_000) / denom, abs=1e-5)
    assert p["volatility"] is None and p["max_drawdown"] is None


def test_series_stats_volatility_and_drawdown():
    # Quarterly points 91 days apart, returns +10%, -10%, +10%.
    d = [date(2025, 1, 1), date(2025, 4, 2), date(2025, 7, 2), date(2025, 10, 1)]
    pts = list(zip(d, [100.0, 110.0, 99.0, 108.9]))
    st = calc.series_stats(pts, [])
    assert st["twr"] == pytest.approx(1.1 * 0.9 * 1.1 - 1)  # 0.089
    assert st["max_drawdown"] == pytest.approx(-0.10)
    # stdev(0.1, -0.1, 0.1) = 0.11547; x sqrt(365.25 / 91)
    assert st["volatility"] == pytest.approx(0.115470 * (365.25 / 91) ** 0.5, rel=1e-4)


# ---------------------------------------------------------------- benchmark

@pytest.fixture
def prices():
    # Stock rises 10% linearly over the year; bonds flat. No month-end rows in between.
    return PriceSeries([(date(2025, 1, 1), {"VTI": 100.0, "BND": 100.0}),
                        (date(2025, 12, 31), {"VTI": 110.0, "BND": 100.0})])


def test_benchmark_no_flows(prices):
    s = stmt(transactions=[], market_change=None)
    b = calc.benchmark(s, "balanced", prices)
    # 60k * 1.10 + 40k = 106,000
    assert b["ending_value"] == pytest.approx(106_000)
    assert b["return"] == pytest.approx(0.06)


def test_benchmark_applies_same_flows(prices):
    # Deposit 10k on 2025-07-02 (day 182 of 364, stock at 105).
    # Before: 600 sh * 105 + 400 sh * 100 = 103,000; +10k = 113,000, rebalanced 60/40:
    # 67,800 / 105 = 645.714 sh stock, 45,200 bonds. End: 645.714 * 110 + 45,200 = 116,228.57
    s = stmt(transactions=[{"date": "2025-07-02", "type": "deposit", "amount": 10_000}], market_change=None)
    b = calc.benchmark(s, "balanced", prices)
    assert b["ending_value"] == pytest.approx(116_228.57, abs=0.01)


def test_benchmark_mixes(prices):
    s = stmt(transactions=[], market_change=None)
    assert calc.benchmark(s, "conservative", prices)["ending_value"] == pytest.approx(103_000)
    assert calc.benchmark(s, "growth", prices)["ending_value"] == pytest.approx(109_000)


def test_benchmark_out_of_range(prices):
    s = stmt(period={"start": "2020-01-01", "end": "2020-12-31"}, transactions=[], fees=[], market_change=None)
    assert calc.benchmark(s, "balanced", prices)["available"] is False


def test_compare_notes_short_period_and_small_gap():
    perf = {"period_years": 1.0, "return_after_fees": 0.051, "ending_balance": 1, "volatility": None, "max_drawdown": None}
    bench = {"available": True, "return": 0.055, "ending_value": 1, "volatility": None, "max_drawdown": None}
    c = calc.compare(perf, bench)
    assert c["meaningful"] is False
    assert len(c["notes"]) == 2


# ---------------------------------------------------------------- projections

def test_project_fees_by_hand():
    # 1% advisory on a flat 100k over the year; no fund expenses; 5% growth.
    # Year 1: 105,000 -> fee 1,050 -> 103,950. Year 2: 109,147.50 -> fee 1,091.475 -> 108,056.025.
    # Without fees after 2 years: 110,250. Total cost 2,193.975; fees paid 2,141.475; lost growth 52.50.
    s = stmt(ending_balance=100_000, holdings=[], transactions=[], market_change=None,
             period={"start": "2025-01-01", "end": "2026-01-01"})
    rate_scale = 365.25 / 365  # advisory rate is annualized on a 365.25-day year
    p = calc.project_fees(s, 2, growth=0.05)
    assert p["advisory_fee_rate"] == pytest.approx(0.01 * rate_scale, abs=1e-5)
    assert p["fees_paid"] == pytest.approx(2_141.475 * rate_scale, rel=2e-3)
    assert p["total_cost"] == pytest.approx(2_193.975 * rate_scale, rel=2e-3)
    assert p["lost_growth"] == pytest.approx(p["total_cost"] - p["fees_paid"], abs=0.02)


def test_project_fees_includes_expense_ratio():
    p = calc.project_fees(stmt(), 10, growth=0.0)
    assert p["fund_expense_rate"] == pytest.approx(650 / 110_000, abs=1e-5)
    assert p["annual_fee_rate"] == pytest.approx(p["advisory_fee_rate"] + p["fund_expense_rate"], abs=1e-5)


# ---------------------------------------------------------------- flags and allocation

def test_flag_costs():
    s = stmt(
        firm="Summit Wealth Partners",
        holdings=[
            {"ticker": "SPGAX", "name": "Summit Premier Growth Fund A", "value": 50_000, "asset_class": "us_stock", "expense_ratio": 0.0125},
            {"ticker": "IDX", "name": "Total Market Index", "value": 60_000, "asset_class": "us_stock", "expense_ratio": 0.0004},
        ],
        fees=[{"date": "2025-03-01", "kind": "load", "amount": 1_150},
              {"date": "2025-12-31", "kind": "advisory", "amount": 1_000}],
        transactions=[{"date": f"2025-{m:02d}-15", "type": t, "amount": 40_000}
                      for m in range(1, 13) for t in ("buy", "sell")],
        market_change=None,
    )
    kinds = {f["kind"] for f in calc.flag_costs(s)}
    assert kinds == {"expense_ratio", "sales_load", "proprietary_fund", "frequent_trading"}
    er = next(f for f in calc.flag_costs(s) if f["kind"] == "expense_ratio")
    assert er["annual_cost"] == 625.0  # 50,000 * 1.25%


def test_generic_firm_words_dont_match():
    s = stmt(firm="Capital Wealth Partners",
             holdings=[{"name": "Global Capital Fund", "value": 110_000, "expense_ratio": 0.0005}])
    assert not [f for f in calc.flag_costs(s) if f["kind"] == "proprietary_fund"]


def test_allocation_overlap():
    a = calc.allocation(stmt(holdings=[
        {"name": "X", "value": 30_000, "asset_class": "us_stock"},
        {"name": "Y", "value": 30_000, "asset_class": "us_stock"},
        {"name": "Z", "value": 40_000, "asset_class": "bond"},
    ]))
    assert a["by_class"]["us_stock"]["share"] == 0.6
    assert a["stock_share"] == 0.6
    assert a["overlaps"][0]["funds"] == ["X", "Y"]
