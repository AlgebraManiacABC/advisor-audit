"""upload -> extract_text -> redact -> [agent: normalize] -> reconcile (retry / ask)
-> compute_performance + benchmark + project_fees + flag_costs -> [agent: explain]
"""

from __future__ import annotations

import logging
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

    if _use_model():
        try:
            return _normalize_model(text, clarification is not None)
        except Exception as e:  # model down, timeout, bad output
            log.warning("model normalize failed: %s", e)
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


def _normalize_model(text: str, final: bool) -> NormalizeOutcome:
    from .agent import Normalizer

    n = Normalizer()
    s = n.first(text)
    r = calc.reconcile(s)
    while not r["ok"] and n.attempts < settings.max_normalize_attempts:
        s = n.retry(r["discrepancies"])
        r = calc.reconcile(s)
    if r["ok"] or final:
        return NormalizeOutcome(s, r["discrepancies"], attempts=n.attempts)
    try:
        q = n.ask(r["discrepancies"])
    except Exception:
        q = _template_question(r["discrepancies"])
    return NormalizeOutcome(s, r["discrepancies"], question=q, attempts=n.attempts)


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
        log.warning("model explain failed: %s", e)
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
