"""Extraction, redaction, parsing, the number check, and the web app."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from advisor_audit import calc
from advisor_audit.agent import allowed_numbers, invented_numbers
from advisor_audit.config import settings
from advisor_audit.extract import extract_bytes
from advisor_audit.redact import redact
from advisor_audit.rules import normalize_rules

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EXPECTED = json.loads((SAMPLES / "expected.json").read_text())
FORMATS = ["statement.csv", "statement.xlsx", "statement.pdf",
           pytest.param("statement_photo.jpg", marks=pytest.mark.skipif(not shutil.which("tesseract"), reason="no tesseract"))]
PII = ["Jordan", "Avery", "Dana", "Whitfield", "4821", "Maple", "64111", "Kansas City"]


@pytest.fixture(autouse=True)
def rules_mode(monkeypatch):
    monkeypatch.setattr(settings, "model_provider", "rules")


@pytest.mark.parametrize("name", FORMATS)
def test_every_format_parses_to_the_same_statement(name):
    text, _ = redact(extract_bytes(name, (SAMPLES / name).read_bytes()))
    for word in PII:
        assert word not in text, f"{word} leaked from {name}"
    s = normalize_rules(text)
    assert calc.reconcile(s)["ok"]
    assert s.starting_balance == EXPECTED["starting_balance"]
    assert s.ending_balance == EXPECTED["ending_balance"]
    assert s.market_change == EXPECTED["market_change"]
    assert s.firm == "Summit Wealth Partners"
    assert [(h.ticker, h.value, h.expense_ratio) for h in s.holdings] == \
        [(h["ticker"], h["value"], h["expense_ratio"]) for h in EXPECTED["holdings"]]
    assert sorted((str(f.date), f.kind, f.amount) for f in s.fees) == \
        sorted((f["date"], f["kind"], f["amount"]) for f in EXPECTED["fees"])
    assert sorted((str(t.date), t.type, t.amount) for t in s.transactions) == \
        sorted((t["date"], t["type"], t["amount"]) for t in EXPECTED["transactions"])
    assert [p.value for p in s.balance_history] == [p["value"] for p in EXPECTED["balance_history"]]


def test_redact_keeps_numbers_and_tickers():
    text = ("Account holder: Pat Q. Lee\nSSN 123-45-6789\nAcct #: 5555-1234-99\n"
            "Email pat@example.com, phone (816) 555-0142\n42 Oak Hill Rd, Suite 5\nOmaha, NE 68102\n"
            "SPGAX Summit Premier Growth Fund A 1,083.309 41.87 45,358.15 1.25%\n03/12/2025 Buy SPGAX 20,000.00")
    out, counts = redact(text)
    for s in ["Pat", "123-45-6789", "5555-1234", "pat@example.com", "555-0142", "Oak Hill", "Omaha"]:
        assert s not in out
    for s in ["SPGAX", "45,358.15", "1.25%", "03/12/2025", "20,000.00", "1,083.309"]:
        assert s in out
    assert counts["ssn"] == 1


def test_number_check_catches_invented_numbers():
    findings = {"performance": {"fees_paid_total": 3674.35, "advisory_fee_rate_annual": 0.01001, "return": -0.02081}}
    allowed = allowed_numbers(findings)
    assert invented_numbers("You paid $3,674 in fees, about 1.00% a year; return -2.1%.", allowed) == []
    assert invented_numbers("You paid $3,674.35 over 10 years in 2025.", allowed) == []
    assert invented_numbers("You could save $12,500 a year.", allowed) == ["$12,500"]


def test_model_unreachable_falls_back_to_rules(monkeypatch):
    from advisor_audit import pipeline

    monkeypatch.setattr(settings, "model_provider", "akamai")
    monkeypatch.setattr(settings, "openai_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(settings, "model_timeout_seconds", 2)
    text, _ = pipeline.read_upload("s.csv", (SAMPLES / "statement.csv").read_bytes())
    out = pipeline.normalize(text)
    assert out.engine == "rules" and out.statement is not None
    assert out.notes


# ---------------------------------------------------------------- web app

@pytest.fixture
def client():
    from advisor_audit.app import app
    return TestClient(app)


def _upload(client, name="statement.csv", data=None, profile="balanced"):
    data = data if data is not None else (SAMPLES / name).read_bytes()
    return client.post("/api/analyze", files={"file": (name, data)}, data={"risk_profile": profile})


def test_analyze_profile_and_delete(client):
    r = _upload(client)
    assert r.status_code == 200
    res = r.json()["results"]
    assert res["performance"]["fees_paid_total"] == 3674.35
    assert len(res["explanation"]["questions"]) == 3
    assert {f["kind"] for f in res["flags"]} >= {"expense_ratio", "sales_load", "proprietary_fund"}
    assert res["comparison"]["available"]

    growth = client.post("/api/profile", data={"risk_profile": "growth"}).json()["results"]
    assert growth["benchmark"]["mix"] == {"VTI": 0.9, "BND": 0.1}

    assert client.delete("/api/session").json() == {"deleted": True}
    assert client.post("/api/profile", data={"risk_profile": "growth"}).status_code == 409


def test_clarifying_question_when_numbers_dont_add_up(client):
    bad = (SAMPLES / "statement.csv").read_text().replace("Ending balance,", "Ending balance,1")
    j = _upload(client, data=bad.encode()).json()
    assert j["status"] == "needs_clarification"
    assert "didn't add up" in j["question"]
    j = client.post("/api/clarify", data={"answer": "I'm not sure"}).json()
    assert j["status"] == "done"
    assert any("didn't pass" in w for w in j["results"]["warnings"])


def test_rejects_bad_input(client):
    assert _upload(client, name="x.exe", data=b"MZ").status_code == 400
    assert _upload(client, profile="yolo").status_code == 400
