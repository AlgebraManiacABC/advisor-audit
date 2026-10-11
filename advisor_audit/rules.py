"""No-model fallbacks.

``normalize_rules`` parses statements laid out as labeled lines plus holdings,
transactions, and value-history tables. That covers the synthetic demo
statements in every format (CSV, XLSX, PDF text, OCR text) and simple real
ones. Anything messier needs the model.

``explain_rules`` writes the plain-English summary and questions from
templates, using only tool outputs.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from .schema import Statement

DATE = r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})"
MONEY = r"(\(?-?\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,4})?\)?)"
ASSET = r"(US Stock|U\.S\. Stock|Intl Stock|International Stock|Bond|Cash|Other)"

ASSET_MAP = {"us stock": "us_stock", "u.s. stock": "us_stock", "intl stock": "intl_stock",
             "international stock": "intl_stock", "bond": "bond", "cash": "cash", "other": "other"}


class ParseError(ValueError):
    pass


def _money(s: str) -> float:
    s = s.strip()
    neg = s.startswith("(") or "-" in s
    v = float(re.sub(r"[^\d.]", "", s))
    return -v if neg else v


def _date(s: str) -> date:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ParseError(f"Bad date {s!r}")


def _lines(text: str) -> list[str]:
    raw = text.splitlines()
    # Drop pdfplumber table dumps; the same rows are in the page text.
    out, skip = [], False
    for line in raw:
        if line.startswith("[table "):
            skip = True
            continue
        if skip and not line.strip():
            skip = False
            continue
        if skip:
            continue
        out.append(line)
    # A CSV comma is one that isn't a thousands separator or followed by a space.
    csv_lines = sum(1 for l in out if re.search(r",(?! )(?!\d{3}(?:\D|$))", l))
    is_csv = csv_lines > 0.4 * max(1, sum(1 for l in out if l.strip()))
    if is_csv:
        out = [" ".join(c.strip() for c in row if c.strip()) for row in csv.reader(io.StringIO("\n".join(out)))]
    return [re.sub(r"\s+", " ", l.replace("|", " ")).strip() for l in out]


LABELS = {
    "starting_balance": r"(?:beginning|starting|opening) (?:balance|value|account value)",
    "ending_balance": r"(?:ending|closing) (?:balance|value|account value)",
    "market_change": r"(?:change in (?:market )?value|market (?:gain|change|change in value)|investment (?:gain|change))",
}

TXN_TYPES = [
    (r"advisory fee|management fee|advisor fee", "fee", "advisory"),
    (r"sales load|front[- ]end load|back[- ]end load|load", "fee", "load"),
    (r"fee", "fee", "other"),
    (r"deposit|contribution", "deposit", None),
    (r"withdrawal|distribution", "withdrawal", None),
    (r"buy|purchase", "buy", None),
    (r"sell|sale|redemption", "sell", None),
    (r"dividend|interest", "dividend", None),
]


def normalize_rules(text: str) -> Statement:
    lines = _lines(text)
    data: dict = {"holdings": [], "transactions": [], "fees": [], "balance_history": []}
    section = None

    for i, line in enumerate(lines):
        low = line.lower()
        if not line:
            continue
        if i < 6 and data.get("firm") is None and re.search(r"(?i)advisory firm\s*:?\s*(.+)", line):
            data["firm"] = re.search(r"(?i)advisory firm\s*:?\s*(.+)", line).group(1).strip()
            continue
        if "period" in low:
            ds = re.findall(DATE, line)
            if len(ds) >= 2:
                data["period"] = {"start": _date(ds[0]), "end": _date(ds[1])}
                continue

        matched = False
        for key, pat in LABELS.items():
            m = re.match(rf"(?i)^{pat}\b\D*?{MONEY}\s*$", line)
            if m:
                data[key] = _money(m.group(1))
                matched = True
        if matched:
            continue

        if re.match(r"(?i)^(holdings|positions|portfolio holdings)\b", line) and not re.search(MONEY + r"\s*$", line):
            section = "holdings"
            continue
        if re.match(r"(?i)^(transactions|account activity|activity)\b", line):
            section = "transactions"
            continue
        if re.match(r"(?i)^(account value history|value history|balance history|monthly values)\b", line):
            section = "history"
            continue
        if re.match(r"(?i)^(account summary|summary)\b", line):
            section = "summary"
            continue

        if section == "holdings":
            m = re.match(rf"^([A-Z][A-Z0-9]{{1,5}})\s+(.+?)\s+{ASSET}\s+{MONEY}\s+{MONEY}\s+{MONEY}\s*(\d+(?:\.\d+)?\s?%|n/?a|-+|—)?\s*$", line, re.I)
            if m:
                er = m.group(7)
                er_val = float(er.replace("%", "").strip()) / 100 if er and "%" in er else None
                data["holdings"].append({
                    "ticker": m.group(1).upper(), "name": m.group(2).strip(),
                    "asset_class": ASSET_MAP[m.group(3).lower()],
                    "quantity": _money(m.group(4)), "value": _money(m.group(6)), "expense_ratio": er_val,
                })
        elif section == "transactions":
            m = re.match(rf"^{DATE}\s+(.+?)\s+{MONEY}\s*$", line)
            if m:
                d, desc, amt = _date(m.group(1)), m.group(2), abs(_money(m.group(3)))
                for pat, typ, kind in TXN_TYPES:
                    if re.match(rf"(?i)^(?:{pat})\b", desc):
                        tick = re.search(r"\b([A-Z]{3,6})\b", desc[len(re.match(rf'(?i)^(?:{pat})', desc).group(0)):])
                        if typ == "fee":
                            data["fees"].append({"date": d, "kind": kind, "amount": amt})
                        else:
                            data["transactions"].append({"date": d, "type": typ, "amount": amt,
                                                         "ticker": tick.group(1) if tick else None})
                        break
        elif section == "history":
            m = re.match(rf"^{DATE}\s+{MONEY}\s*$", line)
            if m:
                data["balance_history"].append({"date": _date(m.group(1)), "value": _money(m.group(2))})

    for key in ("period", "starting_balance", "ending_balance"):
        if key not in data:
            raise ParseError(f"Couldn't find {key.replace('_', ' ')} in the statement.")
    return Statement.model_validate(data)


# ---------------------------------------------------------------- explanation


def _usd(x: float) -> str:
    return f"${x:,.0f}"


def explain_rules(findings: dict) -> dict:
    perf, proj10, proj20 = findings["performance"], findings["projection_10"], findings["projection_20"]
    flags, cmp_ = findings["flags"], findings["comparison"]
    alloc = findings["allocation"]

    fees_line = (
        f"You paid {_usd(perf['fees_paid_total'])} in fees taken from your account between "
        f"{perf['period_start']} and {perf['period_end']}."
    )
    if perf.get("estimated_fund_expenses"):
        fees_line += (f" Your funds also charged an estimated {_usd(perf['estimated_fund_expenses'])} "
                      "inside their own prices, which never shows up as a line on your statement.")
    proj_line = (
        f"At your current total cost of {proj10['annual_fee_rate']:.2%} a year, fees would cost about "
        f"{_usd(proj10['total_cost'])} over 10 years and {_usd(proj20['total_cost'])} over 20 years, "
        f"counting the growth that money would have earned. That assumes "
        f"{proj10['assumed_annual_growth']:.0%} yearly growth and no new deposits."
    )

    questions = []
    if perf.get("advisory_fee_rate_annual"):
        questions.append(
            f"My advisory fee works out to about {perf['advisory_fee_rate_annual']:.2%} a year. "
            "What services does that cover, and is there a lower rate at my balance?"
        )
    by_kind = {f["kind"]: f for f in flags}
    if "expense_ratio" in by_kind:
        f = by_kind["expense_ratio"]
        questions.append(f"Why do I hold {f['holding']}, and is there a lower-cost fund that does the same job?")
    if "sales_load" in by_kind:
        questions.append("I paid a sales load this year. Why was a fund with a load chosen, and who received it?")
    if "proprietary_fund" in by_kind:
        questions.append("Some of my funds are run by your firm. How are you paid when I own them?")
    if alloc["overlaps"]:
        questions.append("Several of my funds hold the same kind of investments. What does each one add?")
    if cmp_.get("available"):
        questions.append("How do you measure whether my account is on track, and what benchmark do you use?")
    questions.append("What have you done this year that wouldn't show up in my returns, like tax-loss harvesting?")

    if cmp_.get("available"):
        word = "more" if cmp_["gap"] >= 0 else "less"
        cmp_line = (
            f"After fees, your account returned {cmp_['account_return']:.1%} and the matched low-cost "
            f"ETF mix returned {cmp_['benchmark_return']:.1%} with the same deposits and withdrawals, "
            f"so your account earned {abs(cmp_['gap']):.1%} {word}."
        )
    else:
        cmp_line = " ".join(cmp_.get("notes", []))

    parts = [f"{v['share']:.0%} {v['label']}" for v in alloc["by_class"].values()]
    own_line = "Your account holds " + ", ".join(parts) + "."
    if alloc["overlaps"]:
        own_line += " " + " ".join(o["detail"] for o in alloc["overlaps"])

    return {
        "fees_summary": fees_line + " " + proj_line,
        "questions": questions[:3],
        "comparison_summary": cmp_line,
        "holdings_summary": own_line,
    }
