"""Check the model endpoint before relying on it: python scripts/check_endpoint.py

1. Lists models and makes a plain chat call (OpenAI-compatible endpoints only).
2. Runs the real normalize step on the synthetic CSV sample (tool call + structured output + reconcile).
3. Runs the explain step and reports any invented numbers.
Only synthetic sample data is sent.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from advisor_audit import calc, pipeline  # noqa: E402
from advisor_audit.config import settings  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "statement.csv"


def main() -> None:
    print(f"provider={settings.model_provider}")
    if settings.model_provider == "akamai":
        from openai import OpenAI

        c = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=60)
        print("models:", [m.id for m in c.models.list().data])
        t = time.time()
        r = c.chat.completions.create(model=settings.openai_model, max_tokens=20,
                                      messages=[{"role": "user", "content": "Say OK."}])
        print(f"chat: {r.choices[0].message.content!r} ({time.time() - t:.1f}s)")

    settings.rules_fallback = False  # fail loudly instead of falling back
    text, _ = pipeline.read_upload(SAMPLE.name, SAMPLE.read_bytes())
    t = time.time()
    out = pipeline.normalize(text)
    print(f"normalize: {time.time() - t:.1f}s, attempts={out.attempts}, discrepancies={out.discrepancies}")
    assert out.statement, "no statement"
    print("reconcile ok:", calc.reconcile(out.statement)["ok"])

    from advisor_audit.agent import explain_with_model

    t = time.time()
    exp, problems = explain_with_model(pipeline.findings_for(out.statement, "balanced"))
    print(f"explain: {time.time() - t:.1f}s, problems={problems}")
    for q in exp["questions"]:
        print("  Q:", q)


if __name__ == "__main__":
    main()
