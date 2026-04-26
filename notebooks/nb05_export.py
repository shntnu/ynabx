# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "duckdb",
#     "marimo",
#     "polars",
#     "pyarrow",
#     "requests",
# ]
# ///

import marimo

__generated_with = "0.23.3"
app = marimo.App(width="medium")

with app.setup:
    import datetime as dt
    import sys
    from pathlib import Path

    import duckdb
    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    YNAB_DIR = NOTEBOOK_DIR.parent
    EXPORT_DIR = YNAB_DIR / "data" / "exports"
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb02_ynab_sync import connect


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb05 - Export

    Dump the local YNAB cache to portable formats for analysis outside the
    notebook (a spreadsheet, an external dashboard, a one-off polars script,
    etc.). Three shapes:

    - `export_transactions(path, since, until, format)` - flat dump of the
      transactions table (parquet by default, csv on demand).
    - `monthly_category_summary(year)` - month x category spend, useful as
      the input to a category-by-month heatmap or pivot.
    - `payee_totals(days, top_n)` - top payees by spend in a window.

    All exports land under `data/exports/` (gitignored) unless an absolute
    path is given.
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Transactions
    """)
    return


@app.function(hide_code=True)
def export_transactions(
    path: str | Path | None = None,
    since: str | None = None,
    until: str | None = None,
    format: str = "parquet",
    include_deleted: bool = False,
) -> Path:
    """Dump transactions to parquet/csv. Returns the written path.

    `since` and `until` are optional ISO dates ("YYYY-MM-DD"). Default is
    everything alive. Format is "parquet" or "csv".
    """
    where = []
    params: list = []
    if not include_deleted:
        where.append("NOT deleted")
    if since:
        where.append("date >= ?")
        params.append(since)
    if until:
        where.append("date <= ?")
        params.append(until)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    con = connect()
    df = con.execute(
        f"""
        SELECT id, parent_id, date, amount_milli, amount_milli/1000.0 AS amount,
               account_name, payee_name, category_name,
               memo, cleared, approved, flag_color,
               import_payee_name, import_payee_name_original,
               has_splits, deleted
        FROM transactions
        {where_sql}
        ORDER BY date DESC
        """,
        params,
    ).pl()
    con.close()

    if path is None:
        stamp = dt.date.today().isoformat()
        path = EXPORT_DIR / f"transactions_{stamp}.{format}"
    path = Path(path)

    if format == "parquet":
        df.write_parquet(path)
    elif format == "csv":
        df.write_csv(path)
    else:
        raise ValueError(f"unsupported format: {format}")
    return path


@app.cell
def _():
    _path = export_transactions(format="parquet")
    _size_mb = _path.stat().st_size / 1024 / 1024
    mo.md(
        f"### Transactions export\n\n"
        f"Wrote `{_path.name}` ({_size_mb:.2f} MB) to `{_path.parent}`."
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Monthly category summary
    """)
    return


@app.function(hide_code=True)
def monthly_category_summary(year: int | None = None) -> pl.DataFrame:
    """Spend per month per category. One row per (month, category)."""
    where = [
        "NOT deleted",
        "(parent_id IS NULL OR has_splits = FALSE)",
        "category_name IS NOT NULL",
        "category_name != 'Uncategorized'",
        "COALESCE(payee_name, '') NOT LIKE 'Transfer :%'",
    ]
    params: list = []
    if year is not None:
        where.append("EXTRACT(year FROM date) = ?")
        params.append(year)
    where_sql = " AND ".join(where)

    con = connect()
    df = con.execute(
        f"""
        SELECT
            DATE_TRUNC('month', date) AS month,
            category_name,
            SUM(amount_milli)/1000.0 AS amount,
            COUNT(*) AS n_txns
        FROM transactions
        WHERE {where_sql}
        GROUP BY month, category_name
        ORDER BY month DESC, amount ASC
        """,
        params,
    ).pl()
    con.close()
    return df


@app.cell
def _():
    monthly = monthly_category_summary(year=2026)
    mo.vstack(
        [
            mo.md("### Monthly category summary (2026)"),
            mo.ui.table(monthly, page_size=20, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Top payees
    """)
    return


@app.function(hide_code=True)
def payee_totals(days: int = 180, top_n: int = 50) -> pl.DataFrame:
    """Top payees by absolute spend over the last `days`."""
    con = connect()
    df = con.execute(
        """
        SELECT
            payee_name,
            COUNT(*) AS n_txns,
            SUM(amount_milli)/1000.0 AS net_amount,
            MIN(date) AS first_seen,
            MAX(date) AS last_seen
        FROM transactions
        WHERE NOT deleted
          AND (parent_id IS NULL OR has_splits = FALSE)
          AND date >= current_date - (? * INTERVAL 1 DAY)
          AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
          AND payee_name IS NOT NULL
        GROUP BY payee_name
        ORDER BY ABS(SUM(amount_milli)) DESC
        LIMIT ?
        """,
        [days, top_n],
    ).pl()
    con.close()
    return df


@app.cell
def _():
    payees = payee_totals(days=90, top_n=20)
    mo.vstack(
        [
            mo.md("### Top payees (last 90 days)"),
            mo.ui.table(payees, page_size=20, selection=None),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
