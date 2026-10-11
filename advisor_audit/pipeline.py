"""upload -> extract_text -> redact -> [agent: normalize] -> reconcile (retry / ask)
-> compute_performance + benchmark + project_fees + flag_costs -> [agent: explain]
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import calc, rules
from .config import settings
from .extract import extract_bytes
from .redact import redact
from .schema import Statement

log = logging.getLogger(__name__)

ADVISOR_UPSIDE = (
    "A return comparison can't see everything an advisor may do. Tax-loss harvesting, rebalancing, "
    "planning for withdrawals, and helping you stay invested when markets drop can all be worth money, "
    "and none of them show up in the numbers above."
)
DISCLAIMER = (
    "This is information about your statement, not financial advice. It doesn't recommend buying or "
    "selling anything or changing advisors."
)


@dataclass
class NormalizeOutcome:
    statement: Statement | None
    discrepancies: list[str] = field(default_factory=list)
    question: str | None = None
    engine: str = "model"
    attempts: int = 0
    notes: list[str] = field(default_factory=list)


def read_upload(filename: str, data: bytes) -> tuple[str, dict]:
    """Extract and redact in memory. The raw bytes and raw text go out of scope here."""
    text, counts = redact(extract_bytes(filename, data))
    return text, counts


def _use_model() -> bool:
    return settings.model_provider != "rules"


def normalize(text: str, clarification: str | None = None) -> NormalizeOutcome:
    if clarification:
        text += f"\n\nThe user clarified: {clarification}"
    notes: list[str] = []

    if _use_model() and settings.parser_first:
        fast = _parse_fully_checked(text)
        if fast is not None:
            return NormalizeOutcome(fast, engine="rules", attempts=0)

    if _use_model():
        try:
            return _normalize_model(text, clarification is not None)
        except Exception as e:  # model down, timeout, bad output
            log.warning("model normalize failed: %s", type(e).__name__)  # never log content
            if not settings.rules_fallback:
                raise
            notes.append("The model was unavailable, so a simpler built-in parser read the statement.")

    try:
        s = rules.normalize_rules(text)
    except rules.ParseError as e:
        return NormalizeOutcome(None, [str(e)], question="We couldn't read this statement's layout. "
                                "Could you upload a PDF or CSV export of it instead?", engine="rules", notes=notes)
    r = calc.reconcile(s)
    if r["ok"] or clarification is not None:
        return NormalizeOutcome(s, r["discrepancies"], engine="rules", attempts=1, notes=notes)
    return NormalizeOutcome(s, r["discrepancies"], question=_template_question(r["discrepancies"]),
                            engine="rules", attempts=1, notes=notes)


def _parse_fully_checked(text: str) -> Statement | None:
    """The built-in parser's result, but only if it's complete enough to trust without the model.

    It must reconcile, include holdings (so the ending balance is checked against them), and
    include the statement's market change (so a missed deposit or fee would show up).
    """
    try:
        s = rules.normalize_rules(text)
    except rules.ParseError:
        return None
    if not s.holdings or s.market_change is None:
        return None
    return s if not check_statement(s, text) else None


# Thousands separators only when followed by exactly three digits, so CSV commas split fields.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?![\d,])(?:\.\d+)?|\d+(?:\.\d+)?")


def _printed_numbers(text: str) -> set[float]:
    return {round(float(m.replace(",", "")), 4) for m in _NUMBER.findall(text)}


def unprinted_values(s: Statement, text: str) -> list[tuple[str, float]]:
    """Numbers in the statement that don't appear in the text. Rates may be printed as percentages."""
    printed = _printed_numbers(text)

    def ok(v: float) -> bool:
        return v == 0 or round(abs(v), 4) in printed or round(abs(v) * 100, 4) in printed

    out = [(k, getattr(s, k)) for k in ("starting_balance", "ending_balance", "market_change")
           if getattr(s, k) is not None and not ok(getattr(s, k))]
    for h in s.holdings:
        for k in ("value", "quantity", "expense_ratio", "load_pct"):
            v = getattr(h, k)
            if v is not None and not ok(v):
                out.append((f"{h.ticker or h.name} {k}", v))
    out += [(f"{t.date} {t.type} amount", t.amount) for t in s.transactions if not ok(t.amount)]
    out += [(f"{f.date} {f.kind} fee", f.amount) for f in s.fees if not ok(f.amount)]
    out += [(f"{p.date} balance", p.value) for p in s.balance_history if not ok(p.value)]
    return out


def _drop_unprinted_optional(s: Statement, text: str) -> Statement:
    """Last resort: clear optional fields the model computed instead of copying."""
    printed = _printed_numbers(text)
    ok = lambda v: v is None or v == 0 or round(abs(v), 4) in printed or round(abs(v) * 100, 4) in printed  # noqa: E731
    s = s.model_copy(deep=True)
    if not ok(s.market_change):
        s.market_change = None
    for h in s.holdings:
        for k in ("expense_ratio", "load_pct"):
            if not ok(getattr(h, k)):
                setattr(h, k, None)
        if not ok(h.quantity):
            h.quantity = 0.0
    return s


def check_statement(s: Statement, text: str) -> list[str]:
    issues = calc.reconcile(s)["discrepancies"]
    bad = unprinted_values(s, text)
    if bad:
        issues.append("These values don't appear anywhere in the statement text: "
                      + "; ".join(f"{k} = {v:g}" for k, v in bad[:10])
                      + ". Copy numbers exactly as printed and use null for anything not printed. Never calculate.")
    return issues


def _normalize_model(text: str, final: bool) -> NormalizeOutcome:
    from .agent import Normalizer

    n = Normalizer()
    s = n.first(text)
    issues = check_statement(s, text)
    while issues and n.attempts < settings.max_normalize_attempts:
        s = n.retry(issues)
        issues = check_statement(s, text)
    if issues:
        s = _drop_unprinted_optional(s, text)
        issues = check_statement(s, text)
    if not issues or final:
        return NormalizeOutcome(s, issues, attempts=n.attempts)
    try:
        q = n.ask(issues)
    except Exception:
        q = _template_question(issues)
    return NormalizeOutcome(s, issues, question=q, attempts=n.attempts)


def _template_question(discrepancies: list[str]) -> str:
    return (f"Your statement's numbers didn't add up: {discrepancies[0]} "
            "Could you check the statement and tell us the correct figure?")


def findings_for(s: Statement, risk_profile: str) -> dict:
    perf = calc.compute_performance(s)
    bench = calc.benchmark(s, risk_profile)
    return {
        "performance": perf,
        "benchmark": bench,
        "comparison": calc.compare(perf, bench),
        "projection_10": calc.project_fees(s, 10),
        "projection_20": calc.project_fees(s, 20),
        "flags": calc.flag_costs(s),
        "allocation": calc.allocation(s),
    }


def explain(findings: dict) -> tuple[dict, list[str]]:
    template = rules.explain_rules(findings)
    if not _use_model():
        return template, []
    try:
        from .agent import explain_with_model

        exp, problems = explain_with_model(findings)
    except Exception as e:
        log.warning("model explain failed: %s", type(e).__name__)
        return template, ["The model was unavailable, so the summary uses built-in wording."]
    # Any part that failed the number check falls back to the template.
    for k, v in exp.items():
        if not v:
            exp[k] = template[k]
    if len(exp["questions"]) < 3:
        exp["questions"] = (exp["questions"] + [q for q in template["questions"] if q not in exp["questions"]])[:3]
    return exp, (["Some model wording used numbers not in the calculations, so built-in wording replaced it."]
                 if problems else [])


def results(s: Statement, risk_profile: str, outcome: NormalizeOutcome | None = None) -> dict:
    findings = findings_for(s, risk_profile)
    explanation, notes = explain(findings)
    warnings = list(outcome.notes if outcome else []) + notes
    if outcome and outcome.discrepancies:
        warnings.append("These checks still didn't pass, so treat the numbers with care: "
                        + " ".join(outcome.discrepancies))
    return {
        **findings,
        "explanation": explanation,
        "advisor_upside": ADVISOR_UPSIDE,
        "disclaimer": DISCLAIMER,
        "risk_profile": risk_profile,
        "warnings": warnings,
        "engine": outcome.engine if outcome else None,
    }
