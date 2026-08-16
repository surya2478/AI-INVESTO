"""The portfolio is the one file that cannot be rebuilt.

`engine/storage/portfolio_schema.sql` says it outright -- everything in the
analytics store rebuilds from providers in an afternoon, nothing in the portfolio
rebuilds at all -- and `.gitignore` says "back it up, do not commit it". Both
instructions were written down and neither was implemented.

The property worth pinning is that a backup which cannot be OPENED is worse than
no backup, because it stops you looking for one. So verification reads row
counts out of the copy rather than checking a file appeared.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from engine.portfolio import book


@pytest.fixture()
def portfolio(tmp_path, monkeypatch):
    """A real portfolio file in a temp dir, with the module pointed at it."""
    path = tmp_path / "db" / "portfolio.duckdb"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(book, "portfolio_path", lambda: path)

    con = book.connect()
    try:
        book.open_position(con, "WABAG", "SATELLITE", "Water EPC, order book")
        book.add_claim(con, "WABAG", "order_book_to_sales", ">=", 2.0, note="AMRUT")
    finally:
        con.close()
    return path


def test_a_backup_is_written_and_verified(portfolio, tmp_path):
    result = book.backup(destination=tmp_path / "safe")
    assert result["ok"]
    assert result["path"].exists()
    assert result["tables"]["positions"] == 1
    assert result["tables"]["thesis_claims"] == 1


def test_the_copy_is_actually_readable(portfolio, tmp_path):
    """Verification opens the copy. A truncated file has a size too."""
    result = book.backup(destination=tmp_path / "safe")
    con = duckdb.connect(str(result["path"]), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM positions").fetchone()[0] == 1
        claim = con.execute("SELECT metric, threshold FROM thesis_claims").fetchone()
        assert claim == ("order_book_to_sales", 2.0)
    finally:
        con.close()


def test_a_corrupt_copy_is_rejected_and_removed(portfolio, tmp_path, monkeypatch):
    """A backup that does not verify must not be left looking like one."""
    def truncate(src, dst):
        __import__("pathlib").Path(dst).write_bytes(b"not a database")

    monkeypatch.setattr(book.shutil, "copy2", truncate)
    result = book.backup(destination=tmp_path / "safe")
    assert not result["ok"]
    assert list((tmp_path / "safe").glob("portfolio-*.duckdb")) == []


def test_every_table_that_matters_is_checked():
    """Adding a table without adding it here would silently stop verifying it."""
    assert set(book.BACKED_UP_TABLES) == {
        "positions", "tranches", "journal",
        "thesis_claims", "thesis_claim_checks", "thesis_health",
    }


def test_retention_keeps_the_newest(portfolio, tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    for day in range(5):
        (safe / f"portfolio-2026081{day}-000000.duckdb").write_bytes(b"old")
    book.backup(destination=safe, keep=3)
    kept = sorted(p.name for p in safe.glob("portfolio-*.duckdb"))
    assert len(kept) == 3
    assert kept[-1].startswith("portfolio-2026")


def test_keep_zero_retains_everything(portfolio, tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    for day in range(3):
        (safe / f"portfolio-2026081{day}-000000.duckdb").write_bytes(b"old")
    book.backup(destination=safe, keep=0)
    assert len(list(safe.glob("portfolio-*.duckdb"))) == 4


def test_a_missing_portfolio_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(book, "portfolio_path", lambda: tmp_path / "absent.duckdb")
    result = book.backup(destination=tmp_path / "safe")
    assert not result["ok"] and "no portfolio" in result["reason"]


def test_the_destination_is_remembered(portfolio, tmp_path):
    """Chosen once, not typed every night."""
    con = book.connect()
    try:
        book.set_backup_destination(con, str(tmp_path / "synced"))
        assert book.backup_destination(con) == tmp_path / "synced"
    finally:
        con.close()
