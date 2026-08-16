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
    max_filings: int = typer.Option(12, help="Quarterly filings per company (0 to skip)"),
    max_annual: int = typer.Option(10, help="Annual filings per company (0 to skip)"),
    themes_only: bool = typer.Option(
        True, help="Restrict to Indian theme-graph names rather than the full universe"
    ),
) -> None:
    """Pull quarterly and annual financials with true filing dates from NSE XBRL.

    Annual filings are what the score actually ranks on, and NSE serves them
    under a separate query, so this fetches both. Note the coverage ceiling:
    XBRL annual documents exist from roughly FY2018, and they carry the balance
    sheet and cash-flow statement only from FY2023.
    """
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
                      f"({max_filings} quarterly + {max_annual} annual each)...")
        started = dt.datetime.now()
        result = sync_fundamentals(con, symbols, max_filings=max_filings,
                                   max_annual=max_annual)

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


@ingest_app.command("filings")
def ingest_filings(
    months: int = typer.Option(30, help="How far back to walk result announcements"),
) -> None:
    """Record when results were published -- the filing_date PDFs cannot supply."""
    from engine.universe import builder

    run_id = _run_id()
    con = db.connect()
    try:
        started = dt.datetime.now()
        console.print("matching BSE scrip codes on ISIN...")
        matched = builder.sync_bse_identity(con)
        console.print(f"  [green]{matched:,}[/green] securities matched to BSE")

        end = dt.date.today() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=int(months * 30.44))
        console.print(f"walking result announcements {start} to {end}...")
        result = builder.sync_filing_dates(con, start, end)

        console.print(f"  fetched [bold]{result['fetched']:,}[/bold] announcements, "
                      f"[green]{result['stored']:,}[/green] filing events stored")
        if result["unparsed"]:
            console.print(f"  [yellow]{result['unparsed']:,} had no parseable period[/yellow] "
                          "(not quarterly results -- ignored rather than guessed)")

        summary = con.execute("""
            SELECT count(DISTINCT security_id) AS companies,
                   count(*)                    AS events,
                   min(period_end)             AS earliest,
                   max(period_end)             AS latest
              FROM filing_events
        """).df()
        if not summary.empty and int(summary.events.iloc[0]):
            r = summary.iloc[0]
            console.print(f"\n[bold]{int(r.events):,} filing events[/bold] across "
                          f"{int(r.companies):,} companies, "
                          f"periods {str(r.earliest)[:10]} to {str(r.latest)[:10]}")

        db.log_ingest(con, run_id, "filings", None, "OK", result["stored"],
                      f"{start}..{end}", started)
    finally:
        con.close()


@ingest_app.command("yahoo")
def ingest_yahoo(
    themes_only: bool = typer.Option(False, help="Theme companies only"),
    limit: int = typer.Option(0, help="Cap companies (0 = all)"),
) -> None:
    """Pull quarterly and annual financials from Yahoo — the primary screening source.

    Free and near-complete, but carries no filing date, so rows are stored with
    is_pit = FALSE and are excluded from as-of reads. Screening today: yes.
    Backtesting: never.
    """
    from engine.fundamentals import coverage_report, sync_yahoo_fundamentals
    from engine.universe.builder import investable_universe

    run_id = _run_id()
    con = db.connect()
    try:
        graph = load_theme_graph()
        if themes_only:
            tickers = graph.india_universe()
        else:
            seen = dict.fromkeys(graph.india_universe())
            for ticker in investable_universe(con):
                seen.setdefault(ticker, None)
            tickers = list(seen)
        if limit:
            tickers = tickers[:limit]

        console.print(f"fetching Yahoo financials for [bold]{len(tickers)}[/bold] companies...")
        started = dt.datetime.now()

        state = {"hit": 0}

        def show(index, total, ticker, rows):
            if rows:
                state["hit"] += 1
            if index % 25 == 0 or index == total:
                console.print(f"  {index:>4}/{total}  {state['hit']} with data")

        result = sync_yahoo_fundamentals(con, tickers, progress=show)

        console.print(f"\n[green]{result['written']:,} facts[/green] from "
                      f"{result['ok']} companies "
                      f"([yellow]{result['empty']} returned nothing[/yellow])")

        cov = coverage_report(con)
        table = Table(title="Fundamentals coverage (all sources)")
        for col in ("metric", "companies", "facts", "earliest", "latest"):
            table.add_column(col, justify="right" if col != "metric" else "left")
        for _, r in cov.head(14).iterrows():
            table.add_row(str(r.metric), f"{int(r.companies):,}", f"{int(r.facts):,}",
                          str(r.earliest)[:10], str(r.latest)[:10])
        console.print(table)

        db.log_ingest(con, run_id, "yahoo_fundamentals", None, "OK",
                      result["written"], f"{result['ok']} companies", started)
    finally:
        con.close()


@ingest_app.command("orders")
def ingest_orders(
    themes_only: bool = typer.Option(True, help="Theme companies only"),
    limit: int = typer.Option(0, help="Cap companies (0 = all)"),
) -> None:
    """Parse order wins and disclosed order book from NSE announcement text."""
    from engine.orders import book_to_sales, sync_orders
    from engine.universe.builder import investable_universe

    run_id = _run_id()
    con = db.connect()
    try:
        graph = load_theme_graph()
        tickers = graph.india_universe() if themes_only else investable_universe(con)
        if limit:
            tickers = tickers[:limit]

        console.print(f"parsing order announcements for [bold]{len(tickers)}[/bold] companies...")
        started = dt.datetime.now()
        state = {"hit": 0}

        def show(index, total, symbol, n):
            if n:
                state["hit"] += 1
            if index % 20 == 0 or index == total:
                console.print(f"  {index:>4}/{total}  {state['hit']} with events")

        result = sync_orders(con, tickers, progress=show)
        console.print(f"\n[green]{result['stored']:,} events[/green] stored · "
                      f"{result['with_book']} companies disclosed an order book")

        stale = con.execute("""
            SELECT count(DISTINCT security_id) FROM order_events
             WHERE kind = 'ORDER_BOOK' AND value_cr IS NOT NULL
               AND event_date < current_date - INTERVAL 450 DAY
               AND security_id NOT IN (
                   SELECT security_id FROM order_events
                    WHERE kind = 'ORDER_BOOK' AND value_cr IS NOT NULL
                      AND event_date >= current_date - INTERVAL 450 DAY)
        """).fetchone()[0]
        if stale:
            console.print(f"[yellow]{stale} companies have only stale order-book "
                          "disclosures (>15 months)[/yellow] — excluded, since a book "
                          "from years ago is not forward visibility")

        bts = book_to_sales(con)
        if not bts.empty:
            table = Table(title="Order book to sales — forward visibility")
            for col in ("ticker", "book (₹cr)", "revenue (₹cr)", "years of sales", "as of"):
                table.add_column(col, justify="right" if col != "ticker" else "left")
            for _, r in bts.head(14).iterrows():
                if pd.isna(r.book_to_sales):
                    continue
                tone = "green" if r.book_to_sales >= 2 else "yellow" if r.book_to_sales >= 1 else "white"
                table.add_row(str(r.ticker).replace(".NS", ""),
                              f"{r.order_book_cr:,.0f}",
                              f"{r.revenue_cr:,.0f}" if pd.notna(r.revenue_cr) else "—",
                              f"[{tone}]{r.book_to_sales:.1f}x[/{tone}]",
                              str(r.event_date)[:10])
            console.print(table)

        db.log_ingest(con, run_id, "orders", None, "OK", result["stored"], None, started)
    finally:
        con.close()


@ingest_app.command("order-pdfs")
def ingest_order_pdfs(
    limit: int = typer.Option(30, help="Documents to read this run"),
    model: str = typer.Option(None, help="OpenRouter model id"),
    days: int = typer.Option(540, help="Only events newer than this"),
) -> None:
    """Read attachments of order announcements whose text carried no figure."""
    from engine.orders import book_to_sales, enrich_unpriced

    run_id = _run_id()
    con = db.connect()
    try:
        started = dt.datetime.now()
        console.print(f"reading up to [bold]{limit}[/bold] order attachments...\n")

        def show(index, total, ticker, value):
            mark = (f"[green]Rs {value:,.0f} cr[/green]" if value
                    else "[dim]no rupee value[/dim]")
            console.print(f"  [{index:>3}/{total}] {ticker.replace('.NS',''):<14} {mark}")

        result = enrich_unpriced(con, limit=limit, since_days=days,
                                 model=model, progress=show)

        if result.get("message"):
            console.print(f"[green]{result['message']}[/green]")
            return
        if result.get("aborted"):
            console.print(f"\n[red]stopped: {result['aborted']}[/red]")

        console.print(f"\n[bold]{result['priced']} of {result['attempted']} priced[/bold] "
                      f"· {result.get('neither', 0)} were not orders "
                      f"· {result.get('foreign_currency', 0)} in foreign currency "
                      f"· {result['failed']} failed")
        console.print(f"cost: [green]${result['cost']:.4f}[/green]"
                      + (f" (${result['cost']/result['attempted']:.4f}/document)"
                         if result["attempted"] else ""))

        bts = book_to_sales(con)
        if not bts.empty:
            table = Table(title="Order book to sales")
            for col in ("ticker", "book (₹cr)", "revenue (₹cr)", "years", "as of"):
                table.add_column(col, justify="right" if col != "ticker" else "left")
            for _, r in bts.head(12).iterrows():
                if pd.isna(r.book_to_sales):
                    continue
                tone = "green" if r.book_to_sales >= 2 else "yellow" if r.book_to_sales >= 1 else "white"
                table.add_row(str(r.ticker).replace(".NS", ""), f"{r.order_book_cr:,.0f}",
                              f"{r.revenue_cr:,.0f}" if pd.notna(r.revenue_cr) else "—",
                              f"[{tone}]{r.book_to_sales:.1f}x[/{tone}]", str(r.event_date)[:10])
            console.print(table)

        db.log_ingest(con, run_id, "order_pdfs", None, "OK", result["priced"], None, started)
    finally:
        con.close()


@ingest_app.command("pledge")
def ingest_pledge(
    themes_only: bool = typer.Option(False, help="Theme companies only"),
    limit: int = typer.Option(0, help="Cap companies (0 = all)"),
) -> None:
    """Load promoter holding and encumbrance from NSE."""
    from engine.universe.builder import investable_universe, sync_promoter_pledge

    run_id = _run_id()
    con = db.connect()
    try:
        graph = load_theme_graph()
        if themes_only:
            tickers = graph.india_universe()
        else:
            seen = dict.fromkeys(graph.india_universe())
            for ticker in investable_universe(con):
                seen.setdefault(ticker, None)
            tickers = list(seen)
        if limit:
            tickers = tickers[:limit]

        console.print(f"fetching pledge disclosures for [bold]{len(tickers)}[/bold] companies...")
        started = dt.datetime.now()
        state = {"hit": 0}

        def show(index, total, symbol, found):
            if found:
                state["hit"] += 1
            if index % 50 == 0 or index == total:
                console.print(f"  {index:>4}/{total}  {state['hit']} with disclosure")

        result = sync_promoter_pledge(con, tickers, progress=show)
        console.print(f"\n[green]{result['written']:,} disclosures[/green] stored "
                      f"([yellow]{result['missing']} without data[/yellow], "
                      f"{result['failed']} failed)")

        worst = con.execute("""
            SELECT s.ticker, o.promoter_pct, o.promoter_pledge_pct, o.quarter_end
              FROM ownership_latest o JOIN securities s ON s.security_id = o.security_id
             WHERE o.promoter_pledge_pct IS NOT NULL
             ORDER BY o.promoter_pledge_pct DESC LIMIT 12
        """).df()
        if not worst.empty:
            table = Table(title="Most pledged promoter stakes")
            for col in ("ticker", "promoter %", "pledged % of stake", "as of"):
                table.add_column(col, justify="right" if col != "ticker" else "left")
            for _, r in worst.iterrows():
                tone = "red" if r.promoter_pledge_pct > 20 else "yellow"
                table.add_row(str(r.ticker),
                              f"{r.promoter_pct:.1f}" if pd.notna(r.promoter_pct) else "-",
                              f"[{tone}]{r.promoter_pledge_pct:.1f}[/{tone}]",
                              str(r.quarter_end)[:10])
            console.print(table)

        db.log_ingest(con, run_id, "pledge", None, "OK", result["written"], None, started)
    finally:
        con.close()


@ingest_app.command("pdf")
def ingest_pdf(
    batch: int = typer.Option(10, help="Companies per batch"),
    model: str = typer.Option(None, help="OpenRouter model id; overrides config"),
    restart: bool = typer.Option(False, help="Start from the first company again"),
    themes_only: bool = typer.Option(
        False, help="Restrict to theme-graph companies — the investable universe"
    ),
) -> None:
    """Extract post-2024 quarters from result PDFs, one batch at a time.

    Only guard-clean extractions reach fundamentals_pit. Everything else is
    quarantined in pdf_extractions with the reason, so quality is visible before
    more data exists.
    """
    from engine.pdf_ingest import quality_report, run_batch

    con = db.connect()
    try:
        scope = "theme companies" if themes_only else "companies"
        console.print(f"processing up to {batch} {scope}...\n")

        def show(index, total, symbol, clean, attempted):
            mark = "green" if clean == attempted and attempted else (
                "yellow" if clean else "red")
            console.print(f"  [{index:>3}/{total}] {symbol:<16} "
                          f"[{mark}]{clean}/{attempted} clean[/{mark}]")

        result = run_batch(con, batch_size=batch, model=model, restart=restart,
                           themes_only=themes_only, progress=show)

        if result.get("message"):
            console.print(f"[green]{result['message']}[/green]")
            return

        if result.get("aborted"):
            console.print(f"\n[red]run stopped: {result['aborted']}[/red]")
            console.print("[dim]No statements were marked failed — nothing is wrong "
                          "with the data. Re-run once the balance is topped up.[/dim]")

        rate = result["clean_rate"]
        tone = "green" if rate >= 0.7 else "yellow" if rate >= 0.4 else "red"
        console.print(
            f"\n[bold]{result['companies']} companies, "
            f"{result['attempted']} statements[/bold] in {result['seconds']:.0f}s\n"
            f"  [green]{result['clean']} clean[/green] (ingested) · "
            f"[yellow]{result['quarantined']} quarantined[/yellow] · "
            f"[red]{result['failed']} failed[/red]\n"
            f"  clean rate: [{tone}]{rate:.0%}[/{tone}]   cost: ${result['cost']:.4f}"
        )

        problems = result["problems"]
        if not problems.empty:
            console.print("\n[bold]why statements were held back[/bold]")
            for _, row in problems.head(10).iterrows():
                console.print(f"  {row['symbol']:<14} {str(row['period_end']):<12} "
                              f"{row['problems'][:76]}")

        cumulative = quality_report(con, model)
        if not cumulative.empty:
            table = Table(title="Cumulative")
            for col in ("status", "statements", "companies", "cost_usd"):
                table.add_column(col, justify="right" if col != "status" else "left")
            for _, r in cumulative.iterrows():
                table.add_row(str(r.status), f"{int(r.statements):,}",
                              f"{int(r.companies):,}", f"${float(r.cost_usd or 0):.4f}")
            console.print(table)

        console.print(f"\nnext batch resumes after [cyan]{result['last_ticker']}[/cyan]")
    finally:
        con.close()


@app.command()
def trends() -> None:
    """Compute daily/weekly/monthly stage and relative strength per company."""
    from engine.features import security_trend

    run_id = _run_id()
    con = db.connect()
    try:
        started = dt.datetime.now()
        console.print("computing company trends...")
        frame = security_trend.compute(con)
        if frame.empty:
            console.print("[yellow]not enough price history[/yellow]")
            return
        written = security_trend.store(con, frame)

        table = Table(title="Stage distribution")
        table.add_column("stage", style="cyan")
        table.add_column("companies", justify="right")
        table.add_column("median 12m", justify="right")
        table.add_column("median RS", justify="right")
        for stage, sub in frame.groupby("stage"):
            table.add_row(stage, f"{len(sub):,}",
                          f"{sub.mom_12m.median():+.0f}%" if sub.mom_12m.notna().any() else "—",
                          f"{sub.rs_12m.median():+.0f}" if sub.rs_12m.notna().any() else "—")
        console.print(table)
        console.print(f"[green]{written:,} companies[/green] scored for trend")
        db.log_ingest(con, run_id, "trends", None, "OK", written, None, started)
    finally:
        con.close()


@app.command()
def score() -> None:
    """Compute G.E.M. pillars and store them.

    Backtested at 3 rebalances: the composite does NOT rank (top decile beat the
    universe but the bottom decile beat the top, mean IC -0.078). Scores are
    stored to GROUP companies into bands, not to order them.
    """
    from engine.scoring import gem

    run_id = _run_id()
    con = db.connect()
    try:
        started = dt.datetime.now()
        as_of = dt.date.today()
        console.print("scoring the universe...")
        frame = gem.score_universe(con, as_of=as_of, include_non_pit=True)
        if frame.empty:
            console.print("[yellow]nothing to score[/yellow]")
            return

        # Band, not rank: thirds of the scored universe. Shared with the nightly
        # job so the column list lives in one place.
        frame = gem.store_scores(con, frame, as_of)

        table = Table(title="Pillar averages by band")
        table.add_column("band", style="cyan")
        table.add_column("companies", justify="right")
        for col in ("growth", "quality", "theme", "momentum"):
            table.add_column(col, justify="right")
        for band in ("UPPER", "MIDDLE", "LOWER"):
            sub = frame[frame.band == band]
            table.add_row(band, f"{len(sub):,}",
                          f"{sub.g_score.mean():.0f}", f"{sub.q_score.mean():.0f}",
                          f"{sub.t_score.mean():.0f}", f"{sub.m_score.mean():.0f}")
        console.print(table)
        console.print(f"[green]{len(frame):,} companies scored[/green] as of {as_of}")
        console.print("[dim]Bands group; they do not rank. See docs/BACKTEST.md.[/dim]")

        db.log_ingest(con, run_id, "score", None, "OK", len(frame), None, started)
    finally:
        con.close()


@app.command()
def gates(
    limit: int = typer.Option(0, help="Cap companies evaluated (0 = all)"),
    show: int = typer.Option(15, help="Rows of detail to print"),
) -> None:
    """Run the quality gates and report what each one rejected."""
    from engine.scoring import gates as gate_engine

    run_id = _run_id()
    con = db.connect()
    try:
        started = dt.datetime.now()
        as_of = dt.date.today()
        console.print(f"evaluating {len(gate_engine.GATES)} gates as of {as_of}...")
        frame = gate_engine.run_gates(con, as_of=as_of, limit=limit)

        if frame.empty:
            console.print("[yellow]no securities to evaluate[/yellow]")
            return

        summary = gate_engine.gate_summary(frame)
        table = Table(title="Gate outcomes")
        table.add_column("gate", style="cyan")
        for col in ("pass", "fail", "unknown"):
            table.add_column(col, justify="right")
        for gate_name, row in summary.iterrows():
            critical = gate_name in gate_engine.CRITICAL
            table.add_row(
                f"{gate_name}{' *' if critical else ''}",
                f"[green]{int(row['PASS'])}[/green]",
                f"[red]{int(row['FAIL'])}[/red]" if row["FAIL"] else "0",
                f"[yellow]{int(row['UNKNOWN'])}[/yellow]" if row["UNKNOWN"] else "0",
            )
        console.print(table)
        console.print("[dim]* failing a starred gate rejects the name outright[/dim]")

        verdict = gate_engine.verdicts(frame)
        counts = verdict.verdict.value_counts().to_dict()
        console.print(
            f"\n[red]{counts.get('REJECTED', 0)} rejected[/red] · "
            f"[yellow]{counts.get('FLAGGED', 0)} flagged[/yellow] · "
            f"[yellow]{counts.get('UNVETTED', 0)} unvetted[/yellow] · "
            f"[green]{counts.get('CLEARED', 0)} cleared[/green] "
            f"of {len(verdict):,} companies"
        )

        rejected = verdict[verdict.verdict == "REJECTED"].head(show)
        if not rejected.empty:
            console.print("\n[bold]rejected[/bold] (reason shown to you in the app):")
            for _, r in rejected.iterrows():
                console.print(f"  [red]x[/red] {r.ticker:<16} {r.reason[:88]}")

        settings.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = settings.REPORT_DIR / "gate_verdicts.csv"
        verdict.to_csv(out, index=False)
        console.print(f"\nreport: {out}")

        db.log_ingest(con, run_id, "gates", None, "OK", len(frame),
                      f"{len(verdict)} companies", started)
    finally:
        con.close()


# ----------------------------------------------------------------- portfolio
folio_app = typer.Typer(help="Portfolio: positions, staged buying, thesis health")
app.add_typer(folio_app, name="folio")


@folio_app.command("open")
def folio_open(
    ticker: str = typer.Argument(..., help="NSE symbol, e.g. WABAG"),
    tier: str = typer.Option("SATELLITE", help="CORE | SATELLITE | WATCHLIST"),
    thesis: str = typer.Option(..., "--thesis", help="Why, in your own words"),
    theme: str = typer.Option(None, help="Theme label"),
    weight: float = typer.Option(None, help="Target % of portfolio"),
) -> None:
    """Open a position as an intention, and lay out its three-tranche ladder."""
    from engine.portfolio import book

    con = book.connect()
    try:
        position_id = book.open_position(con, ticker, tier, thesis, theme, weight)
        console.print(f"[green]opened[/green] {ticker.upper()} as {tier.upper()} "
                      f"(position {position_id})")
        ladder = con.execute("""
            SELECT stage, planned_pct, trigger FROM tranches
             WHERE position_id = ? ORDER BY stage
        """, [position_id]).df()
        table = Table(title="Ladder")
        for col in ("stage", "share", "buy when"):
            table.add_column(col)
        for _, r in ladder.iterrows():
            table.add_row(str(int(r.stage)), f"{r.planned_pct:.0f}%", r.trigger)
        console.print(table)

        # A thesis is required; a CHECKABLE thesis is not, and that is the gap
        # this prompt exists to close. Prose cannot be tested, so without a
        # claim nothing will ever tell you the reason stopped being true.
        console.print(
            f"\n[yellow]No claims yet — this thesis cannot be disproved.[/yellow]\n"
            f"  [dim]investo folio claim {ticker.upper().removesuffix('.NS')} --list[/dim]"
            "   to see what the engine can measure"
        )
    finally:
        con.close()


@folio_app.command("buy")
def folio_buy(
    ticker: str = typer.Argument(...),
    stage: int = typer.Option(..., help="Which tranche (1, 2 or 3)"),
    shares: float = typer.Option(..., help="Shares bought"),
    price: float = typer.Option(..., help="Price per share"),
    note: str = typer.Option(None),
) -> None:
    """Record an executed tranche."""
    from engine.portfolio import book

    con = book.connect()
    try:
        book.record_buy(con, ticker, stage, shares, price, note=note)
        console.print(f"[green]recorded[/green] {ticker.upper()} stage {stage}: "
                      f"{shares:,.0f} @ {price:,.2f} = Rs {shares*price:,.0f}")
    finally:
        con.close()


@folio_app.command("status")
def folio_status() -> None:
    """Holdings, thesis health and concentration."""
    from engine.portfolio import book

    con = book.connect()
    try:
        health = book.review_thesis(con)
        held = book.holdings(con)
        if held.empty:
            console.print("[yellow]no open positions[/yellow] — "
                          "start with `investo folio open TICKER --thesis \"...\"`")
            return

        merged = held.merge(health[["position_id", "health", "reasons"]],
                            on="position_id", how="left")
        table = Table(title="Positions")
        for col in ("ticker", "tier", "cost", "value", "P&L", "weight", "next", "health"):
            table.add_column(col, justify="right" if col not in ("ticker", "tier") else "left")
        tone = {"GREEN": "green", "AMBER": "yellow", "RED": "red"}
        for _, r in merged.iterrows():
            colour = tone.get(r.health, "white")
            table.add_row(
                r.ticker.replace(".NS", ""), r.tier,
                f"{r.cost:,.0f}" if r.cost else "—",
                f"{r.value:,.0f}" if pd.notna(r.value) and r.value else "—",
                f"{r.pnl_pct:+.1f}%" if pd.notna(r.pnl_pct) else "—",
                f"{r.weight_pct:.1f}%" if pd.notna(r.weight_pct) else "—",
                f"stage {int(r.next_stage)}" if pd.notna(r.next_stage) else "complete",
                f"[{colour}]{r.health or '?'}[/{colour}]",
            )
        console.print(table)

        for _, r in merged[merged.health.isin(["AMBER", "RED"])].iterrows():
            console.print(f"  [yellow]{r.ticker.replace('.NS','')}[/yellow]: {r.reasons}")

        # Every claim, including the ones holding. A thesis still true is the
        # evidence for staying in, and it was measured nightly and shown nowhere.
        claims = book.claim_status(con)
        # ASCII, deliberately: the Windows console encodes cp1252 by default and
        # a tick character crashes the whole command with a UnicodeEncodeError.
        # The PWA renders UTF-8 and uses proper marks there.
        mark = {"HOLDS": "[green]holds [/green]", "BROKEN": "[red]BROKEN[/red]",
                "UNCHECKABLE": "[dim]n/a   [/dim]"}
        console.print("\n[bold]Thesis claims[/bold]")
        for _, position in merged.iterrows():
            name = position.ticker.replace(".NS", "")
            mine = claims[claims.position_id == position.position_id] if not claims.empty \
                else claims
            if mine.empty:
                console.print(f"  {name}: [yellow]not falsifiable[/yellow] — no claims. "
                              f"[dim]investo folio claim {name} --list[/dim]")
                continue
            for _, c in mine.iterrows():
                observed = "n/d" if pd.isna(c.observed) else f"{c.observed:,.2f}"
                console.print(f"  {name}: {mark.get(c.status, '[dim]—[/dim]')} "
                              f"{c.metric} {c.comparator} {c.threshold:g} "
                              f"[dim]· now {observed}[/dim]")

        x = book.xray(con)
        console.print(f"\ninvested Rs {x['invested']:,.0f} · value Rs {x['value']:,.0f}"
                      + (f" · {x['pnl_pct']:+.1f}%" if x.get("pnl_pct") is not None else ""))
        if x.get("theme_concentration"):
            top = list(x["theme_concentration"].items())[:4]
            console.print("theme mix: " + " · ".join(f"{k} {v:.0f}%" for k, v in top))
            if x["largest_theme_pct"] > 40:
                console.print(f"[yellow]{x['largest_theme_pct']:.0f}% sits in one theme — "
                              "check these are separate bets[/yellow]")
    finally:
        con.close()


@folio_app.command("plan")
def folio_plan(budget: float = typer.Argument(..., help="This month's investable amount")) -> None:
    """Split a monthly budget across the tranches that are due."""
    from engine.portfolio import book

    con = book.connect()
    try:
        book.review_thesis(con)
        plan = book.deployment_plan(con, budget)
        if plan.empty:
            console.print("[yellow]nothing due[/yellow] — every ladder is complete, "
                          "or the theses that would receive money are not GREEN")
            return
        table = Table(title=f"Deploying Rs {budget:,.0f}")
        for col in ("ticker", "tier", "stage", "allocate", "buy when"):
            table.add_column(col, justify="right" if col == "allocate" else "left")
        for _, r in plan.iterrows():
            table.add_row(r.ticker.replace(".NS", ""), r.tier, f"stage {int(r.stage)}",
                          f"Rs {r.allocate:,.0f}", (r.trigger or "")[:52])
        console.print(table)
        console.print("[dim]Only GREEN theses receive money. Adding to a broken "
                      "thesis is the habit this is meant to interrupt.[/dim]")
    finally:
        con.close()


@folio_app.command("note")
def folio_note(
    ticker: str = typer.Argument(...),
    body: str = typer.Option(..., "--body"),
    kind: str = typer.Option("NOTE", help="NOTE | REVIEW | EXIT"),
) -> None:
    """Add a journal entry."""
    from engine.portfolio import book

    con = book.connect()
    try:
        book.add_journal(con, ticker, body, kind)
        console.print(f"[green]logged[/green] {kind.upper()} for {ticker.upper()}")
    finally:
        con.close()


@folio_app.command("claim")
def folio_claim(
    # Optional so `--list` works on its own: someone reaching for this command
    # usually does not yet know what the engine can measure.
    ticker: str = typer.Argument("", help="Position to attach the claim to"),
    metric: str = typer.Argument("", help="What to measure (see --list)"),
    comparator: str = typer.Argument("", help="'>=' or '<='"),
    threshold: float = typer.Argument(0.0, help="The number you are betting on"),
    note: str = typer.Option("", "--note", help="Why this number, in your words"),
    show: bool = typer.Option(False, "--list", help="List measurable metrics and exit"),
) -> None:
    """Record what would make this thesis WRONG.

        investo folio claim WABAG order_book_to_sales '>=' 2.0 --note "AMRUT 2.0"

    A thesis in prose cannot be checked, so nothing ever checked one. A claim is
    one falsifiable assertion, tested against real data every night, and reported
    as HOLDS, BROKEN or UNCHECKABLE -- the third distinct from the first, because
    a claim nobody can measure is not a claim that passed.
    """
    from engine.portfolio import book, claims as claim_engine

    if show:
        table = Table(title="Measurable claim metrics")
        table.add_column("metric", style="cyan")
        table.add_column("unit", justify="center")
        table.add_column("measures")
        for name, (_fn, unit, label) in sorted(claim_engine.MEASURES.items()):
            table.add_row(name, unit, label)
        console.print(table)
        console.print("[dim]investo folio claim WABAG order_book_to_sales '>=' 2.0[/dim]")
        return

    if not (ticker and metric and comparator):
        console.print("[red]need ticker, metric and comparator[/red] — e.g. "
                      "[dim]investo folio claim WABAG order_book_to_sales '>=' 2.0[/dim]\n"
                      "Run with --list to see what the engine can measure.")
        raise typer.Exit(1)

    con = book.connect()
    try:
        book.add_claim(con, ticker, metric, comparator, threshold, note)
        console.print(f"[green]claim recorded[/green] {ticker.upper()}: "
                      f"{metric} {comparator} {threshold:g}")
        console.print("[dim]Checked on every review. A broken claim moves the "
                      "position off GREEN.[/dim]")
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        con.close()


@folio_app.command("backup")
def folio_backup(
    to: str = typer.Option("", "--to", help="Folder to back up into, remembered after"),
    keep: int = typer.Option(10, help="How many to retain (0 = keep all)"),
) -> None:
    """Copy the portfolio somewhere safe, and verify the copy opens.

    Everything in the analytics store rebuilds from providers in an afternoon.
    Nothing in here rebuilds at all — positions, theses, claims, and the history
    of what was true when. Point --to at a synced folder; a second copy on the
    same disk survives a mistake, not a disk failure.
    """
    from engine.portfolio import book

    if to:
        con = book.connect()
        try:
            book.set_backup_destination(con, to)
        finally:
            con.close()

    result = book.backup(keep=keep)
    if not result["ok"]:
        console.print(f"[red]backup failed[/red] — {result['reason']}")
        raise typer.Exit(1)

    console.print(f"[green]backed up[/green] {result['path']}")
    console.print(f"[dim]{result['rows']} rows verified across "
                  f"{len(result['tables'])} tables · "
                  f"{result['bytes'] / 1e6:.1f} MB"
                  + (f" · {result['pruned']} older removed" if result["pruned"] else "")
                  + "[/dim]")
    # Is the copy inside the project itself? Compare paths rather than matching
    # on names: the first version looked for "AI-Investo" in the string and
    # warned about C:\Users\...\OneDrive\AI-Investo-backups, which is exactly
    # the destination it was meant to encourage.
    from pathlib import Path as _Path
    try:
        inside_project = result["path"].resolve().is_relative_to(
            _Path(settings.DATA_DIR).resolve().parent)
    except (OSError, ValueError):
        inside_project = False
    if inside_project:
        console.print("[yellow]This is still on the same disk as the original.[/yellow] "
                      "[dim]investo folio backup --to <synced folder>[/dim]")


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
