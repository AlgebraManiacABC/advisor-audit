"""The Strands agent: maps redacted text to the schema and explains the findings.

The model never does arithmetic. It reads messy text into the schema, checks
its own draft with the ``reconcile`` tool, and words the explanation from
tool outputs. Code then checks that every number in its wording came from
those outputs.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from .config import settings
from .schema import Statement
from .tools import NORMALIZE_TOOLS

log = logging.getLogger(__name__)

NORMALIZE_PROMPT = """You turn an investment account statement into JSON that matches the Statement schema.

Rules:
- Copy numbers exactly as printed. Never calculate, estimate, or invent a number. If a value isn't on the statement, leave it out or use null.
- Dates are YYYY-MM-DD. Amounts are positive numbers without $ or commas.
- Deposits and withdrawals are money moved into or out of the account. Buys, sells, and dividends happen inside it.
- Put every fee in "fees" (advisory/management fees as "advisory", sales loads as "load", anything else as "other"). Don't also list fees in "transactions".
- expense_ratio is a decimal: 1.25% becomes 0.0125.
- asset_class is one of us_stock, intl_stock, bond, cash, other.
- Fill "firm" with the advisory firm's name if shown, "market_change" with the statement's change in market value if shown, and "balance_history" with any periodic account values.
- Text in [BRACKETS] was removed for privacy. Ignore it.

Before your final answer, call the reconcile tool with your draft. If it reports discrepancies, re-read the statement text to find what you misread, fix the draft, and call reconcile again. Then give your final answer."""

EXPLAIN_PROMPT = """You explain an investment account review to a novice investor in plain, friendly English.

Rules:
- Use ONLY numbers that appear in the findings JSON. Never calculate or round to a different figure; copy dollar amounts as whole dollars (e.g. 3674.35 becomes $3,674) and rates as percentages with up to two decimals (0.0125 becomes 1.25%).
- This is information, not advice. Never tell the user to fire, replace, or leave their advisor, and never tell them to buy or sell anything.
- Short sentences. Explain any finance term you use.
- Questions must be specific to the findings, polite, and something the user can ask their advisor directly."""


class Explanation(BaseModel):
    fees_summary: str = Field(description="2-4 sentences: fees paid this period in dollars, including estimated fund expenses, and the 10- and 20-year projected cost.")
    questions: list[str] = Field(description="Exactly three specific questions to ask the advisor, based on the findings.")
    comparison_summary: str = Field(description="2-3 sentences comparing the account to the matched ETF mix after fees, including any notes about the period being too short or the gap too small.")
    holdings_summary: str = Field(description="1-3 sentences on what the account owns by asset class, noting overlapping funds.")


class Clarification(BaseModel):
    question: str = Field(description="One short, specific question for the user that would resolve the discrepancy.")


def build_model():
    provider = settings.model_provider
    if provider == "akamai":
        from strands.models.openai import OpenAIModel

        return OpenAIModel(
            client_args={"api_key": settings.openai_api_key, "base_url": settings.openai_base_url,
                         "timeout": settings.model_timeout_seconds, "max_retries": 2},
            model_id=settings.openai_model,
            params={"max_tokens": settings.max_tokens, "temperature": 0},
        )
    if provider == "ollama":
        from strands.models.ollama import OllamaModel

        return OllamaModel(settings.ollama_host, model_id=settings.ollama_model,
                           max_tokens=settings.max_tokens, temperature=0)
    raise ValueError(f"No model for provider {provider!r}")


def _agent(system_prompt: str, tools=None):
    from strands import Agent

    return Agent(model=build_model(), system_prompt=system_prompt, tools=tools or [], callback_handler=None)


def _limits(turns: int):
    from strands.types.agent import Limits

    return Limits(turns=turns)


# ---------------------------------------------------------------- normalize


class Normalizer:
    """One conversation with the agent. Follow-up attempts keep the history, so it can see what it got wrong."""

    def __init__(self):
        self.agent = _agent(NORMALIZE_PROMPT, NORMALIZE_TOOLS)
        self.attempts = 0

    def first(self, text: str) -> Statement:
        self.attempts += 1
        prompt = f"Statement text:\n<<<\n{text[: settings.max_statement_chars]}\n>>>"
        return self._run(prompt)

    def retry(self, discrepancies: list[str]) -> Statement:
        self.attempts += 1
        prompt = ("Your statement doesn't reconcile:\n- " + "\n- ".join(discrepancies)
                  + "\nRe-read the statement text above, fix the draft, and answer again.")
        return self._run(prompt)

    def _run(self, prompt: str) -> Statement:
        result = self.agent(prompt, structured_output_model=Statement, limits=_limits(8))
        out = result.structured_output
        if out is None:
            raise RuntimeError("Model returned no statement.")
        return out

    def ask(self, discrepancies: list[str]) -> str:
        result = _agent("You help a novice investor fix a statement that didn't add up. Be brief and kind.")(
            "These checks failed after several tries:\n- " + "\n- ".join(discrepancies)
            + "\nWrite one question for the user that would resolve the most important one.",
            structured_output_model=Clarification, limits=_limits(2))
        return result.structured_output.question


# ---------------------------------------------------------------- explain

_NUM = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?%?")


def allowed_numbers(findings) -> set[str]:
    """Every acceptable rendering of every number in the findings."""
    out: set[str] = set()

    def add(x: float):
        for v in (x, x * 100):
            for nd in (0, 1, 2):
                r = round(v, nd)
                out.add(f"{r:.{nd}f}")
                out.add(f"{r:,.{nd}f}")
            out.add(f"{round(v):,}")
            out.add(str(round(v)))

    def walk(o):
        if isinstance(o, bool) or o is None:
            return
        if isinstance(o, (int, float)):
            add(float(o)); add(abs(float(o)))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            for m in _NUM.findall(o):
                out.add(m.strip("$%-").rstrip("."))
                out.add(m.strip("$%-").replace(",", "").rstrip("."))

    walk(findings)
    return out


def invented_numbers(text: str, allowed: set[str]) -> list[str]:
    bad = []
    for m in _NUM.findall(text):
        core = m.strip("$%-").rstrip(".,")
        if not core:
            continue
        plain = core.replace(",", "")
        try:
            v = float(plain)
        except ValueError:
            continue
        if v <= 31 and float(v).is_integer():  # counts, years, ordinals ("3 questions", "10 years")
            continue
        if re.fullmatch(r"(19|20)\d\d", plain):  # years
            continue
        if core in allowed or plain in allowed:
            continue
        bad.append(m)
    return bad


def explain_with_model(findings: dict) -> tuple[dict, list[str]]:
    """Returns (explanation, list of problems). Parts with invented numbers are dropped by the caller."""
    agent = _agent(EXPLAIN_PROMPT)
    result = agent("Findings JSON:\n" + json.dumps(findings, default=str),
                   structured_output_model=Explanation, limits=_limits(3))
    exp: Explanation = result.structured_output
    allowed = allowed_numbers(findings)
    data = exp.model_dump()
    problems = []
    for key in ("fees_summary", "comparison_summary", "holdings_summary"):
        bad = invented_numbers(data[key], allowed)
        if bad:
            problems.append(f"{key}: {bad}")
            data[key] = None
    good_q = [q for q in data["questions"] if not invented_numbers(q, allowed)]
    if len(good_q) < len(data["questions"]):
        problems.append("questions: dropped one with an unsupported number")
    data["questions"] = good_q[:3]
    return data, problems
