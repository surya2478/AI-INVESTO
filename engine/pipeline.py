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


# ------------------------------------------------------------------ universe
@app.command()
def universe() -> None:
    """Sync the investable universe from NSE: symbols, indices, surveillance, flows.

    Run before `ingest prices` -- this is what defines the universe the price
    fetcher then populates.
    """
    from engine.providers.nse_provider import NSEProvider
    from engine.universe import builder

    run_id = _run_id()
    provider = NSEProvider()
    con = db.connect()

    try:
        started = dt.datetime.now()
        console.print("fetching NSE symbol master...")
        symbols = builder.sync_symbol_master(con, provider)
        console.print(f"  [green]{symbols:,}[/green] EQ-series listings")

        console.print("fetching index constituents...")
        counts = builder.sync_index_membership(con, provider)
        table = Table(title="Index membership")
        table.add_column("index", style="cyan")
        table.add_column("members", justify="right")
        for index_name, n in counts.items():
            table.add_row(index_name, f"{n:,}")
        console.print(table)

        levels = builder.sync_index_levels(con, provider)
        flagged = builder.sync_surveillance(con, provider)
        flow_rows = builder.sync_flows(con, provider)

        console.print(f"index levels snapshotted: [green]{levels}[/green]")
        console.print(f"ASM-flagged names: [yellow]{flagged}[/yellow] "
                      "(hard reject in the quality gates)")
        console.print(f"FII/DII flow rows: [green]{flow_rows}[/green]")

        investable = builder.investable_universe(con)
        console.print(f"\n[bold]investable universe: {len(investable):,} names[/bold]")

        db.log_ingest(con, run_id, "universe", None, "OK", symbols,
                      f"{len(counts)} indices", started)
    finally:
        con.close()


# -------------------------------------------------------------------- ingest
ingest_app = typer.Typer(help="Data ingestion stages")
app.add_typer(ingest_app, name="ingest")


@ingest_app.command("prices")
def ingest_prices(
    start: str = typer.Option(settings.HISTORY_START, help="History start date"),
    only_resolved: bool = typer.Option(
        True, help="Skip symbols that failed validation"
    ),
    themes_only: bool = typer.Option(
        False, help="Restrict to theme-graph symbols instead of the full universe"
    ),
) -> None:
    """Pull daily bars for the investable universe plus all theme/macro symbols."""
    run_id = _run_id()
    graph = load_theme_graph()
    provider = YFinanceProvider()
    con = db.connect()

    try:
        tickers = graph.all_tickers()

        if not themes_only:
            from engine.universe.builder import investable_universe

            # Order-stable union: theme and macro symbols first, then the rest
            # of the NSE universe the screener needs in order to find names
            # outside the hand-authored theme lists.
            seen = dict.fromkeys(tickers)
            for ticker in investable_universe(con):
                seen.setdefault(ticker, None)
            tickers = list(seen)

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


@ingest_app.command("fundamentals")
def ingest_fundamentals(
    limit: int = typer.Option(0, help="Cap the number of symbols (0 = all)"),
    max_filings: int = typer.Option(12, help="Filings per company (~3 years of quarters)"),
    themes_only: bool = typer.Option(
        True, help="Restrict to Indian theme-graph names rather than the full universe"
    ),
) -> None:
    """Pull quarterly financials with true filing dates from NSE XBRL."""
    from engine.fundamentals import coverage_report, pit_selftest, sync_fundamentals
    from engine.universe.builder import investable_universe

    run_id = _run_id()
    graph = load_theme_graph()
    con = db.connect()

    try:
        if themes_only:
            tickers = graph.india_universe()
        else:
            tickers = investable_universe(con)
        symbols = [t.removesuffix(".NS") for t in tickers if t.endswith(".NS")]
        if limit:
            symbols = symbols[:limit]

        console.print(f"fetching financials for [bold]{len(symbols)}[/bold] companies "
                      f"({max_filings} filings each)...")
        started = dt.datetime.now()
        result = sync_fundamentals(con, symbols, max_filings=max_filings)

        console.print(f"[green]{result['written']:,} facts[/green] from "
                      f"{len(result['ok'])} companies")
        if result["empty"]:
            console.print(f"[yellow]{len(result['empty'])} returned nothing[/yellow]: "
                          + ", ".join(result["empty"][:10]))
        if result["failed"]:
            console.print(f"[red]{len(result['failed'])} failed[/red]")
            for symbol, reason in result["failed"][:10]:
                console.print(f"  {symbol:<14} {reason}")

        cov = coverage_report(con)
        table = Table(title="Fundamentals coverage")
        for col in ("metric", "companies", "facts", "earliest", "latest"):
            table.add_column(col, justify="right" if col != "metric" else "left")
        for _, r in cov.head(20).iterrows():
            table.add_row(str(r.metric), f"{int(r.companies):,}", f"{int(r.facts):,}",
                          str(r.earliest)[:10], str(r.latest)[:10])
        console.print(table)

        check = pit_selftest(con)
        style = "green" if check["passed"] else "red"
        console.print(
            f"\n[{style}]point-in-time guard: "
            f"{'PASS' if check['passed'] else 'FAIL'}[/{style}] — "
            f"{check['facts_filed_after']:,} facts were filed after "
            f"{check['as_of']}, and {check['leaked']} leaked into the as-of view"
        )

        db.log_ingest(con, run_id, "fundamentals", None, "OK", result["written"],
                      f"{len(result['ok'])} companies", started)
    finally:
        con.close()


# ------------------------------------------------------------------ coverage
@app.command()
def coverage() -> None:
    """Report what data actually landed -- the honest view before trusting scores."""
    con = db.connect(read_only=True)
    try:
        # `registered` counts every symbol in the master; `priced` counts those
        # we actually hold bars for. Collapsing the two would overstate coverage,
        # since the NSE master lists ~2,100 names we deliberately do not fetch.
        summary = con.execute("""
            SELECT s.country,
                   count(DISTINCT s.security_id)                          AS registered,
                   count(DISTINCT o.security_id)                          AS priced,
                   count(o.date)                                          AS bars,
                   min(o.date)                                            AS first_bar,
                   max(o.date)                                            AS last_bar
              FROM securities s
              LEFT JOIN ohlcv o ON o.security_id = s.security_id
             GROUP BY s.country
             ORDER BY priced DESC
        """).df()

        table = Table(title="Coverage by country")
        for col in ("country", "registered", "priced", "bars", "first bar", "last bar"):
            table.add_column(col, justify="right" if col != "country" else "left")
        for _, r in summary.iterrows():
            table.add_row(
                str(r.country), f"{int(r.registered):,}", f"{int(r.priced):,}",
                f"{int(r.bars):,}", str(r.first_bar)[:10], str(r.last_bar)[:10],
            )
        console.print(table)

        gap = con.execute("""
            SELECT count(*) AS n
              FROM index_membership im
              LEFT JOIN ohlcv o ON o.security_id = im.security_id
             WHERE im.index_name = 'NIFTY TOTAL MARKET'
               AND im.to_date IS NULL
               AND o.security_id IS NULL
        """).fetchone()[0]
        if gap:
            console.print(f"[yellow]{gap} index members have no price history[/yellow] "
                          "— investigate before scoring")

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
