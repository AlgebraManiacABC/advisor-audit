"""Replace the synthetic data/etf_prices.csv with real monthly total-return prices.

Run where Yahoo Finance is reachable: python scripts/fetch_prices.py [VTI BND]
Uses adjusted closes (dividends reinvested). This data is public market data,
not statement content.
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "etf_prices.csv"


def monthly(ticker: str) -> dict[str, float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=15y&interval=1mo"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
    closes = data["indicators"]["adjclose"][0]["adjclose"]
    out = {}
    for ts, px in zip(data["timestamp"], closes):
        if px is not None:
            out[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()] = round(px, 4)
    return out


def main() -> None:
    tickers = sys.argv[1:] or ["VTI", "BND"]
    series = {t: monthly(t) for t in tickers}
    dates = sorted(set.intersection(*(set(s) for s in series.values())))
    with OUT.open("w") as f:
        f.write("date," + ",".join(tickers) + "\n")
        for d in dates:
            f.write(d + "," + ",".join(str(series[t][d]) for t in tickers) + "\n")
    print(f"Wrote {len(dates)} rows to {OUT}. Update data/README.md to say the prices are real.")


if __name__ == "__main__":
    main()
