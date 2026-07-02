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

    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb01_ynab_client import active_budget_id, patch
    from nb02_ynab_sync import connect, sync
    from nb03_review import payee_top_categories, pending


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb04 - Bulk edit

    Apply category fixes to YNAB transactions in batches. Two layers:

    - **Auto mode** - take `nb03.pending()` rows where the historical
      suggestion disagrees with the current category, build a patch list,
      apply via the YNAB bulk PATCH endpoint.
    - **Plan mode is the default** - functions return the proposed edits as
      a polars DataFrame. Nothing is sent to YNAB until you call `apply()`
      with `dry_run=False`.

    After applying, the local cache is brought back in sync via
    `nb02.sync()`.
    """)
    return


@app.function(hide_code=True)
def propose_apply_suggestions(
    days: int = 30,
    history_days: int = 180,
    min_history: int = 2,
) -> pl.DataFrame:
    """Build a patch plan from nb03.pending().

    Includes only rows where:
      - needs_review is true (current != suggested, or current is Uncategorized)
      - the suggestion is non-null (we have payee history to draw from)
      - that suggestion appeared at least `min_history` times historically
    """
    df = pending(days=days, history_days=history_days)
    df = df.filter(pl.col("needs_review") & pl.col("suggested_category_id").is_not_null())

    # Filter on min_history: re-look up payee history count
    con = connect()
    keep = []
    for row in df.iter_rows(named=True):
        tops = payee_top_categories(con, row["payee_id"], days=history_days)
        top_count = tops[0][2] if tops else 0
        keep.append(top_count >= min_history)
    con.close()

    df = df.with_columns(pl.Series("strong_enough", keep))
    df = df.filter(pl.col("strong_enough"))

    return df.select(
        [
            "id",
            "date",
            "account",
            "payee",
            "amount",
            "current_category",
            "suggested_category",
            "current_category_id",
            "suggested_category_id",
            "memo",
        ]
    )


@app.function(hide_code=True)
def apply_edits(plan: pl.DataFrame, dry_run: bool = True) -> dict:
    """Send the plan to YNAB via the bulk transactions PATCH endpoint.

    `plan` must have columns `id` and `suggested_category_id`. Each row
    becomes a {id, category_id} entry in the request body.

    Default is dry-run: prints the body it WOULD send and returns counts.
    Pass dry_run=False to actually mutate. After a non-dry-run, calls
    nb02.sync() to refresh the local cache.
    """
    if plan.height == 0:
        return {"applied": 0, "dry_run": dry_run, "message": "empty plan"}

    body = {
        "transactions": [
            {"id": row["id"], "category_id": row["suggested_category_id"]} for row in plan.iter_rows(named=True)
        ]
    }

    if dry_run:
        preview = body["transactions"][:5]
        return {
            "dry_run": True,
            "n": len(body["transactions"]),
            "preview": preview,
            "message": f"DRY RUN - would patch {len(body['transactions'])} txns; pass dry_run=False to apply.",
        }

    bid = active_budget_id()
    resp = patch(f"/budgets/{bid}/transactions", body)
    sync_result = sync(bid)
    return {
        "applied": len(body["transactions"]),
        "dry_run": False,
        "duplicate_import_ids": resp.get("duplicate_import_ids", []),
        "ynab_server_knowledge": resp.get("server_knowledge"),
        "post_sync": sync_result,
    }


@app.cell
def _():
    plan = propose_apply_suggestions(days=30, min_history=2)
    return (plan,)


@app.cell
def _(plan):
    # Manual overrides: txn_id -> category_id
    # The naive payee-suggestion misses cases like a Mac Mini purchase showing as
    # Apple's recurring iCloud category. Set overrides here. Empty dict = no overrides.
    overrides: dict[str, str] = {
        # "<txn-id>": "<category-id>",  # short note about why
    }

    if overrides:
        plan_final = (
            plan.with_columns(
                pl.col("id")
                .map_elements(lambda i: overrides.get(i, None), return_dtype=pl.String)
                .alias("_override_cid"),
            )
            .with_columns(
                pl.when(pl.col("_override_cid").is_not_null())
                .then(pl.col("_override_cid"))
                .otherwise(pl.col("suggested_category_id"))
                .alias("suggested_category_id"),
            )
            .drop("_override_cid")
        )
    else:
        plan_final = plan
    plan_final
    return (plan_final,)


@app.cell
def _(plan):
    mo.vstack(
        [
            mo.md(
                f"### Proposed plan\n\n"
                f"**{plan.height}** edits would be applied if you ran "
                "`apply_edits(plan, dry_run=False)`."
            ),
            mo.ui.table(plan, page_size=25, selection=None)
            if plan.height
            else mo.md("_(empty plan - nothing to apply)_"),
        ]
    )
    return


@app.cell
def _(plan_final):
    # Apply. Default is dry_run=True. Flip to False to actually mutate YNAB.
    # After a real apply, nb02.sync() is called automatically to refresh the cache.
    apply_result = apply_edits(plan_final, dry_run=True)
    apply_result
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## To extend

    - **Approve alongside categorize.** The bulk PATCH can also flip `approved`; add an
      opt-in flag so a confirmed plan clears the review backlog in the same call.
    - **Plan provenance.** Stamp each plan row with the suggester that produced it
      (exact-payee vs token fallback) so a bad apply can be traced to its source.
    - **Undo plan.** Emit the inverse plan (old categories) next to each apply, so a
      mistaken batch can be reverted with the same `apply_edits` machinery.
    """)
    return


if __name__ == "__main__":
    app.run()
