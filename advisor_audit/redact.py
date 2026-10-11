"""Strip personal identifiers before any text reaches the model.

Removes SSNs, account numbers, emails, phone numbers, street addresses,
and names on labeled lines (e.g. "Account holder: ..."). Leaves dollar
amounts, dates, tickers, and fund names alone, since the model needs them.
"""

from __future__ import annotations

import re

_LABEL = r"(?:account\s*(?:holder|owner|name)|client(?:\s*name)?|investor|prepared\s*for|owner|name|dear|attn|attention|beneficiary|advisor|adviser|financial\s*advisor|your\s*advisor|rep(?:resentative)?|address|mailing\s*address)"

PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    ("ssn_masked", re.compile(r"\b(?:x{3}|\*{3})-(?:x{2}|\*{2})-\d{4}\b", re.I), "[SSN]"),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    ("phone", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"), "[PHONE]"),
    # Labeled account numbers, including masked ones like XXXX-1234 or ****1234.
    ("account", re.compile(
        r"(?i)\b(acc(?:oun)?t\.?\s*(?:no\.?|number|num|#)?\s*[:#]?\s*)[A-Z0-9*xX][A-Z0-9*xX\- ]{3,}[0-9]"), r"\1[ACCOUNT]"),
    ("masked_number", re.compile(r"(?i)(?:[x*]{3,}[\s-]?)+\d{3,}"), "[ACCOUNT]"),
    ("long_number", re.compile(r"(?<![\d.,$])\d{9,}(?![\d.,])"), "[NUMBER]"),
    ("dashed_number", re.compile(r"\b\d{4}-\d{4}(?:-\d{2,4})?\b"), "[ACCOUNT]"),
    ("street", re.compile(
        r"(?i)\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][a-z0-9]+\s+){1,4}"
        r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|ln|lane|dr|drive|ct|court|way|pl|place|ter|terrace|pkwy|parkway|cir|circle|hwy|highway)\b\.?"
        r"(?:,?\s*(?:apt|suite|ste|unit|#)\.?\s*\w+)?"), "[ADDRESS]"),
    ("po_box", re.compile(r"(?i)\bp\.?\s*o\.?\s*box\s+\d+"), "[ADDRESS]"),
    ("city_state_zip", re.compile(r"\b[A-Z][a-zA-Z.]+(?:\s[A-Z][a-zA-Z.]+)*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"), "[ADDRESS]"),
    ("labeled_name", re.compile(rf"(?im)^(\s*{_LABEL}\s*[:,]?\s+)(?!\[)([^\n,|]+?)(\s*(?:[,|]|$))"), r"\1[NAME]\3"),
    ("labeled_name_csv", re.compile(rf"(?im)^(\s*\"?{_LABEL}\"?\s*,\s*\"?)(?!\[)([^\n,\"]+)"), r"\1[NAME]"),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return (redacted text, count of redactions by kind)."""
    counts: dict[str, int] = {}
    for kind, pat, repl in PATTERNS:
        text, n = pat.subn(repl, text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts
