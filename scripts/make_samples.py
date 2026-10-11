"""Generate the synthetic demo data.

- data/etf_prices.csv: a SYNTHETIC monthly total-return price series for the
  benchmark ETFs (see data/README.md). Replace with real history via
  scripts/fetch_prices.py before showing benchmark numbers as real.
- samples/: one fictional account rendered as CSV, XLSX, PDF, and a
  photo-like PNG, plus the ground-truth JSON they should all parse to.

The sample account is always built from the synthetic series, so the samples
don't change when real prices are fetched. The price file is only written if
it's missing or you pass --synthetic-prices.

Everything here is fictional. Run: python scripts/make_samples.py
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA, SAMPLES = ROOT / "data", ROOT / "samples"

FIRM = "Summit Wealth Partners"
HOLDER = "Jordan Avery"
ADVISOR = "Dana Whitfield"
ACCOUNT = "XXXX-XXXX-4821"
ADDRESS = ["1428 Maple Grove Ave, Apt 3", "Kansas City, MO 64111"]
START, END = date(2025, 1, 1), date(2025, 12, 31)
START_BAL = 250_000.00

# ticker, name, asset class, expense ratio, ending weight, price
HOLDINGS = [
    ("SPGAX", "Summit Premier Growth Fund A", "US Stock", 0.0125, 0.18, 41.87),
    ("TSMIX", "Total Stock Market Index Fund", "US Stock", 0.0004, 0.27, 128.42),
    ("LCIX", "Large Cap 500 Index Fund", "US Stock", 0.0005, 0.12, 512.10),
    ("INTX", "International Equity Index Fund", "Intl Stock", 0.0008, 0.10, 34.66),
    ("AGBX", "Aggregate Bond Index Fund", "Bond", 0.0005, 0.30, 9.81),
    ("CASH", "Cash Sweep", "Cash", None, 0.03, 1.00),
]
DEPOSITS = [(date(2025, 2, 14), 10_000.00), (date(2025, 8, 15), 5_000.00)]
WITHDRAWALS = [(date(2025, 11, 10), 8_000.00)]
LOAD = (date(2025, 3, 12), 1_150.00)  # 5.75% front-end load on a $20,000 SPGAX purchase
TRADES = [
    (date(2025, 3, 12), "Sell", "TSMIX", 21_150.00),
    (date(2025, 3, 12), "Buy", "SPGAX", 20_000.00),
    (date(2025, 6, 18), "Sell", "AGBX", 6_000.00),
    (date(2025, 6, 18), "Buy", "SPGAX", 6_000.00),
    (date(2025, 9, 22), "Sell", "LCIX", 4_500.00),
    (date(2025, 9, 22), "Buy", "INTX", 4_500.00),
]
DIVIDENDS = [(date(2025, 3, 31), "AGBX", 812.44), (date(2025, 6, 30), "TSMIX", 655.10),
             (date(2025, 9, 30), "AGBX", 798.03), (date(2025, 12, 31), "LCIX", 702.58)]


def month_ends(y0: int, m0: int, y1: int, m1: int) -> list[date]:
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        out.append(nxt - timedelta(days=1))
        y, m = nxt.year, nxt.month
    return out


def make_prices() -> dict[date, tuple[float, float]]:
    rng = random.Random(2026)
    dates = month_ends(2014, 12, 2026, 9)
    vti, bnd, rows = 100.0, 100.0, {}
    for d in dates:
        rows[d] = (round(vti, 4), round(bnd, 4))
        shock = rng.gauss(0, 1)
        vti *= 1 + 0.0085 + 0.045 * shock
        bnd *= 1 + 0.0025 + 0.012 * (0.2 * shock + rng.gauss(0, 1))
    path = DATA / "etf_prices.csv"
    if path.exists() and "--synthetic-prices" not in sys.argv:
        return rows  # keep the existing (possibly real) price file
    DATA.mkdir(exist_ok=True)
    with path.open("w") as f:
        f.write("date,VTI,BND\n")
        for d, (a, b) in rows.items():
            f.write(f"{d},{a},{b}\n")
    return rows


def make_account(prices: dict[date, tuple[float, float]]) -> dict:
    """Month-by-month account values: a 67/33 mix of the synthetic ETFs, less fund costs and fees."""
    rng = random.Random(11)
    ends = month_ends(2025, 1, 2025, 12)
    prev_px = prices[date(2024, 12, 31)]
    value, history, fees = START_BAL, [{"date": str(START), "value": START_BAL}], []
    wer = sum(h[3] * h[4] for h in HOLDINGS if h[3])
    for d in ends:
        px = prices[d]
        r = 0.67 * (px[0] / prev_px[0] - 1) + 0.33 * (px[1] / prev_px[1] - 1) - wer / 12 + rng.gauss(0, 0.002)
        prev_px = px
        value *= 1 + r
        value += sum(a for dd, a in DEPOSITS if dd.month == d.month)
        value -= sum(a for dd, a in WITHDRAWALS if dd.month == d.month)
        if d.month == LOAD[0].month:
            value -= LOAD[1]
            fees.append({"date": str(LOAD[0]), "kind": "load", "amount": LOAD[1]})
        if d.month in (3, 6, 9, 12):
            fee = round(value * 0.0025, 2)  # 1% a year, charged quarterly
            value -= fee
            fees.append({"date": str(d), "kind": "advisory", "amount": fee})
        value = round(value, 2)
        history.append({"date": str(d), "value": value})

    end_bal = value
    holdings, used = [], 0.0
    for i, (tick, name, ac, er, w, price) in enumerate(HOLDINGS):
        v = round(end_bal * w, 2) if i < len(HOLDINGS) - 1 else round(end_bal - used, 2)
        used += v
        holdings.append({"ticker": tick, "name": name,
                         "asset_class": {"US Stock": "us_stock", "Intl Stock": "intl_stock", "Bond": "bond", "Cash": "cash"}[ac],
                         "quantity": round(v / price, 3), "value": v, "expense_ratio": er, "price": price})

    txns = ([{"date": str(d), "type": "deposit", "ticker": None, "amount": a} for d, a in DEPOSITS]
            + [{"date": str(d), "type": "withdrawal", "ticker": None, "amount": a} for d, a in WITHDRAWALS]
            + [{"date": str(d), "type": t.lower(), "ticker": k, "amount": a} for d, t, k, a in TRADES]
            + [{"date": str(d), "type": "dividend", "ticker": k, "amount": a} for d, k, a in DIVIDENDS])
    txns.sort(key=lambda t: t["date"])
    total_fees = sum(f["amount"] for f in fees)
    dep, wd = sum(a for _, a in DEPOSITS), sum(a for _, a in WITHDRAWALS)
    return {
        "period": {"start": str(START), "end": str(END)},
        "starting_balance": START_BAL,
        "ending_balance": end_bal,
        "market_change": round(end_bal - START_BAL - dep + wd + total_fees, 2),
        "firm": FIRM,
        "holdings": holdings,
        "transactions": txns,
        "fees": fees,
        "balance_history": history,
    }


def _us(d: str) -> str:
    y, m, dd = d.split("-")
    return f"{m}/{dd}/{y}"


def _ac(code: str) -> str:
    return {"us_stock": "US Stock", "intl_stock": "Intl Stock", "bond": "Bond", "cash": "Cash"}[code]


def _activity(acct: dict) -> list[tuple[str, str, str]]:
    """(date, description, amount) rows, transactions and fees together, by date."""
    rows = []
    for t in acct["transactions"]:
        desc = t["type"].capitalize() + (f" {t['ticker']}" if t["ticker"] else "")
        rows.append((t["date"], desc, t["amount"]))
    for f in acct["fees"]:
        rows.append((f["date"], "Advisory fee" if f["kind"] == "advisory" else "Sales load SPGAX", f["amount"]))
    rows.sort(key=lambda r: r[0])
    return rows


def summary_rows(acct: dict) -> list[tuple[str, float]]:
    dep = sum(t["amount"] for t in acct["transactions"] if t["type"] == "deposit")
    wd = sum(t["amount"] for t in acct["transactions"] if t["type"] == "withdrawal")
    return [("Beginning balance", acct["starting_balance"]), ("Deposits", dep), ("Withdrawals", wd),
            ("Fees", sum(f["amount"] for f in acct["fees"])), ("Change in market value", acct["market_change"]),
            ("Ending balance", acct["ending_balance"])]


def render_lines(acct: dict) -> list[str]:
    """The paper statement as text lines (used for the image)."""
    L = [f"Advisory firm: {FIRM}", "Annual Account Statement",
         f"Account holder: {HOLDER}", f"Account number: {ACCOUNT}", *ADDRESS,
         f"Your advisor: {ADVISOR}", f"Statement period: {_us(acct['period']['start'])} - {_us(acct['period']['end'])}", "",
         "Account Summary"]
    L += [f"{k} ${v:,.2f}" for k, v in summary_rows(acct)]
    L += ["", "Holdings", "Ticker Name Class Quantity Price Value Exp. ratio"]
    for h in acct["holdings"]:
        er = f"{h['expense_ratio'] * 100:.2f}%" if h["expense_ratio"] is not None else "n/a"
        L.append(f"{h['ticker']} {h['name']} {_ac(h['asset_class'])} {h['quantity']:,.3f} {h['price']:,.2f} {h['value']:,.2f} {er}")
    L += ["", "Transactions", "Date Description Amount"]
    L += [f"{_us(d)} {desc} {a:,.2f}" for d, desc, a in _activity(acct)]
    L += ["", "Account Value History", "Date Value"]
    L += [f"{_us(p['date'])} {p['value']:,.2f}" for p in acct["balance_history"]]
    return L


def write_csv(acct: dict, path: Path) -> list[list[str]]:
    rows = [["Advisory firm", FIRM], ["Annual Account Statement"], ["Account holder", HOLDER],
            ["Account number", ACCOUNT], ["Address", ADDRESS[0] + ", " + ADDRESS[1]], ["Your advisor", ADVISOR],
            ["Statement period", f"{acct['period']['start']} to {acct['period']['end']}"], [],
            ["Account Summary"]]
    rows += [[k, f"{v:.2f}"] for k, v in summary_rows(acct)]
    rows += [[], ["Holdings"], ["Ticker", "Name", "Asset class", "Quantity", "Price", "Market value", "Expense ratio"]]
    for h in acct["holdings"]:
        er = f"{h['expense_ratio'] * 100:.2f}%" if h["expense_ratio"] is not None else "n/a"
        rows.append([h["ticker"], h["name"], _ac(h["asset_class"]), f"{h['quantity']:.3f}", f"{h['price']:.2f}", f"{h['value']:.2f}", er])
    rows += [[], ["Transactions"], ["Date", "Description", "Amount"]]
    rows += [[d, desc, f"{a:.2f}"] for d, desc, a in _activity(acct)]
    rows += [[], ["Account Value History"], ["Date", "Value"]]
    rows += [[p["date"], f"{p['value']:.2f}"] for p in acct["balance_history"]]
    import csv
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)
    return rows


def write_xlsx(rows: list[list[str]], path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Statement"
    for r in rows:
        ws.append(r)
    wb.save(path)


def write_pdf(acct: dict, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    grid = TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
                       ("FONTSIZE", (0, 0), (-1, -1), 8), ("ALIGN", (-1, 1), (-1, -1), "RIGHT")])
    story = [Paragraph(f"Advisory firm: {FIRM}", ss["Title"]), Paragraph("Annual Account Statement", ss["Heading2"])]
    for line in [f"Account holder: {HOLDER}", f"Account number: {ACCOUNT}", *ADDRESS, f"Your advisor: {ADVISOR}",
                 f"Statement period: {_us(acct['period']['start'])} - {_us(acct['period']['end'])}"]:
        story.append(Paragraph(line, ss["Normal"]))
    story += [Spacer(1, 10), Paragraph("Account Summary", ss["Heading3"])]
    for k, v in summary_rows(acct):
        story.append(Paragraph(f"{k} ${v:,.2f}", ss["Normal"]))
    story += [Spacer(1, 10), Paragraph("Holdings", ss["Heading3"])]
    t = [["Ticker", "Name", "Class", "Quantity", "Price", "Value", "Exp. ratio"]]
    for h in acct["holdings"]:
        er = f"{h['expense_ratio'] * 100:.2f}%" if h["expense_ratio"] is not None else "n/a"
        t.append([h["ticker"], h["name"], _ac(h["asset_class"]), f"{h['quantity']:,.3f}", f"{h['price']:,.2f}", f"{h['value']:,.2f}", er])
    story += [Table(t, style=grid), Spacer(1, 10), Paragraph("Transactions", ss["Heading3"])]
    t = [["Date", "Description", "Amount"]] + [[_us(d), desc, f"{a:,.2f}"] for d, desc, a in _activity(acct)]
    story += [Table(t, style=grid), Spacer(1, 10), Paragraph("Account Value History", ss["Heading3"])]
    t = [["Date", "Value"]] + [[_us(p["date"]), f"{p['value']:,.2f}"] for p in acct["balance_history"]]
    story.append(Table(t, style=grid))
    story += [Spacer(1, 14), Paragraph("SYNTHETIC DEMO STATEMENT. Fictional account, people, and firm.", ss["Italic"])]
    SimpleDocTemplate(str(path), pagesize=letter, title="Synthetic statement").build(story)


def write_image(acct: dict, path: Path) -> None:
    """A paper statement as if photographed: off-white, slightly rotated, a little noise."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    lines = render_lines(acct)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 30)
    except OSError:
        font = ImageFont.load_default(size=30)
    w, lh = 1900, 44
    img = Image.new("L", (w, 120 + lh * len(lines)), 246)
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((70, 60 + i * lh), line, fill=25, font=font)
    rng = random.Random(5)
    px = img.load()
    for _ in range(img.width * img.height // 60):
        x, y = rng.randrange(img.width), rng.randrange(img.height)
        px[x, y] = max(0, px[x, y] - rng.randrange(40))
    img = img.filter(ImageFilter.GaussianBlur(0.6)).rotate(0.8, expand=True, fillcolor=200)
    img.convert("RGB").save(path, quality=88)


def main() -> None:
    prices = make_prices()
    acct = make_account(prices)
    SAMPLES.mkdir(exist_ok=True)
    truth = {k: v for k, v in acct.items()}
    truth["holdings"] = [{k: v for k, v in h.items() if k != "price"} for h in acct["holdings"]]
    (SAMPLES / "expected.json").write_text(json.dumps(truth, indent=2) + "\n")
    rows = write_csv(acct, SAMPLES / "statement.csv")
    write_xlsx(rows, SAMPLES / "statement.xlsx")
    write_pdf(acct, SAMPLES / "statement.pdf")
    write_image(acct, SAMPLES / "statement_photo.jpg")
    print(f"Ending balance {acct['ending_balance']:,.2f}; wrote {SAMPLES}")


if __name__ == "__main__":
    main()
