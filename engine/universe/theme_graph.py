"""Load and validate the theme graph.

The YAML is hand-authored, so symbols in it are hypotheses. `validate_tickers`
probes each one against the provider and reports what does not resolve, rather
than letting a typo silently shrink a theme basket and skew its index.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd
import yaml

from engine.config import settings


@dataclass
class ThemeNode:
    node_id: str
    name: str
    leg: str                      # GLOBAL | INDIA
    tickers: list[str]
    rationale: str = ""
    # Share of the company that the theme actually drives, 0-1. Defaults to 1.0,
    # which is the claim "this is a pure play" -- true for CG Power in the
    # data-centre power chain and plainly false for TCS in quantum. Set it on
    # the node where the whole node is an adjacency, or per ticker where members
    # differ. These are hand-authored judgements, not measured revenue shares.
    exposures: dict[str, float] = field(default_factory=dict)

    def exposure(self, ticker: str) -> float:
        return self.exposures.get(ticker, 1.0)


@dataclass
class Theme:
    theme_id: str
    name: str
    tier: int
    status: str
    description: str
    nodes: list[ThemeNode] = field(default_factory=list)

    @property
    def global_tickers(self) -> list[str]:
        return [t for n in self.nodes if n.leg == "GLOBAL" for t in n.tickers]

    @property
    def india_tickers(self) -> list[str]:
        return [t for n in self.nodes if n.leg == "INDIA" for t in n.tickers]

    @property
    def india_exposures(self) -> dict[str, float]:
        """Exposure to THIS theme per ticker.

        A company can sit in two nodes of one theme -- a transformer maker is in
        the data-centre power chain and in interconnect -- without being twice
        as exposed to the theme. So nodes are combined by taking the strongest
        claim, not by adding them up.
        """
        out: dict[str, float] = {}
        for node in self.nodes:
            if node.leg != "INDIA":
                continue
            for ticker in node.tickers:
                out[ticker] = max(out.get(ticker, 0.0), node.exposure(ticker))
        return out


@dataclass
class ThemeGraph:
    themes: list[Theme]
    macro_series: dict[str, str]
    benchmarks: dict[str, str]

    def theme(self, theme_id: str) -> Theme:
        for t in self.themes:
            if t.theme_id == theme_id:
                return t
        raise KeyError(f"unknown theme: {theme_id}")

    def all_tickers(self, include_macro: bool = True) -> list[str]:
        """Every distinct symbol the engine needs, order-stable."""
        seen: dict[str, None] = {}
        for theme in self.themes:
            for node in theme.nodes:
                for ticker in node.tickers:
                    seen.setdefault(ticker, None)
        if include_macro:
            for symbol in {**self.macro_series, **self.benchmarks}.values():
                seen.setdefault(symbol, None)
        return list(seen)

    def india_universe(self) -> list[str]:
        seen: dict[str, None] = {}
        for theme in self.themes:
            for ticker in theme.india_tickers:
                seen.setdefault(ticker, None)
        return list(seen)

    def membership_frame(self) -> pd.DataFrame:
        """Long frame of (theme_id, node_id, leg, ticker) for persistence."""
        rows = [
            {
                "theme_id": theme.theme_id,
                "node_id": node.node_id,
                "node_name": node.name,
                "leg": node.leg,
                "ticker": ticker,
                "rationale": node.rationale,
            }
            for theme in self.themes
            for node in theme.nodes
            for ticker in node.tickers
        ]
        return pd.DataFrame(rows)


def _yahoo_suffix(ticker: str) -> str:
    """India-leg symbols are written bare in YAML; append the NSE suffix."""
    if "." in ticker or "=" in ticker or ticker.startswith("^"):
        return ticker
    return f"{ticker}.NS"


def _parse_node_tickers(node: dict, leg: str) -> tuple[list[str], dict[str, float]]:
    """Ticker list plus per-ticker exposure.

    Two spellings, so existing config keeps working unchanged:

        tickers: [CGPOWER, SIEMENS]                 # pure plays, exposure 1.0
        exposure: 0.05                              # node-wide default
        tickers:
          - TCS
          - {ticker: LTTS, exposure: 0.15}          # per-ticker override
    """
    default = float(node.get("exposure", 1.0))
    tickers: list[str] = []
    exposures: dict[str, float] = {}

    for item in node.get("tickers", []) or []:
        if isinstance(item, dict):
            raw = item.get("ticker")
            if not raw:
                raise ValueError(f"node {node.get('id')}: ticker entry without a ticker")
            weight = float(item.get("exposure", default))
        else:
            raw, weight = item, default

        if not 0.0 < weight <= 1.0:
            raise ValueError(
                f"node {node.get('id')}: exposure for {raw} is {weight}; "
                "it is a share of the company and must be in (0, 1]"
            )

        ticker = _yahoo_suffix(raw) if leg == "INDIA" else raw
        tickers.append(ticker)
        exposures[ticker] = weight

    return tickers, exposures


def load_theme_graph(path=None) -> ThemeGraph:
    """Parse config/themes.yaml into a ThemeGraph."""
    path = path or settings.THEMES_FILE
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    themes: list[Theme] = []
    for entry in raw.get("themes", []):
        nodes: list[ThemeNode] = []
        for leg, key in (("GLOBAL", "global_nodes"), ("INDIA", "india_nodes")):
            for node in entry.get(key, []) or []:
                tickers, exposures = _parse_node_tickers(node, leg)
                nodes.append(
                    ThemeNode(
                        node_id=node["id"],
                        name=node.get("name", node["id"]),
                        leg=leg,
                        tickers=tickers,
                        rationale=(node.get("rationale") or "").strip(),
                        exposures=exposures,
                    )
                )
        themes.append(
            Theme(
                theme_id=entry["id"],
                name=entry["name"],
                tier=int(entry.get("tier", 2)),
                status=entry.get("status", "ACTIVE"),
                description=(entry.get("description") or "").strip(),
                nodes=nodes,
            )
        )

    return ThemeGraph(
        themes=themes,
        macro_series=raw.get("macro_series", {}) or {},
        benchmarks=raw.get("benchmarks", {}) or {},
    )


def validate_tickers(
    graph: ThemeGraph, provider, lookback_days: int = 30
) -> pd.DataFrame:
    """Probe every symbol; report which resolve to real price history.

    Returns one row per ticker with status RESOLVED or UNRESOLVED, plus the
    themes it appears in, so a bad symbol can be traced back to its config entry.
    """
    tickers = graph.all_tickers()
    start = dt.date.today() - dt.timedelta(days=lookback_days)
    prices = provider.fetch_ohlcv(tickers, start=start)

    if prices.empty:
        resolved: dict[str, int] = {}
        last_seen: dict[str, object] = {}
    else:
        counts = prices.groupby("ticker").size()
        resolved = counts.to_dict()
        last_seen = prices.groupby("ticker")["date"].max().to_dict()

    membership = graph.membership_frame()
    where = (
        membership.groupby("ticker")["theme_id"]
        .apply(lambda s: ",".join(sorted(set(s))))
        .to_dict()
    )

    rows = []
    for ticker in tickers:
        bars = resolved.get(ticker, 0)
        rows.append({
            "ticker": ticker,
            "status": "RESOLVED" if bars > 0 else "UNRESOLVED",
            "bars": bars,
            "last_date": last_seen.get(ticker),
            "themes": where.get(ticker, "macro/benchmark"),
        })

    return pd.DataFrame(rows).sort_values(["status", "ticker"]).reset_index(drop=True)
