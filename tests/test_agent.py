"""The Strands agent wiring, driven by a scripted fake model (no network)."""

import json
from pathlib import Path

import pytest
from strands.models.model import Model

from advisor_audit import agent as A
from advisor_audit import pipeline, rules
from advisor_audit.config import settings
from advisor_audit.extract import extract_bytes
from advisor_audit.redact import redact

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class ScriptedModel(Model):
    """Replies with one tool call per turn, from a script. "OUT" means the structured-output tool."""

    def __init__(self, script):
        self.script = list(script)
        self.seen_tools = []
        self.seen_text = []

    def update_config(self, **kw):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, *a, **k):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        names = [t["name"] for t in (tool_specs or [])]
        self.seen_tools.append(names)
        self.seen_text.append(json.dumps(messages, default=str))
        name, payload = self.script.pop(0)
        if name == "OUT":
            name = next(n for n in names if n != "reconcile")
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": f"t{len(self.seen_tools)}"}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


@pytest.fixture
def text():
    return redact(extract_bytes("s.csv", (SAMPLES / "statement.csv").read_bytes()))[0]


@pytest.fixture
def good(text):
    return rules.normalize_rules(text).model_dump(mode="json")


@pytest.fixture
def use_model(monkeypatch):
    monkeypatch.setattr(settings, "model_provider", "akamai")

    def install(script):
        m = ScriptedModel(script)
        monkeypatch.setattr(A, "build_model", lambda: m)
        return m

    return install


def test_model_never_sees_pii(use_model, text, good):
    m = use_model([("OUT", good)])
    pipeline.normalize(text)
    for word in ["Jordan", "Avery", "Whitfield", "4821", "Maple"]:
        assert word not in m.seen_text[0]


def test_agent_checks_its_draft_and_retries(use_model, text, good):
    bad = dict(good, ending_balance=good["ending_balance"] + 500)
    m = use_model([("reconcile", {"statement": bad}), ("OUT", bad), ("OUT", good)])
    out = pipeline.normalize(text)
    assert "reconcile" in m.seen_tools[0]
    assert out.statement.ending_balance == good["ending_balance"]
    assert out.attempts == 2 and out.engine == "model"
    assert "off by 500.00" in m.seen_text[-1]  # the retry prompt carried the discrepancy


def test_agent_asks_after_three_failures(use_model, text, good):
    bad = dict(good, ending_balance=good["ending_balance"] + 500)
    use_model([("OUT", bad)] * 3 + [("OUT", {"question": "Is your ending balance $252,489.71?"})])
    out = pipeline.normalize(text)
    assert out.attempts == 3
    assert out.question == "Is your ending balance $252,489.71?"


def test_explanation_with_invented_number_is_replaced(use_model, good):
    from advisor_audit.schema import Statement

    findings = pipeline.findings_for(Statement.model_validate(good), "balanced")
    use_model([("OUT", {
        "fees_summary": "You paid $3,674 in fees. You'd save $99,999 by switching.",
        "questions": ["What does my 1.00% fee cover?", "Why SPGAX?", "Why a load?"],
        "comparison_summary": "Your account trailed the ETF mix.",
        "holdings_summary": "Mostly US stocks.",
    })])
    exp, notes = pipeline.explain(findings)
    assert "99,999" not in exp["fees_summary"]
    assert exp["fees_summary"] == rules.explain_rules(findings)["fees_summary"]
    assert exp["comparison_summary"] == "Your account trailed the ETF mix."
    assert len(exp["questions"]) == 3
    assert notes


def test_computed_values_are_sent_back_then_dropped(use_model, text, good):
    # The model works out a 5.75% load (1,150 / 20,000) that the statement never prints.
    computed = json.loads(json.dumps(good))
    computed["holdings"][0]["load_pct"] = 0.0575
    m = use_model([("OUT", computed)] * 3)
    out = pipeline.normalize(text)
    assert "load_pct = 0.0575" in m.seen_text[1]  # the retry named the unprinted value
    assert out.statement.holdings[0].load_pct is None
    assert out.discrepancies == [] and out.question is None


@pytest.mark.parametrize("name", ["statement.csv", "statement.xlsx", "statement.pdf", "statement_photo.jpg"])
def test_correct_statements_pass_the_printed_check(name):
    import shutil
    if name.endswith(".jpg") and not shutil.which("tesseract"):
        pytest.skip("no tesseract")
    t = redact(extract_bytes(name, (SAMPLES / name).read_bytes()))[0]
    assert pipeline.unprinted_values(rules.normalize_rules(t), t) == []
