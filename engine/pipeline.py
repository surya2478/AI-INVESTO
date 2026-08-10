"""AI-Investo pipeline CLI.

    investo init                 create the database
    investo validate             probe every theme ticker, report what resolves
    investo ingest prices        pull daily bars for the whole universe
    investo coverage             what landed, what is missing

Every stage is idempotent and resumable -- a failed night is fixed by rerunning,
not by unpicking partial state.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from engine.config import settings
from engine.providers.yfinance_provider import YFinanceProvider, classify_ticker
from engine.storage import db
from engine.universe.theme_graph import load_theme_graph, validate_tickers

app = typer.Typer(add_completion=False, help="AI-Investo analytics pipeline")
console = Console()

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
)


def _run_id() -> str:
    return f"{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------- init
@app.command()
def init() -> None:
    """Create the DuckDB database and apply the schema."""
    path = db.init_db()
    console.print(f"[green]database ready[/green] {path}")


# ------------------------------------------------------------------ validate
@app.command()
def validate(
    write_report: bool = typer.Option(True, help="Write reports/ticker_validation.csv"),
) -> None:
    """Probe every symbol in the theme graph and report what actually resolves.

    Run this after editing config/themes.yaml. Unresolved symbols are config
    bugs -- a mistyped ticker silently shrinks a theme basket and biases its index.
    """
    graph = load_theme_graph()
    provider = YFinanceProvider()

    total = len(graph.all_tickers())
    console.print(f"probing [bold]{total}[/bold] symbols across "
                  f"{len(graph.themes)} themes...")

    report = validate_tickers(graph, provider)
    resolved = report[report.status == "RESOLVED"]
    unresolved = report[report.status == "UNRESOLVED"]

    table = Table(title="Ticker validation")
    table.add_column("theme", style="cyan")
    table.add_column("resolved", justify="right", style="green")
    table.add_column("unresolved", justify="right", style="red")
    table.add_column("india leg", justify="right")

    bad = set(unresolved.ticker)
    for theme in graph.themes:
        g, i = theme.global_tickers, theme.india_tickers
        every = g + i
        table.add_row(
            f"{theme.theme_id} (T{theme.tier})",
            str(sum(t not in bad for t in every)),
            str(sum(t in bad for t in every)),
            f"{sum(t not in bad for t in i)}/{len(i)}",
        )
    console.print(table)

    if not unresolved.empty:
        console.print(f"\n[red]{len(unresolved)} unresolved[/red] "
                      "(fix or remove these in config/themes.yaml):")
        for _, row in unresolved.iterrows():
            console.print(f"  [red]x[/red] {row.ticker:<16} used by: {row.themes}")

    console.print(f"\n[green]{len(resolved)}/{total} resolved[/green]")

    if write_report:
        settings.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = settings.REPORT_DIR / "ticker_validation.csv"
        report.to_csv(out, index=False)
        console.print(f"report: {out}")


# -------------------------------------------------------------------- ingest
ingest_app = typer.Typer(help="Data ingestion stages")
app.add_typer(ingest_app, name="ingest")


@ingest_app.command("prices")
def ingest_prices(
    start: str = typer.Option(settings.HISTORY_START, help="History start date"),
    only_resolved: bool = typer.Option(
        True, help="Skip symbols that failed validation"
    ),
) -> None:
    """Pull daily bars for every symbol in the theme graph."""
    run_id = _run_id()
    graph = load_theme_graph()
    provider = YFinanceProvider()
    con = db.connect()

    try:
        tickers = graph.all_tickers()

        if only_resolved:
            report_path = settings.REPORT_DIR / "ticker_validation.csv"
            if report_path.exists():
                report = pd.read_csv(report_path)
                bad = set(report.loc[report.status == "UNRESOLVED", "ticker"])
                skipped = [t for t in tickers if t in bad]
                tickers = [t for t in tickers if t not in bad]
                if skipped:
                    console.print(f"[yellow]skipping {len(skipped)} unresolved "
                                  "symbols[/yellow] (run `validate` to refresh)")
            else:
                console.print("[yellow]no validation report found; "
                              "ingesting all symbols[/yellow]")

        console.print(f"fetching [bold]{len(tickers)}[/bold] symbols from {start}")
        started = dt.datetime.now()
        prices = provider.fetch_ohlcv(tickers, start=start)

        if prices.empty:
            console.print("[red]no price data returned[/red]")
            db.log_ingest(con, run_id, "prices", None, "EMPTY", 0,
                          "provider returned nothing", started)
            return

        # Register securities before prices, so security_id exists to join on.
        seen = prices["ticker"].drop_duplicates()
        profile_rows = []
        for ticker in seen:
            exchange, country, currency = classify_ticker(ticker)
            profile_rows.append({
                "ticker": ticker, "exchange_symbol": ticker.split(".")[0],
                "isin": None, "name": None, "exchange": exchange,
                "country": country, "currency": currency, "sector": None,
                "industry": None, "market_cap": None, "listing_date": None,
                "delisted_date": None, "is_active": True, "source": provider.name,
            })
        db.upsert_securities(con, pd.DataFrame(profile_rows))

        id_map = db.security_map(con)
        prices = prices.assign(
            security_id=prices["ticker"].map(id_map), source=provider.name
        ).dropna(subset=["security_id"])
        prices["security_id"] = prices["security_id"].astype("int64")

        written = db.upsert_ohlcv(
            con,
            prices[["security_id", "date", "open", "high", "low", "close",
                    "adj_close", "volume", "source"]],
        )
        db.log_ingest(con, run_id, "prices", None, "OK", written,
                      f"{seen.size} symbols", started)

        console.print(f"[green]wrote {written:,} bars[/green] for "
                      f"{seen.size} symbols  (run {run_id})")
    finally:
        con.close()


# ------------------------------------------------------------------ coverage
@app.command()
def coverage() -> None:
    """Report what data actually landed -- the honest view before trusting scores."""
    con = db.connect(read_only=True)
    try:
        summary = con.execute("""
            SELECT s.country,
                   count(DISTINCT s.security_id)             AS securities,
                   count(o.date)                             AS bars,
                   min(o.date)                               AS first_bar,
                   max(o.date)                               AS last_bar
              FROM securities s
              LEFT JOIN ohlcv o ON o.security_id = s.security_id
             GROUP BY s.country
             ORDER BY securities DESC
        """).df()

        table = Table(title="Coverage by country")
        for col in ("country", "securities", "bars", "first bar", "last bar"):
            table.add_column(col)
        for _, r in summary.iterrows():
            table.add_row(
                str(r.country), f"{int(r.securities):,}", f"{int(r.bars):,}",
                str(r.first_bar), str(r.last_bar),
            )
        console.print(table)

        stale = con.execute("""
            WITH last_bar AS (
                SELECT security_id, max(date) AS d FROM ohlcv GROUP BY security_id
            )
            SELECT s.ticker, l.d AS last_bar
              FROM last_bar l
              JOIN securities s ON s.security_id = l.security_id
             WHERE l.d < (SELECT max(date) FROM ohlcv) - INTERVAL 10 DAY
             ORDER BY l.d
             LIMIT 20
        """).df()

        if not stale.empty:
            console.print(f"\n[yellow]{len(stale)} symbols stale by >10 days"
                          "[/yellow] (possible delisting or symbol change):")
            for _, r in stale.iterrows():
                console.print(f"  {r.ticker:<16} last bar {r.last_bar}")

        thin = con.execute("""
            SELECT s.ticker, count(*) AS bars
              FROM ohlcv o JOIN securities s ON s.security_id = o.security_id
             GROUP BY s.ticker HAVING count(*) < 250
             ORDER BY bars
        """).df()
        if not thin.empty:
            console.print(f"\n[yellow]{len(thin)} symbols with <1yr history"
                          "[/yellow] (recent listings -- expected for new-economy names)")
    finally:
        con.close()


if __name__ == "__main__":
    app()
