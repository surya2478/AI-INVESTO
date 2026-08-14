"""Central configuration.

Paths and tunables live here so the pipeline, API and tests all agree. Anything
a user might reasonably want to change is overridable via environment variable
or .env, with defaults chosen for a single-user local install.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env_path(key: str, default: Path) -> Path:
    raw = os.getenv(key)
    return Path(raw).expanduser() if raw else default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    ROOT: Path = ROOT
    DATA_DIR: Path = field(default_factory=lambda: _env_path("INVESTO_DATA_DIR", ROOT / "data"))
    CONFIG_DIR: Path = field(default_factory=lambda: ROOT / "config")
    REPORT_DIR: Path = field(default_factory=lambda: ROOT / "reports")

    # Providers ------------------------------------------------------------
    # Deliberately conservative: NSE blocks aggressive clients, and a nightly
    # job has no reason to hurry.
    REQUEST_TIMEOUT: float = field(default_factory=lambda: _env_float("INVESTO_TIMEOUT", 20.0))
    RATE_LIMIT_SLEEP: float = field(default_factory=lambda: _env_float("INVESTO_RATE_SLEEP", 0.35))
    MAX_RETRIES: int = 3
    BATCH_SIZE: int = 40                     # tickers per yfinance download call

    # History --------------------------------------------------------------
    HISTORY_START: str = "2012-01-01"        # pre-dates the 2015 backtest window

    # LLM extraction -------------------------------------------------------
    # Routed through OpenRouter, so the model is a config value rather than a
    # code change. Accuracy per model is measured by
    # scripts/validate_pdf_extraction.py before any model is trusted.
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_MODEL: str = field(
        default_factory=lambda: os.getenv("INVESTO_LLM_MODEL", "anthropic/claude-sonnet-5")
    )
    # 'native' lets the model read the PDF directly -- best for the ruled tables
    # in a results statement. OpenRouter DEFAULTS to mistral-ocr at $2/1,000
    # pages, which is for scans; BSE statements are digital text, so paying for
    # OCR would be waste. 'cloudflare-ai' is free but flattens table structure.
    PDF_ENGINE: str = field(
        default_factory=lambda: os.getenv("INVESTO_PDF_ENGINE", "native")
    )

    # Universe filters -----------------------------------------------------
    # A multibagger has to start small; a name already at Rs 1L cr cannot 10x.
    MIN_MARKET_CAP_CR: float = 300.0
    MAX_MARKET_CAP_CR: float = 75_000.0
    MIN_DAILY_TURNOVER_CR: float = 0.5       # liquidity floor, tradeable size

    @property
    def DB_PATH(self) -> Path:
        return self.DATA_DIR / "db" / "investo.duckdb"

    @property
    def RAW_DIR(self) -> Path:
        return self.DATA_DIR / "raw"

    @property
    def THEMES_FILE(self) -> Path:
        return self.CONFIG_DIR / "themes.yaml"

    @property
    def UNIVERSE_FILE(self) -> Path:
        return self.CONFIG_DIR / "universe.yaml"


settings = Settings()

# --------------------------------------------------------------------------
# G.E.M. score pillar weights. These are the Stage-2 defaults and are expected
# to change at Stage 3, when the backtest tells us what actually predicts a
# multibagger. Treat any weight here as a hypothesis, not a conclusion.
GEM_WEIGHTS: dict[str, float] = {
    "t_score": 0.20,   # Theme tailwind
    "g_score": 0.25,   # Growth inflection  -- strongest prior
    "q_score": 0.20,   # Business quality
    "d_score": 0.15,   # Discovery / under-ownership
    "v_score": 0.10,   # Valuation sanity   -- a brake, not a value screen
    "m_score": 0.10,   # Price trend confluence
}

assert abs(sum(GEM_WEIGHTS.values()) - 1.0) < 1e-9, "GEM weights must sum to 1"
