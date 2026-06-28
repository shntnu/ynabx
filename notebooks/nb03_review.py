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

__generated_with = "0.23.3"
app = marimo.App(width="medium")

with app.setup:
    import sys
    from pathlib import Path

    import duckdb
    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb02_ynab_sync import connect


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb03 - Categorization review

    Triage workflow for the question *"are the new transactions categorized
    correctly?"* - the thing you'd otherwise scroll through YNAB by hand to
    check.

    A transaction is **pending review** when any of these is true:

    - `approved = FALSE` - YNAB's "freshly imported, not eyeballed yet" flag
    - `category_name = 'Uncategorized'` - never got a category assigned
    - `cleared = 'uncleared'` - the bank hasn't cleared it yet

    Inter-account transfers (`payee` like `Transfer : *`) are excluded - they
    don't carry categories in YNAB and aren't review-worthy.

    For each pending row we attach a **suggested category** based on history:
    the most-common category for that payee in the last 180 days. When the
    current category disagrees with the suggestion, it's the row to look at.

    This notebook is read-only - it shows you what to fix. Edits happen in
    the YNAB app (or in `nb04_bulk_edit` later).
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Suggestion engine
    """)
    return


@app.function(hide_code=True)
def payee_top_categories(con: duckdb.DuckDBPyConnection, payee_id: str, days: int = 180) -> list[tuple[str, str, int]]:
    """Most-common categories for a payee in the last `days`.

    Returns a list of (category_name, category_id, count), descending by count.
    Excludes deleted txns, the 'Uncategorized' bucket, and split parents.
    """
    rows = con.execute(
        """
        SELECT category_name, ANY_VALUE(category_id) AS cid, COUNT(*) AS n
        FROM transactions
        WHERE payee_id = ?
          AND NOT deleted
          AND date >= current_date - (? * INTERVAL 1 DAY)
          AND category_name IS NOT NULL
          AND category_name != 'Uncategorized'
          AND (parent_id IS NULL OR has_splits = FALSE)
        GROUP BY category_name
        ORDER BY n DESC
        """,
        [payee_id, days],
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


@app.function(hide_code=True)
def suggest_category(con: duckdb.DuckDBPyConnection, payee_id: str, days: int = 180) -> tuple[str, str] | None:
    """Top historical (category_name, category_id) for a payee, or None."""
    tops = payee_top_categories(con, payee_id, days)
    return (tops[0][0], tops[0][1]) if tops else None


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Pending review
    """)
    return


@app.function(hide_code=True)
def pending(days: int = 30, history_days: int = 180) -> pl.DataFrame:
    """Transactions needing review, with suggested categories from payee history.

    Excludes inter-account transfers (`Transfer : *`).
    """
    con = connect()
    rows = con.execute(
        """
        SELECT id, date, account_name, payee_id, payee_name,
               amount_milli/1000.0 AS amount,
               category_id AS current_category_id,
               category_name AS current_category,
               cleared, approved, flag_color, memo
        FROM transactions
        WHERE NOT deleted
          AND (parent_id IS NULL OR has_splits = FALSE)
          AND (NOT approved OR category_name = 'Uncategorized' OR cleared = 'uncleared')
          AND date >= current_date - (? * INTERVAL 1 DAY)
          AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
        ORDER BY date DESC, account_name, payee_name
        """,
        [days],
    ).fetchall()
    cols = [
        "id",
        "date",
        "account",
        "payee_id",
        "payee",
        "amount",
        "current_category_id",
        "current_category",
        "cleared",
        "approved",
        "flag",
        "memo",
    ]
    df = pl.DataFrame(rows, schema=cols, orient="row")

    sug_names: list[str | None] = []
    sug_ids: list[str | None] = []
    for pid in df["payee_id"]:
        s = suggest_category(con, pid, days=history_days) if pid else None
        sug_names.append(s[0] if s else None)
        sug_ids.append(s[1] if s else None)
    con.close()

    df = df.with_columns(
        pl.Series("suggested_category", sug_names),
        pl.Series("suggested_category_id", sug_ids),
    )
    df = df.with_columns(
        ((pl.col("current_category") == pl.col("suggested_category")) | pl.col("suggested_category").is_null()).alias(
            "agree"
        ),
        (
            (pl.col("current_category") == "Uncategorized")
            | (
                pl.col("suggested_category").is_not_null()
                & (pl.col("current_category") != pl.col("suggested_category"))
            )
        ).alias("needs_review"),
    )
    return df.select(
        [
            "date",
            "account",
            "payee",
            "amount",
            "current_category",
            "suggested_category",
            "needs_review",
            "agree",
            "current_category_id",
            "suggested_category_id",
            "cleared",
            "approved",
            "flag",
            "memo",
            "id",
            "payee_id",
        ]
    )


@app.cell
def _():
    _df = pending(days=30, history_days=180)
    _n_review = _df.filter(pl.col("needs_review")).height
    mo.vstack(
        [
            mo.md(
                f"**{_df.height}** transactions pending (last 30d). "
                f"**{_n_review}** flagged as `needs_review = true`: either uncategorized, or the "
                f"historical suggestion disagrees with the current category."
            ),
            mo.ui.table(_df, page_size=25, selection=None),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
