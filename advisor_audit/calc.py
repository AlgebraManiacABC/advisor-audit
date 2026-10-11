"""Deterministic calculations. All arithmetic in the app happens here, never in the model.

Conventions:
- Returns are decimals (0.05 = 5%).
- Deposits and withdrawals are external cash flows. Fees, buys, sells, and
  dividends happen inside the account and are not external flows.
- Account balances on the statement are already net of fees taken from the
  account, so the account's return is its after-fee return.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date

from .config import BENCHMARK_MIXES, settings
from .prices import PriceSeries, load_prices
from .schema import Fee, Statement

GENERIC_FIRM_WORDS = {
    "the", "and", "of", "fund", "funds", "capital", "wealth", "partners", "advisors", "advisers",
    "advisory", "group", "financial", "investment", "investments", "management", "asset", "assets",
    "securities", "llc", "inc", "co", "company", "trust", "global", "private", "services", "planning",
}


# ---------------------------------------------------------------- helpers


def _years(start: date, end: date) -> float:
    return max((end - start).days, 1) / 365.25


def fee_items(s: Statement) -> list[Fee]:
    """The statement's fees. Falls back to fee transactions if the fee list is empty."""
    if s.fees:
        return list(s.fees)
    return [Fee(date=t.date, kind="other", amount=t.amount) for t in s.transactions if t.type == "fee"]


def external_flows(s: Statement) -> list[tuple[date, float]]:
    """Deposits (+) and withdrawals (-), in date order."""
    flows = [(t.date, t.amount if t.type == "deposit" else -t.amount)
             for t in s.transactions if t.type in ("deposit", "withdrawal")]
    return sorted(flows)


def average_balance(s: Statement) -> float:
    if len(s.balance_history) >= 2:
        return statistics.fmean(p.value for p in s.balance_history)
    return (s.starting_balance + s.ending_balance) / 2


def weighted_expense_ratio(s: Statement) -> tuple[float | None, float]:
    """(value-weighted expense ratio of holdings with a known ratio, share of value covered)."""
    total = sum(h.value for h in s.holdings)
    known = [(h.value, h.expense_ratio) for h in s.holdings if h.expense_ratio is not None]
    known_value = sum(v for v, _ in known)
    if not total or not known_value:
        return None, 0.0
    return sum(v * er for v, er in known) / known_value, known_value / total


def modified_dietz(start_value: float, end_value: float, flows: list[tuple[date, float]],
                   start: date, end: date) -> float:
    days = max((end - start).days, 1)
    net = sum(a for _, a in flows)
    weighted = sum(a * (end - d).days / days for d, a in flows)
    denom = start_value + weighted
    if denom <= 0:
        return 0.0
    return (end_value - start_value - net) / denom


def _annualize(r: float, years: float) -> float:
    return (1 + r) ** (1 / years) - 1 if years > 0 and r > -1 else r


def series_stats(points: list[tuple[date, float]], flows: list[tuple[date, float]]) -> dict:
    """Time-weighted return, volatility, and max drawdown from periodic values.

    Each sub-period uses Modified Dietz on the flows inside it, and the
    sub-period returns are chained.
    """
    points = sorted(points)
    rets = []
    for (d0, v0), (d1, v1) in zip(points, points[1:]):
        sub = [(d, a) for d, a in flows if d0 < d <= d1]
        rets.append(modified_dietz(v0, v1, sub, d0, d1))
    index, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        index *= 1 + r
        peak = max(peak, index)
        mdd = min(mdd, index / peak - 1)
    vol = None
    if len(rets) >= 3:
        avg_days = (points[-1][0] - points[0][0]).days / len(rets)
        per_year = 365.25 / max(avg_days, 1)
        vol = statistics.stdev(rets) * math.sqrt(per_year)
    return {"twr": index - 1, "volatility": vol, "max_drawdown": mdd, "periods": len(rets)}


# ---------------------------------------------------------------- reconcile


def reconcile(s: Statement) -> dict:
    """Check that start + deposits - withdrawals - fees + gains = end, plus sanity checks.

    Returns ``{"ok": bool, "discrepancies": [str], "figures": {...}}``.
    """
    tol = max(1.0, 0.001 * abs(s.ending_balance))
    issues: list[str] = []

    deposits = sum(t.amount for t in s.transactions if t.type == "deposit")
    withdrawals = sum(t.amount for t in s.transactions if t.type == "withdrawal")
    fees = sum(f.amount for f in fee_items(s))
    implied_gain = s.ending_balance - s.starting_balance - deposits + withdrawals + fees

    if s.period.end <= s.period.start:
        issues.append(f"Period end {s.period.end} is not after period start {s.period.start}.")
    if s.starting_balance < 0 or s.ending_balance < 0:
        issues.append("A balance is negative.")

    if s.market_change is not None and abs(implied_gain - s.market_change) > tol:
        issues.append(
            f"Balance equation is off by {implied_gain - s.market_change:,.2f}: "
            f"start {s.starting_balance:,.2f} + deposits {deposits:,.2f} - withdrawals {withdrawals:,.2f} "
            f"- fees {fees:,.2f} + market change {s.market_change:,.2f} "
            f"= {s.starting_balance + deposits - withdrawals - fees + s.market_change:,.2f}, "
            f"but ending balance is {s.ending_balance:,.2f}. Check for missed deposits, withdrawals, or fees."
        )

    if s.holdings:
        hsum = sum(h.value for h in s.holdings)
        if abs(hsum - s.ending_balance) > tol:
            issues.append(
                f"Holdings add up to {hsum:,.2f} but the ending balance is {s.ending_balance:,.2f} "
                f"(off by {hsum - s.ending_balance:,.2f}). Check for a missed or duplicated holding."
            )

    fee_txns = sum(t.amount for t in s.transactions if t.type == "fee")
    if s.fees and fee_txns and abs(fee_txns - fees) > tol:
        issues.append(f"Fee transactions total {fee_txns:,.2f} but the fee list totals {fees:,.2f}.")

    outside = [str(x.date) for x in [*s.transactions, *s.fees]
               if not s.period.start <= x.date <= s.period.end]
    if outside:
        issues.append(f"{len(outside)} dated item(s) fall outside the statement period: {', '.join(outside[:5])}.")

    if s.balance_history:
        bh = sorted(s.balance_history, key=lambda p: p.date)
        if bh[-1].date == s.period.end and abs(bh[-1].value - s.ending_balance) > tol:
            issues.append(f"Last balance-history value {bh[-1].value:,.2f} doesn't match ending balance.")
        if bh[0].date == s.period.start and abs(bh[0].value - s.starting_balance) > tol:
            issues.append(f"First balance-history value {bh[0].value:,.2f} doesn't match starting balance.")

    base = s.starting_balance + deposits
    if base > 0 and s.period.end > s.period.start:
        r = implied_gain / base
        bound = 0.6 * max(1.0, _years(s.period.start, s.period.end))
        if abs(r) > bound:
            issues.append(
                f"Implied market gain of {implied_gain:,.2f} ({r:.0%}) is implausible for this period. "
                "A balance, deposit, or withdrawal was probably misread."
            )

    return {
        "ok": not issues,
        "discrepancies": issues,
        "figures": {
            "deposits": round(deposits, 2),
            "withdrawals": round(withdrawals, 2),
            "fees": round(fees, 2),
            "implied_market_change": round(implied_gain, 2),
            "tolerance": round(tol, 2),
        },
    }


# ---------------------------------------------------------------- performance


def compute_performance(s: Statement) -> dict:
    start, end = s.period.start, s.period.end
    years = _years(start, end)
    flows = external_flows(s)

    by_kind: dict[str, float] = defaultdict(float)
    for f in fee_items(s):
        by_kind[f.kind] += f.amount
    cash_fees = sum(by_kind.values())

    avg_bal = average_balance(s)
    wer, coverage = weighted_expense_ratio(s)
    fund_expenses = avg_bal * wer * years if wer is not None else None

    net_return = modified_dietz(s.starting_balance, s.ending_balance, flows, start, end)
    gross_return = modified_dietz(s.starting_balance, s.ending_balance + cash_fees, flows, start, end)

    out = {
        "period_start": str(start),
        "period_end": str(end),
        "period_years": round(years, 2),
        "starting_balance": round(s.starting_balance, 2),
        "ending_balance": round(s.ending_balance, 2),
        "net_deposits": round(sum(a for _, a in flows), 2),
        "fees_paid": {k: round(v, 2) for k, v in by_kind.items()},
        "fees_paid_total": round(cash_fees, 2),
        "estimated_fund_expenses": round(fund_expenses, 2) if fund_expenses is not None else None,
        "expense_ratio_coverage": round(coverage, 3),
        "total_cost": round(cash_fees + (fund_expenses or 0.0), 2),
        "average_balance": round(avg_bal, 2),
        "advisory_fee_rate_annual": round(by_kind.get("advisory", 0.0) / years / avg_bal, 5) if avg_bal else None,
        "weighted_expense_ratio": round(wer, 5) if wer is not None else None,
        "return_after_fees": round(net_return, 5),
        "return_before_fees": round(gross_return, 5),
        "return_method": "modified_dietz",
        "volatility": None,
        "max_drawdown": None,
    }
    if years > 1:
        out["return_after_fees_annualized"] = round(_annualize(net_return, years), 5)

    if len(s.balance_history) >= 2:
        pts = [(p.date, p.value) for p in s.balance_history]
        st = series_stats(pts, flows)
        out.update(
            return_after_fees=round(st["twr"], 5),
            return_method="time_weighted",
            volatility=round(st["volatility"], 5) if st["volatility"] is not None else None,
            max_drawdown=round(st["max_drawdown"], 5),
        )
        if years > 1:
            out["return_after_fees_annualized"] = round(_annualize(st["twr"], years), 5)
    return out


# ---------------------------------------------------------------- benchmark


def simulate_benchmark(mix: dict[str, float], starting_balance: float, flows: list[tuple[date, float]],
                       start: date, end: date, prices: PriceSeries,
                       valuation_dates: list[date]) -> dict[date, float]:
    """Value of a stock/bond ETF mix over time, rebalanced monthly, with the account's flows applied."""
    tickers = {"stock": settings.stock_etf, "bond": settings.bond_etf}
    weights = {tickers[k]: w for k, w in mix.items()}

    events: dict[date, float] = defaultdict(float)
    for d, a in flows:
        events[d] += a
    rebalance = set(prices.dates_between(start, end))
    timeline = sorted({start, end, *events, *rebalance, *valuation_dates})

    units = {t: starting_balance * w / prices.price(t, start) for t, w in weights.items()}
    values: dict[date, float] = {}
    for d in timeline:
        value = sum(u * prices.price(t, d) for t, u in units.items()) + events.get(d, 0.0)
        if d == start or d in events or d in rebalance:
            units = {t: value * w / prices.price(t, d) for t, w in weights.items()}
        values[d] = value
    return values


def benchmark(s: Statement, risk_profile: str, prices: PriceSeries | None = None) -> dict:
    prices = prices or load_prices()
    mix = BENCHMARK_MIXES[risk_profile]
    start, end = s.period.start, s.period.end
    if not prices.covers(start, end):
        return {"available": False,
                "reason": f"Bundled ETF prices cover {prices.first} to {prices.last}; "
                          f"the statement runs {start} to {end}."}

    flows = external_flows(s)
    vdates = [p.date for p in s.balance_history if start <= p.date <= end]
    values = simulate_benchmark(mix, s.starting_balance, flows, start, end, prices, vdates)
    ending = values[end]

    out = {
        "available": True,
        "risk_profile": risk_profile,
        "mix": {settings.stock_etf: mix["stock"], settings.bond_etf: mix["bond"]},
        "description": f"{mix['stock']:.0%} total US stock market ETF ({settings.stock_etf}) / "
                       f"{mix['bond']:.0%} total bond market ETF ({settings.bond_etf}), rebalanced monthly",
        "ending_value": round(ending, 2),
        "return": round(modified_dietz(s.starting_balance, ending, flows, start, end), 5),
        "return_method": "modified_dietz",
        "volatility": None,
        "max_drawdown": None,
    }
    if len(vdates) >= 2:
        st = series_stats([(d, values[d]) for d in vdates], flows)
        out["return"] = round(st["twr"], 5)
        out["return_method"] = "time_weighted"
        out["volatility"] = round(st["volatility"], 5) if st["volatility"] is not None else None
        out["max_drawdown"] = round(st["max_drawdown"], 5)
    years = _years(start, end)
    if years > 1:
        out["return_annualized"] = round(_annualize(out["return"], years), 5)
    return out


def compare(perf: dict, bench: dict) -> dict:
    """Account vs benchmark, with plain statements about whether the gap means anything."""
    if not bench.get("available"):
        return {"available": False, "notes": [bench.get("reason", "Benchmark unavailable.")]}
    years = perf["period_years"]
    acct = perf.get("return_after_fees_annualized", perf["return_after_fees"])
    bmk = bench.get("return_annualized", bench["return"])
    gap = acct - bmk
    notes = []
    if years < 3:
        notes.append(
            f"This statement covers about {years:g} year{'s' if years != 1 else ''}. Comparisons over less than about 3 years "
            "mostly reflect short-term market swings, so treat the gap as a rough signal only."
        )
    if abs(gap) < 0.01:
        notes.append("The gap is under 1 percentage point, which is too small to mean much on its own.")
    return {
        "available": True,
        "account_return": acct,
        "benchmark_return": bmk,
        "annualized": years > 1,
        "gap": round(gap, 5),
        "dollar_difference": round(perf["ending_balance"] - bench["ending_value"], 2),
        "account_volatility": perf["volatility"],
        "benchmark_volatility": bench["volatility"],
        "account_max_drawdown": perf["max_drawdown"],
        "benchmark_max_drawdown": bench["max_drawdown"],
        "meaningful": years >= 3 and abs(gap) >= 0.01,
        "notes": notes,
    }


# ---------------------------------------------------------------- fees forward


def _project(balance: float, rate: float, growth: float, years: int) -> tuple[float, float]:
    """(ending value, total fees paid) after ``years`` of growth with fees charged on the balance."""
    paid = 0.0
    for _ in range(years):
        balance *= 1 + growth
        fee = balance * rate
        paid += fee
        balance -= fee
    return balance, paid


def project_fees(s: Statement, years: int, growth: float | None = None) -> dict:
    """Dollar cost of the current fee rate projected forward, with no new deposits."""
    growth = settings.projection_return if growth is None else growth
    perf_years = _years(s.period.start, s.period.end)
    avg_bal = average_balance(s)
    advisory = sum(f.amount for f in fee_items(s) if f.kind == "advisory")
    advisory_rate = advisory / perf_years / avg_bal if avg_bal else 0.0
    wer, _ = weighted_expense_ratio(s)
    rate = advisory_rate + (wer or 0.0)

    bal = s.ending_balance
    no_fee_value, _ = _project(bal, 0.0, growth, years)
    with_fee_value, paid = _project(bal, rate, growth, years)

    mix_rate = settings.stock_etf_er  # both benchmark ETFs cost about the same
    cheap_value, cheap_paid = _project(bal, mix_rate, growth, years)

    return {
        "years": years,
        "starting_balance": round(bal, 2),
        "assumed_annual_growth": growth,
        "annual_fee_rate": round(rate, 5),
        "advisory_fee_rate": round(advisory_rate, 5),
        "fund_expense_rate": round(wer, 5) if wer is not None else None,
        "fees_paid": round(paid, 2),
        "lost_growth": round(no_fee_value - with_fee_value - paid, 2),
        "total_cost": round(no_fee_value - with_fee_value, 2),
        "ending_value_with_fees": round(with_fee_value, 2),
        "low_cost_comparison": {
            "annual_fee_rate": mix_rate,
            "fees_paid": round(cheap_paid, 2),
            "total_cost": round(no_fee_value - cheap_value, 2),
        },
    }


# ---------------------------------------------------------------- flags and allocation


def _firm_words(firm: str | None) -> set[str]:
    if not firm:
        return set()
    words = {w.strip(".,&").lower() for w in firm.split()}
    return {w for w in words if len(w) > 2 and w not in GENERIC_FIRM_WORDS}


def flag_costs(s: Statement) -> list[dict]:
    flags: list[dict] = []
    for h in s.holdings:
        label = h.name or h.ticker
        if h.expense_ratio is not None and h.expense_ratio >= 0.005:
            sev = "high" if h.expense_ratio >= 0.01 else "medium"
            flags.append({
                "kind": "expense_ratio", "severity": sev, "holding": label,
                "detail": f"{label} has an expense ratio of {h.expense_ratio:.2%} a year. "
                          "Broad index funds often charge under 0.10%.",
                "annual_cost": round(h.value * h.expense_ratio, 2),
            })
        if h.load_pct:
            flags.append({"kind": "sales_load", "severity": "high", "holding": label,
                          "detail": f"{label} carries a {h.load_pct:.2%} sales load."})

    loads = [f for f in fee_items(s) if f.kind == "load"]
    if loads:
        total = sum(f.amount for f in loads)
        flags.append({"kind": "sales_load", "severity": "high", "holding": None,
                      "detail": f"You paid ${total:,.2f} in sales loads this period.", "amount": round(total, 2)})

    fw = _firm_words(s.firm)
    for h in s.holdings:
        if fw and fw & {w.strip(".,&").lower() for w in h.name.split()}:
            flags.append({"kind": "proprietary_fund", "severity": "medium", "holding": h.name or h.ticker,
                          "detail": f"{h.name} appears to be run by the advisor's own firm ({s.firm}). "
                                    "Firms can earn more from their own funds."})

    trades = [t for t in s.transactions if t.type in ("buy", "sell")]
    years = _years(s.period.start, s.period.end)
    avg_bal = average_balance(s)
    if trades and avg_bal:
        traded = sum(t.amount for t in trades)
        turnover = traded / 2 / avg_bal / years
        per_month = len(trades) / (years * 12)
        if turnover > 0.5 or per_month > 2:
            flags.append({"kind": "frequent_trading", "severity": "medium", "holding": None,
                          "detail": f"{len(trades)} trades this period, about {turnover:.0%} annual turnover. "
                                    "Frequent trading can add costs that don't appear as fees.",
                          "turnover": round(turnover, 3)})

    unknown = [h.name or h.ticker for h in s.holdings if h.expense_ratio is None and h.asset_class != "cash"]
    if unknown:
        flags.append({"kind": "missing_expense_ratio", "severity": "info", "holding": None,
                      "detail": "The statement doesn't list expense ratios for: " + ", ".join(unknown)
                                + ". Their fund costs aren't included above."})
    return flags


ASSET_CLASS_LABELS = {
    "us_stock": "US stocks", "intl_stock": "international stocks", "bond": "bonds",
    "cash": "cash", "other": "other investments",
}


def allocation(s: Statement) -> dict:
    total = sum(h.value for h in s.holdings)
    by_class: dict[str, float] = defaultdict(float)
    members: dict[str, list[str]] = defaultdict(list)
    for h in s.holdings:
        by_class[h.asset_class] += h.value
        members[h.asset_class].append(h.name or h.ticker)
    overlaps = [
        {"asset_class": c, "funds": names,
         "detail": f"{len(names)} funds all hold {ASSET_CLASS_LABELS[c]}, so they likely own many of the same securities."}
        for c, names in members.items() if c in ("us_stock", "intl_stock", "bond") and len(names) > 1
    ]
    return {
        "total": round(total, 2),
        "by_class": {c: {"value": round(v, 2), "share": round(v / total, 4) if total else 0.0,
                         "label": ASSET_CLASS_LABELS[c]} for c, v in sorted(by_class.items(), key=lambda kv: -kv[1])},
        "stock_share": round((by_class["us_stock"] + by_class["intl_stock"]) / total, 4) if total else 0.0,
        "overlaps": overlaps,
    }
