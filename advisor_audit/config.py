"""Settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from .env into the environment, without overriding what's already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # "akamai" (default): an OpenAI-compatible endpoint on Akamai, via Strands' OpenAIModel.
    # "ollama": a self-hosted Ollama on our own Akamai Linode (GPU recommended).
    # "rules": no model. A deterministic parser for simple labeled statements and a
    #          template explainer; used by tests and as the fallback when the model fails.
    model_provider: str = field(default_factory=lambda: _env("MODEL_PROVIDER", "akamai"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://ws.akamai-workshops.com/v1"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "hackathon-chat"))
    ollama_host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "qwen2.5:14b-instruct"))
    max_tokens: int = field(default_factory=lambda: int(_env("MAX_TOKENS", "4096")))
    model_timeout_seconds: float = field(default_factory=lambda: float(_env("MODEL_TIMEOUT_SECONDS", "90")))
    # The model sees at most this many characters of redacted statement text.
    max_statement_chars: int = field(default_factory=lambda: int(_env("MAX_STATEMENT_CHARS", "40000")))
    # Try the built-in parser first and skip the model when its result fully checks out
    # (seconds instead of minutes). Set PARSER_FIRST=0 to always use the model.
    parser_first: bool = field(default_factory=lambda: _env("PARSER_FIRST", "1") == "1")
    # If the model fails, fall back to the rules normalizer/explainer.
    rules_fallback: bool = field(default_factory=lambda: _env("RULES_FALLBACK", "1") == "1")

    prices_csv: str = field(default_factory=lambda: _env("PRICES_CSV", str(ROOT / "data" / "etf_prices.csv")))
    stock_etf: str = field(default_factory=lambda: _env("STOCK_ETF", "VTI"))
    bond_etf: str = field(default_factory=lambda: _env("BOND_ETF", "BND"))
    # Expense ratios of the benchmark ETFs, used when projecting their cost.
    stock_etf_er: float = 0.0003
    bond_etf_er: float = 0.0003

    # Growth rate assumed when projecting fees forward.
    projection_return: float = field(default_factory=lambda: float(_env("PROJECTION_RETURN", "0.05")))

    session_ttl_seconds: int = field(default_factory=lambda: int(_env("SESSION_TTL_SECONDS", "1800")))
    max_upload_bytes: int = 15 * 1024 * 1024
    max_normalize_attempts: int = 3


settings = Settings()

BENCHMARK_MIXES = {
    "conservative": {"stock": 0.30, "bond": 0.70},
    "balanced": {"stock": 0.60, "bond": 0.40},
    "growth": {"stock": 0.90, "bond": 0.10},
}
