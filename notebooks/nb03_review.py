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
    import sys
    from pathlib import Path

    import duckdb
    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb02_ynab_sync import connect

    # Generic name fragments that carry no category signal - dropped from token matching.
    # In app.setup so it's in scope for the @app.function helpers (marimo demotes a bare
    # module-level assignment between cells and it goes undefined at import).
    TOKEN_STOP = frozenset(
        "the of and llc inc ltd co store shop vending sub subscription payments com www "
        "city premium outlet outlets de se us usa new place house bar group services".split()
    )


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


@app.function(hide_code=True)
def suggest_by_tokens(
    con: duckdb.DuckDBPyConnection,
    payee_name: str,
    history_days: int = 365,
    min_support: int = 3,
) -> tuple[str, str, int, str] | None:
    """History-based fallback for a NEW payee that `suggest_category` can't touch.

    The exact-payee suggester is blind to first-time merchants (no payee_id
    history). This keys off the name instead: tokenize it, and for each token
    find the dominant historical category among past payees whose name contains
    that token. Returns (category_name, category_id, support, driver_token) for
    the highest-support token, or None if nothing clears `min_support`.

    ponytail: deliberately a noisy heuristic - rare/coincidental tokens mislead
    ('lab' keys to Restaurants, 'hotel' to Career). The driver_token is returned
    so the human sees WHY and can override. Suggest-then-confirm; never auto-apply.

    Upgrade path (only if this ever goes UNATTENDED, e.g. a scheduled auto-
    categorize with no human glance): swap literal token-overlap for semantic
    nearest-neighbor - embed historical payee names with a small local model
    (sentence-transformers/all-MiniLM-L6-v2, ~22M params, CPU, no API), embed
    the new name, take the closest neighbor's category. Catches 'Topaz Thai' ~
    'Bangkok Garden' with no shared token. Don't bother for ~15 human-reviewed
    rows/month - an in-context LLM (or just eyeballing) is lazier and already in
    the loop. Embeddings earn the torch dependency only when the machine, not a
    person, makes the final call.
    """
    import re

    tokens = {
        t
        for t in re.split(r"[^a-z0-9]+", (payee_name or "").lower())
        if len(t) >= 3 and not t.isdigit() and t not in TOKEN_STOP
    }
    best: tuple[str, str, int, str] | None = None
    for tok in tokens:
        row = con.execute(
            """
            SELECT category_name, ANY_VALUE(category_id) AS cid, COUNT(*) AS n
            FROM transactions
            WHERE NOT deleted
              AND category_name IS NOT NULL AND category_name != 'Uncategorized'
              AND (parent_id IS NULL OR has_splits = FALSE)
              AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
              AND lower(payee_name) LIKE '%' || ? || '%'
              AND date >= current_date - (? * INTERVAL 1 DAY)
            GROUP BY category_name
            ORDER BY n DESC
            LIMIT 1
            """,
            [tok, history_days],
        ).fetchone()
        if row and row[2] >= min_support and (best is None or row[2] > best[2]):
            best = (row[0], row[1], row[2], tok)
    return best


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


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## New-payee suggestions (token fallback)

    The rows above where `suggested_category` is null are first-time merchants -
    exact-payee history is blind to them. `suggest_by_tokens` keys off the name
    instead. Treat `via` as the reason, not a verdict: a generic/rare token can
    mislead (`lab` -> Restaurants). Confirm before sending to `nb04`.
    """)
    return


@app.cell
def _():
    _df = pending(days=30, history_days=180)
    _new = _df.filter(pl.col("suggested_category_id").is_null())
    _con = connect()
    _out = []
    for _r in _new.iter_rows(named=True):
        _s = suggest_by_tokens(_con, _r["payee"])
        _out.append(
            {
                "date": _r["date"],
                "payee": _r["payee"],
                "amount": _r["amount"],
                "token_category": _s[0] if _s else None,
                "support": _s[2] if _s else 0,
                "via": _s[3] if _s else None,
                "id": _r["id"],
            }
        )
    _con.close()
    _tbl = pl.DataFrame(_out) if _out else pl.DataFrame()
    mo.vstack(
        [
            mo.md(
                f"**{len(_out)}** new-payee rows; **{sum(1 for o in _out if o['token_category'])}** got a token suggestion."
            ),
            mo.ui.table(_tbl, page_size=25, selection=None) if _out else mo.md("_(no new-payee rows)_"),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
