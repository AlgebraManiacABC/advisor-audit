# Benchmark price data

`etf_prices.csv` holds real monthly adjusted closes (dividends reinvested) for VTI and BND from
Yahoo Finance, fetched with `scripts/fetch_prices.py`. Re-run that script to refresh it.

The sample statements in `samples/` are built from a separate synthetic price series inside
`scripts/make_samples.py`, so they don't change when this file is refreshed.
