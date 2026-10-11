# Is my advisor worth their fee?

Hack Midwest 2026 (FinTech; AWS Strands Agents; Akamai Cloud AI). A novice investor uploads a
managed-account statement (PDF, CSV, XLSX, or a photo) and picks a risk profile. The app shows, in
plain English, what they own, what they paid in fees, and how the account did against a cheap ETF mix
of matching risk. It gives information, not advice: it never says to fire an advisor or to buy or sell anything.

## Run it

```bash
pip install -r requirements-dev.txt          # plus Tesseract: apt-get install tesseract-ocr
cp .env.example .env                         # add OPENAI_API_KEY for the Akamai endpoint
set -a; . ./.env; set +a
uvicorn advisor_audit.app:app --reload       # http://localhost:8000
```

`MODEL_PROVIDER=rules` runs with no model: a built-in parser for simple labeled statements (it reads
all the demo samples) and template wording. The app also falls back to it if the model fails.

```bash
pytest                                        # 32 tests, no network
python scripts/check_endpoint.py              # live check of the model endpoint using synthetic data
python scripts/make_samples.py                # regenerate samples/
```

Try `samples/statement.pdf`, `statement.csv`, `statement.xlsx`, or `statement_photo.jpg`. They're the same
fictional account, so the results match across formats.

## How it works

```
upload -> extract_text -> redact -> [agent: normalize, calls reconcile on its draft]
       -> reconcile (agent retries up to 3x, then asks the user one question)
       -> compute_performance + benchmark + project_fees + flag_costs -> [agent: explain] -> results
```

- **The model never does arithmetic.** All numbers come from `advisor_audit/calc.py`. The model reads
  messy text into the schema (`schema.py`) and words the explanation.
- **Code checks the model's output.** Every number in the model's wording must come from the tool outputs
  (`agent.invented_numbers`). Any part that fails is replaced with template wording.
- **Strands** (`agent.py`, `tools.py`): `@tool` functions, `structured_output_model` for the statement
  and explanation, `limits` on turns, and `max_tokens` on every call. The normalizing agent calls the
  `reconcile` tool on its own draft and fixes it. `extract_text` and `redact` run in code before the model
  sees anything, so the model can't pull unredacted text into its context.
- **Model:** Strands' `OpenAIModel` pointed at Akamai's OpenAI-compatible endpoint (`hackathon-chat`).
  `MODEL_PROVIDER=ollama` switches to a self-hosted Ollama on our own Linode. No AWS cloud services are used.

### Schema additions

The brief's schema is kept. Four optional fields were added: `firm` (to spot proprietary funds),
`market_change` (so `reconcile` can check the balance equation), `balance_history` (needed for
volatility and drawdown), and `holding.load_pct`.

### Calculations (`calc.py`)

| | Method |
|---|---|
| Return | Time-weighted (monthly Modified Dietz, chained) when the statement has periodic values; otherwise Modified Dietz. After fees, since balances are net of fees. |
| Fees | Fees taken from the account by kind, plus estimated fund expenses (average balance × weighted expense ratio × years). |
| Benchmark | Stock/bond ETF mix (30/70, 60/40, 90/10) bought with the starting balance on day one, same deposits and withdrawals on the same dates, rebalanced monthly. Same return method as the account. |
| Projection | Current advisory rate + weighted expense ratio, 5% growth, no new deposits. Reports fees paid and lost growth, plus a 0.03% ETF mix for contrast. |
| "Means anything?" | Says so when the period is under 3 years or the gap is under 1 percentage point. |
| Flags | Expense ratio ≥ 0.5%, sales loads, funds sharing a distinctive word with the firm's name, turnover > 50%/yr or > 2 trades/month. |

## Privacy

- Redaction (`redact.py`) runs before any text reaches the model: SSNs, account numbers, emails,
  phone numbers, addresses, and names on labeled lines. Tests check that the demo PII never reaches the model.
- Uploads are parsed in memory and never written to disk. Each session keeps only the normalized
  statement and results, in process memory, and expires after 30 minutes. "Delete my data" clears it at once.
  While a clarifying question is pending, the session also holds the *redacted* text.
- OCR (Tesseract) and parsing run on our server. All demo and test data is synthetic.

## Deploy (Akamai Linode)

```bash
# .env needs OPENAI_API_KEY and DOMAIN (DNS-only A record pointing at the Linode)
./deploy/deploy.sh root@<linode-ip>
```

This ships `git archive HEAD` and copies `.env` with owner-only permissions. It then runs Docker Compose
(the app, plus Caddy for HTTPS) and polls `/healthz`. There's one app process on purpose, since sessions
live in memory.

## Status and caveats

- Build-order steps 1–5, 7 and 8 are done and tested. Step 6 (deploy) is scripted but not yet run.
- **`data/etf_prices.csv` is synthetic placeholder data** (market-data sites were blocked in the build
  sandbox). Run `python scripts/fetch_prices.py` before showing benchmark numbers as real. See `data/README.md`.
- The agent path is tested with a scripted fake model. Run `scripts/check_endpoint.py` against the
  real endpoint before the demo, and pre-run the demo in case the shared endpoint is slow.
- The brief asks for inference on "our own Akamai instance". The workshop endpoint runs on Akamai but is
  a shared, hosted service, not ours. For strict self-hosting, use `MODEL_PROVIDER=ollama` on a GPU Linode.
- The rules parser handles simple labeled layouts only. Real-world statements need the model.
