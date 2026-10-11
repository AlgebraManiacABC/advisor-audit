"""Strands tools. Each wraps a deterministic function from ``calc``, ``extract``, or ``redact``.

The agent is only given the statement-level tools. ``extract_text`` and
``redact`` run in code before the model sees anything, so the model can
never pull raw (unredacted) statement text into its context.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from strands import tool

from . import calc
from .extract import extract_bytes
from .redact import redact as _redact
from .schema import Statement


def _statement(statement: dict) -> Statement:
    return Statement.model_validate(statement)


@tool
def extract_text(file_path: str) -> str:
    """Return the raw text of a PDF, CSV, XLSX, or image statement. Runs locally (pdfplumber, pandas, Tesseract)."""
    p = Path(file_path)
    return extract_bytes(p.name, p.read_bytes())


@tool
def redact(text: str) -> str:
    """Strip account numbers, SSNs, names, and addresses from statement text."""
    return _redact(text)[0]


@tool
def reconcile(statement: dict) -> dict:
    """Check a draft normalized statement: starting balance + deposits - withdrawals - fees + market change
    must equal the ending balance, holdings must add up to the ending balance, and dates must fall in the
    period. Returns {"ok": bool, "discrepancies": [...], "figures": {...}}. Call this on your draft before
    giving your final answer, fix any discrepancies by re-reading the statement text, and call it again."""
    try:
        return calc.reconcile(_statement(statement))
    except ValidationError as e:
        return {"ok": False, "discrepancies": [f"Schema error: {err['loc']}: {err['msg']}" for err in e.errors()[:8]]}


@tool
def compute_performance(statement: dict) -> dict:
    """Fee-adjusted return, total fees in dollars, volatility, and max drawdown for a normalized statement."""
    return calc.compute_performance(_statement(statement))


@tool
def benchmark(statement: dict, risk_profile: str) -> dict:
    """Return of the matched low-cost ETF mix (conservative 30/70, balanced 60/40, growth 90/10 stock/bond)
    over the statement period, with the statement's deposits and withdrawals applied."""
    return calc.benchmark(_statement(statement), risk_profile)


@tool
def project_fees(statement: dict, years: int) -> dict:
    """Dollar cost of the account's current fee rate projected forward the given number of years."""
    return calc.project_fees(_statement(statement), years)


@tool
def flag_costs(statement: dict) -> list:
    """Flag high expense ratios, sales loads, proprietary funds, and frequent trading."""
    return calc.flag_costs(_statement(statement))


NORMALIZE_TOOLS = [reconcile]
ALL_TOOLS = [extract_text, redact, reconcile, compute_performance, benchmark, project_fees, flag_costs]
