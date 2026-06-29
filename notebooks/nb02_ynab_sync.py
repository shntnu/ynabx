# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "duckdb",
#     "marimo",
#     "polars",
#     "requests",
# ]
# ///

import marimo

__generated_with = "0.23.11"
app = marimo.App(width="medium")

with app.setup:
    import datetime as dt
    import os
    import sys
    from pathlib import Path

    import duckdb
    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    YNAB_DIR = NOTEBOOK_DIR.parent
    DB_PATH = Path(os.environ.get("YNAB_DB_PATH", YNAB_DIR / "data" / "ynab.db"))
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb01_ynab_client import active_budget_id, get

    def txn_rows(txns: list[dict]) -> list[tuple]:
        """Flatten a YNAB transactions payload into DuckDB row tuples.

        One row per leaf; split parents get has_splits=True and their
        subtransactions become separate rows with parent_id set.
        """
        rows = []
        for t in txns:
            subs = t.get("subtransactions") or []
            rows.append(
                (
                    t["id"],
                    None,
                    t["date"],
                    t["amount"],
                    t.get("payee_id"),
                    t.get("payee_name"),
                    t.get("category_id"),
                    t.get("category_name"),
                    t["account_id"],
                    t.get("account_name"),
                    t.get("memo"),
                    t.get("cleared"),
                    t.get("approved"),
                    t.get("flag_color"),
                    t.get("import_id"),
                    t.get("import_payee_name"),
                    t.get("import_payee_name_original"),
                    bool(subs),
                    t.get("deleted", False),
                )
            )
            for sub in subs:
                rows.append(
                    (
                        sub["id"],
                        t["id"],
                        t["date"],
                        sub["amount"],
                        sub.get("payee_id") or t.get("payee_id"),
                        sub.get("payee_name") or t.get("payee_name"),
                        sub.get("category_id"),
                        sub.get("category_name"),
                        t["account_id"],
                        t.get("account_name"),
                        sub.get("memo"),
                        t.get("cleared"),
                        t.get("approved"),
                        t.get("flag_color"),
                        t.get("import_id"),
                        t.get("import_payee_name"),
                        t.get("import_payee_name_original"),
                        False,
                        sub.get("deleted", False),
                    )
                )
        return rows

    def upsert(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> int:
        """Upsert row tuples (from txn_rows) into the transactions table."""
        if not rows:
            return 0
        con.executemany(
            """
            INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (id) DO UPDATE SET
                parent_id=EXCLUDED.parent_id, date=EXCLUDED.date,
                amount_milli=EXCLUDED.amount_milli,
                payee_id=EXCLUDED.payee_id, payee_name=EXCLUDED.payee_name,
                category_id=EXCLUDED.category_id, category_name=EXCLUDED.category_name,
                account_id=EXCLUDED.account_id, account_name=EXCLUDED.account_name,
                memo=EXCLUDED.memo, cleared=EXCLUDED.cleared, approved=EXCLUDED.approved,
                flag_color=EXCLUDED.flag_color, import_id=EXCLUDED.import_id,
                import_payee_name=EXCLUDED.import_payee_name,
                import_payee_name_original=EXCLUDED.import_payee_name_original,
                has_splits=EXCLUDED.has_splits, deleted=EXCLUDED.deleted
        """,
            rows,
        )
        return len(rows)


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb02 - Local cache (delta sync)

    Mirror YNAB transactions to a local DuckDB at `data/ynab.db` so analyses
    in later notebooks query a fast local cache instead of round-tripping to
    the API. Uses YNAB's `server_knowledge` cursor for cheap delta sync:
    the first call pulls everything, subsequent calls return only what
    changed.

    What's in the DB:

    - **`transactions`** - one row per leaf entry. Split parents are stored
      with `has_splits=TRUE`; their subtransactions are stored as separate
      rows with `parent_id` set. Sum `amount_milli` over `parent_id IS NULL
      OR has_splits=FALSE` to get total spend without double-counting.
    - **`meta`** - one row per budget tracking `server_knowledge` and the
      last sync timestamp.

    Amounts are in **milliunits** (×1000). Divide by 1000 to get dollars.

    Run `sync()` to bring the cache up to date. Idempotent: re-running with
    no remote changes returns 0 rows upserted in <1s.

    `reconcile()` is the occasional deep clean: the delta feed leaves behind
    imports that YNAB later *matches* into another transaction (they 404 and
    get no deletion tombstone), so they linger as duplicate rows and
    double-count in spend. reconcile() pulls the full `since_date` snapshot and
    prunes anything absent from it - with a guard that aborts if the pull comes
    back suspiciously short.
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Connection + schema
    """)
    return


@app.function(hide_code=True)
def connect() -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection at DB_PATH and ensure tables exist."""
    con = duckdb.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id VARCHAR PRIMARY KEY,
            parent_id VARCHAR,
            date DATE,
            amount_milli BIGINT,
            payee_id VARCHAR,
            payee_name VARCHAR,
            category_id VARCHAR,
            category_name VARCHAR,
            account_id VARCHAR,
            account_name VARCHAR,
            memo VARCHAR,
            cleared VARCHAR,
            approved BOOLEAN,
            flag_color VARCHAR,
            import_id VARCHAR,
            import_payee_name VARCHAR,
            import_payee_name_original VARCHAR,
            has_splits BOOLEAN,
            deleted BOOLEAN
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            budget_id VARCHAR PRIMARY KEY,
            server_knowledge BIGINT,
            last_synced_at TIMESTAMP
        )
    """)
    return con


@app.function(hide_code=True)
def last_knowledge(con: duckdb.DuckDBPyConnection, budget_id: str) -> int:
    """Last server_knowledge stored for this budget; 0 if never synced."""
    row = con.execute("SELECT server_knowledge FROM meta WHERE budget_id = ?", [budget_id]).fetchone()
    return row[0] if row else 0


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Delta sync
    """)
    return


@app.function(hide_code=True)
def sync(budget_id: str | None = None) -> dict:
    """Delta-sync transactions for the given budget into the local DuckDB.

    Uses YNAB's server_knowledge cursor: only changed records since the last
    sync are returned. Idempotent - calling repeatedly returns 0 new rows.

    Note: the delta feed includes import rows that YNAB later *matches* into an
    existing transaction. Once matched they 404 individually and carry no
    deletion tombstone, so sync() can never remove them - they linger as
    duplicate rows. Run reconcile() periodically to prune them.

    Returns a dict with counts and the new server_knowledge value.
    """
    bid = budget_id or active_budget_id()
    con = connect()
    since = last_knowledge(con, bid)

    payload = get(f"/budgets/{bid}/transactions", last_knowledge_of_server=since)
    new_sk = payload["server_knowledge"]
    txns = payload["transactions"]

    rows = txn_rows(txns)
    n_upserted = upsert(con, rows)

    con.execute(
        """
        INSERT INTO meta VALUES (?, ?, ?)
        ON CONFLICT (budget_id) DO UPDATE SET
            server_knowledge=EXCLUDED.server_knowledge,
            last_synced_at=EXCLUDED.last_synced_at
    """,
        [bid, new_sk, dt.datetime.now()],
    )
    con.close()

    return {
        "budget_id": bid,
        "since_knowledge": since,
        "new_knowledge": new_sk,
        "txns_in_payload": len(txns),
        "rows_upserted": n_upserted,
    }


@app.function(hide_code=True)
def reconcile(budget_id: str | None = None, prune_guard: float = 0.9) -> dict:
    """Align the cache to YNAB's authoritative full snapshot, pruning stale rows.

    sync() can leave duplicate rows behind: the delta feed includes imports that
    YNAB later matches/absorbs, and those never get a deletion tombstone. This
    pulls the *full* history via `since_date` (NOT `last_knowledge_of_server`,
    which YNAB caps to a rolling ~1 year) - that snapshot is the clean current
    state and excludes the absorbed twins. We upsert it, then DELETE any cache
    row whose id is absent from it.

    Safety: refuses to prune if the snapshot's live-row count is below
    `prune_guard` x the current cache (default 90%). A short/partial pull (e.g.
    the ~1yr window) trips this and aborts rather than deleting real history.

    Returns counts incl. `rows_pruned` (-1 if the guard aborted the prune).
    """
    bid = budget_id or active_budget_id()
    con = connect()

    payload = get(f"/budgets/{bid}/transactions", since_date="2000-01-01")
    new_sk = payload["server_knowledge"]
    txns = payload["transactions"]
    rows = txn_rows(txns)

    cache_alive = con.execute("SELECT COUNT(*) FROM transactions WHERE NOT deleted").fetchone()[0]
    live_ids = {r[0] for r in rows if not r[18]}  # r[18] = deleted flag

    aborted = bool(cache_alive) and len(live_ids) < prune_guard * cache_alive
    n_upserted = upsert(con, rows)

    n_pruned = -1
    if not aborted and live_ids:
        con.execute("CREATE TEMP TABLE _live (id VARCHAR)")
        con.executemany("INSERT INTO _live VALUES (?)", [(i,) for i in live_ids])
        n_pruned = con.execute(
            "SELECT COUNT(*) FROM transactions WHERE NOT deleted AND id NOT IN (SELECT id FROM _live)"
        ).fetchone()[0]
        con.execute("DELETE FROM transactions WHERE NOT deleted AND id NOT IN (SELECT id FROM _live)")
        con.execute("DROP TABLE _live")

    con.execute(
        """
        INSERT INTO meta VALUES (?, ?, ?)
        ON CONFLICT (budget_id) DO UPDATE SET
            server_knowledge=EXCLUDED.server_knowledge,
            last_synced_at=EXCLUDED.last_synced_at
    """,
        [bid, new_sk, dt.datetime.now()],
    )
    con.close()

    return {
        "budget_id": bid,
        "snapshot_live": len(live_ids),
        "cache_alive_before": cache_alive,
        "rows_upserted": n_upserted,
        "rows_pruned": n_pruned,
        "prune_aborted": aborted,
        "new_knowledge": new_sk,
    }


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Status
    """)
    return


@app.function(hide_code=True)
def status() -> dict:
    """Snapshot of the local cache: row counts, date range, last sync."""
    con = connect()
    out = {
        "db_path": str(DB_PATH),
        "rows_total": con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "rows_alive": con.execute("SELECT COUNT(*) FROM transactions WHERE NOT deleted").fetchone()[0],
        "split_parents": con.execute("SELECT COUNT(*) FROM transactions WHERE has_splits").fetchone()[0],
        "date_min": con.execute("SELECT MIN(date) FROM transactions").fetchone()[0],
        "date_max": con.execute("SELECT MAX(date) FROM transactions").fetchone()[0],
        "meta": con.execute("SELECT * FROM meta").fetchall(),
    }
    con.close()
    return out


@app.cell
def _():
    _s = status()
    mo.md(
        "### Local cache status\n\n"
        f"- DB: `{_s['db_path']}`\n"
        f"- Rows: **{_s['rows_alive']:,}** alive ({_s['rows_total']:,} total)\n"
        f"- Split parents: {_s['split_parents']}\n"
        f"- Date range: {_s['date_min']} - {_s['date_max']}\n"
        f"- Meta: `{_s['meta']}`"
    )
    return


if __name__ == "__main__":
    app.run()
