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
    import math
    import statistics as _st
    import sys
    from pathlib import Path

    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb01_ynab_client import active_budget_id, get, patch
    from nb02_ynab_sync import connect

    # --- model parameters (validated by data/processed/backtest_anticipation.py) ---
    PCT = 0.75  # recency-weighted seasonal percentile; p75 = knee of buffer-vs-hoard curve
    WINDOW_YEARS = 4  # short window: rising costs (e.g. electricity) catch fast, stale years fade
    LUMPY_COV = 0.70  # year-over-year CoV above this -> flag for a goal, don't trust the percentile

    # Config: category names that get special handling. Generic on purpose (no private data).
    REIMBURSABLE = {"Personal", "Business"}  # fronted-then-reimbursed; not funded from monthly income
    SWEEP_CATEGORY = "Emergency Fund"  # catch-all/staging: the remainder of Ready-to-Assign lands here

    # YNAB built-in groups whose categories are never manually assigned here.
    _EXCLUDE_GROUPS = {"Credit Card Payments", "Internal Master Category"}

    # scheduled-transaction frequency -> occurrences per month (monthly-equivalent bill load)
    PER_MONTH = {
        "daily": 30,
        "weekly": 4.345,
        "everyOtherWeek": 2.17,
        "twiceAMonth": 2,
        "every4Weeks": 1.0857,
        "monthly": 1,
        "everyOtherMonth": 0.5,
        "every3Months": 1 / 3,
        "every4Months": 0.25,
        "twiceAYear": 1 / 6,
        "yearly": 1 / 12,
        "everyOtherYear": 1 / 24,
        "never": 0,
    }

    def _season(tm: int) -> set[int]:
        """Target month +/- 1 (wrapping), the comparable-season set for a percentile."""
        return {((tm - 2) % 12) + 1, tm, (tm % 12) + 1}

    def _spend_cells() -> dict:
        """(category, year, month) -> dollars spent, from the DuckDB cache.

        Spend filters: not deleted, outflow only, non-transfer, split CHILDREN only
        (NOT has_splits drops split parents to avoid double-counting).
        """
        con = connect()
        rows = con.execute(
            """
            SELECT category_name, year(date), month(date), SUM(-amount_milli) / 1000.0
            FROM transactions
            WHERE NOT deleted AND amount_milli < 0 AND NOT has_splits
              AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
              AND category_name IS NOT NULL
              AND year(date) >= year(current_date) - 6
            GROUP BY 1, 2, 3
            """
        ).fetchall()
        con.close()
        return {(r[0], r[1], r[2]): r[3] for r in rows}

    def _scheduled_monthly(bid: str) -> dict:
        """category -> monthly-equivalent scheduled outflow (fixed-bill floor)."""
        out: dict[str, float] = {}
        for s in get(f"/budgets/{bid}/scheduled_transactions")["scheduled_transactions"]:
            if s["deleted"] or s["amount"] >= 0 or not s["category_name"]:
                continue
            out[s["category_name"]] = out.get(s["category_name"], 0.0) + (-s["amount"] / 1000.0) * PER_MONTH.get(
                s["frequency"], 0
            )
        return out

    def _excluded_ids(bid: str) -> set[str]:
        """Category ids in the CC-payment and internal groups (never assigned here)."""
        ids: set[str] = set()
        for g in get(f"/budgets/{bid}/categories")["category_groups"]:
            if g["name"] in _EXCLUDE_GROUPS:
                ids.update(c["id"] for c in g["categories"])
        return ids

    def _seasonal_p(cell: dict, c: str, ty: int, tm: int) -> float:
        """Recency-weighted PCT-percentile over same-season months in a WINDOW_YEARS window."""
        season = _season(tm)
        slots = [(y, mn) for y in range(ty - WINDOW_YEARS + 1, ty + 1) for mn in season if (y, mn) < (ty, tm)]
        pool: list[float] = []
        for y, mn in slots:
            w = max(1, y - (ty - WINDOW_YEARS))  # linear recency weight, recent years heavier
            pool += [cell.get((c, y, mn), 0.0)] * w
        pool.sort()
        return pool[max(0, math.ceil(PCT * len(pool)) - 1)] if pool else 0.0

    def _trailing3(cell: dict, c: str, ty: int, tm: int) -> float:
        """Mean of the 3 calendar months before the target (regime guard for rising costs)."""
        tot = 0.0
        for k in range(1, 4):
            mm, yy = tm - k, ty
            if mm <= 0:
                mm += 12
                yy -= 1
            tot += cell.get((c, yy, mm), 0.0)
        return tot / 3.0

    def _cov(cell: dict, c: str, ty: int, tm: int) -> float:
        """Coefficient of variation of seasonal spend across the 4 complete prior years."""
        season = _season(tm)
        yrs = [sum(cell.get((c, y, mn), 0.0) for mn in season) / len(season) for y in range(ty - 4, ty)]
        mu = _st.mean(yrs) if yrs else 0.0
        return (_st.pstdev(yrs) / mu) if mu > 1 else 0.0


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb07 - Budget assignment planner

    Proposes how to assign a month's Ready-to-Assign across categories, then writes it.

    **Why this exists.** The workflow is "live on last month's income": each month you
    assign the prior month's whole inflow, then anything left over sweeps into a
    catch-all (`SWEEP_CATEGORY`) so Ready to Assign hits `$0`. Doing that by hand is slow;
    this anticipates each category's need so it's a review-and-apply, not a re-derivation.

    **The model** (per category, validated by `data/processed/backtest_anticipation.py`):

    - **goaled** category -> fund its `goal_under_funded` (the goal already encodes intent).
    - **reimbursable** (`REIMBURSABLE`) -> `0`; it's fronted-then-reimbursed, handled elsewhere.
    - **everything else** -> `max(seasonal_p75, trailing-3-month avg, scheduled floor) - available`,
      floored at `0`. The seasonal percentile anticipates typical spend; the trailing-3mo term is
      a regime guard that catches permanent cost rises; the scheduled term floors fixed bills.
    - **LUMPY** categories (high year-over-year variance) are flagged - the percentile guesses on
      project/annual spend, so set a goal there instead.

    The remainder of Ready to Assign sweeps to `SWEEP_CATEGORY`. **Writes are dry-run by default.**
    """)
    return


@app.function(hide_code=True)
def anticipate(month: str) -> pl.DataFrame:
    """Build the proposed assignment plan for `month` ('YYYY-MM-01').

    Returns a polars DataFrame, one row per funded category plus a final
    `SWEEP_CATEGORY` row, where the `assign` column sums to that month's
    `to_be_budgeted` (so applying it drives Ready to Assign to ~0).
    Columns: category, category_id, available, scheduled, p75, trail3,
    goal_uf, assign, basis, lumpy.
    """
    bid = active_budget_id()
    m = get(f"/budgets/{bid}/months/{month}")["month"]
    ty, tm = int(month[:4]), int(month[5:7])
    cell = _spend_cells()
    sched = _scheduled_monthly(bid)
    excl = _excluded_ids(bid)
    pool = m["to_be_budgeted"] / 1000.0

    recs, sweep_id = [], None
    for c in m["categories"]:
        if c["hidden"] or c["deleted"] or c["id"] in excl:
            continue
        if c["name"] == SWEEP_CATEGORY:
            sweep_id = c["id"]
            continue
        avail = c["balance"] / 1000.0
        uf = (c.get("goal_under_funded") or 0) / 1000.0
        sm = sched.get(c["name"], 0.0)
        p = _seasonal_p(cell, c["name"], ty, tm)
        t = _trailing3(cell, c["name"], ty, tm)
        if c["name"] in REIMBURSABLE:
            assign, basis = 0.0, "reimbursable"
        elif c.get("goal_type"):
            assign, basis = uf, "goal"
        else:
            assign, basis = max(0.0, max(p, t, sm) - avail), "guard"
        lumpy = basis == "guard" and _cov(cell, c["name"], ty, tm) > LUMPY_COV
        if assign > 0.005 or sm > 0 or p > 0 or t > 0:
            recs.append(
                {
                    "category": c["name"],
                    "category_id": c["id"],
                    "available": round(avail, 2),
                    "scheduled": round(sm, 2),
                    "p75": round(p, 2),
                    "trail3": round(t, 2),
                    "goal_uf": round(uf, 2),
                    "assign": round(assign, 2),
                    "basis": basis,
                    "lumpy": lumpy,
                }
            )

    df = pl.DataFrame(recs).sort("assign", descending=True)
    sweep = round(pool - float(df["assign"].sum()), 2)
    df = df.vstack(
        pl.DataFrame(
            [
                {
                    "category": SWEEP_CATEGORY,
                    "category_id": sweep_id,
                    "available": None,
                    "scheduled": None,
                    "p75": None,
                    "trail3": None,
                    "goal_uf": None,
                    "assign": sweep,
                    "basis": "sweep",
                    "lumpy": False,
                }
            ],
            schema=df.schema,
        )
    )
    return df


@app.function(hide_code=True)
def assign(plan: pl.DataFrame, month: str, dry_run: bool = True) -> dict:
    """Apply the plan: set each category's `budgeted` for `month`.

    `budgeted` is set to (current budgeted + row.assign) - i.e. the plan TOPS UP
    to the anticipated need (the `assign` column was computed net of current
    available). Intended as a once-per-month assign. Dry-run by default: prints
    what it would set. Budget assignments do not touch the transactions cache,
    so nb02.sync() is not called; the month is re-fetched to confirm.
    """
    bid = active_budget_id()
    m = get(f"/budgets/{bid}/months/{month}")["month"]
    cur = {c["id"]: c["budgeted"] for c in m["categories"]}
    edits = []
    for row in plan.iter_rows(named=True):
        a = row["assign"]
        if a is None or a <= 0.005 or not row["category_id"]:
            continue
        cid = row["category_id"]
        new_budgeted = cur.get(cid, 0) + round(a * 1000)
        edits.append(
            {
                "category": row["category"],
                "category_id": cid,
                "from": round(cur.get(cid, 0) / 1000, 2),
                "to": round(new_budgeted / 1000, 2),
                "_new": new_budgeted,
            }
        )

    if dry_run:
        return {
            "dry_run": True,
            "month": month,
            "n": len(edits),
            "preview": [{k: e[k] for k in ("category", "from", "to")} for e in edits[:10]],
            "message": f"DRY RUN - would set budgeted on {len(edits)} categories for {month}; pass dry_run=False to apply.",
        }

    for e in edits:
        patch(f"/budgets/{bid}/months/{month}/categories/{e['category_id']}", {"category": {"budgeted": e["_new"]}})
    after = get(f"/budgets/{bid}/months/{month}")["month"]
    return {
        "applied": len(edits),
        "dry_run": False,
        "month": month,
        "to_be_budgeted_after": round(after["to_be_budgeted"] / 1000, 2),
    }


@app.cell
def _():
    # Target month = the month you are funding. Default: the month after the API's
    # "current" (the one-month lag - fund next month from last month's income).
    _bid = active_budget_id()
    _cur = get(f"/budgets/{_bid}/months/current")["month"]["month"]  # 'YYYY-MM-01'
    _y, _m = int(_cur[:4]), int(_cur[5:7]) + 1
    if _m > 12:
        _y, _m = _y + 1, 1
    TARGET = f"{_y:04d}-{_m:02d}-01"
    plan = anticipate(TARGET)
    return TARGET, plan


@app.cell
def _(TARGET, plan):
    _assigned = float(plan.filter(pl.col("basis") != "sweep")["assign"].sum())
    _sweep = float(plan.filter(pl.col("basis") == "sweep")["assign"].sum())
    _lumpy = plan.filter(pl.col("lumpy"))["category"].to_list()
    mo.vstack(
        [
            mo.md(
                f"### Proposed plan for {TARGET}\n\n"
                f"- **${_assigned:,.0f}** anticipated across categories\n"
                f"- **${_sweep:,.0f}** sweeps to **{SWEEP_CATEGORY}** "
                f"({'Ready to Assign -> $0' if _sweep >= -0.005 else 'SHORTFALL - needs exceed the pool'})\n"
                + (f"- LUMPY (set a goal instead of trusting the percentile): {', '.join(_lumpy)}\n" if _lumpy else "")
                + "\nEdit `overrides` below for any row your judgment beats the model on, then dry-run `assign`."
            ),
            mo.ui.table(plan, page_size=40, selection=None),
        ]
    )
    return


@app.cell
def _(plan):
    # Manual overrides: category_id -> assign-dollars. Your knowledge beats the model on
    # lumpy/seasonal/one-off rows (e.g. a summer camp, a planned project). Empty = none.
    overrides: dict[str, float] = {
        # "<category-id>": 255.0,  # short note about why
    }

    if overrides:
        plan_final = plan.with_columns(
            pl.when(pl.col("category_id").is_in(list(overrides)))
            .then(pl.col("category_id").replace_strict(overrides, default=None, return_dtype=pl.Float64))
            .otherwise(pl.col("assign"))
            .alias("assign")
        )
        # re-sweep so the remainder still zeroes Ready to Assign
        _pool = float(plan["assign"].sum())
        _nonsweep = float(plan_final.filter(pl.col("basis") != "sweep")["assign"].sum())
        plan_final = plan_final.with_columns(
            pl.when(pl.col("basis") == "sweep")
            .then(pl.lit(round(_pool - _nonsweep, 2)))
            .otherwise(pl.col("assign"))
            .alias("assign")
        )
    else:
        plan_final = plan
    plan_final
    return (plan_final,)


@app.cell
def _(TARGET, plan_final):
    # Apply. Dry-run by default. Flip to dry_run=False to actually set the budget.
    apply_result = assign(plan_final, TARGET, dry_run=True)
    apply_result
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## To extend

    - **Graduate the LUMPY flags into goals.** For each flagged category, set a YNAB
      target (annual_total / 12) so it funds as a sinking fund instead of a percentile guess.
    - **Reimbursable float.** Fund `REIMBURSABLE` categories from the outstanding-reimbursement
      set (the ~$8.8k owed) rather than deferring to the sweep.
    - **Seasonal-shape drift.** Electricity inverted its seasonal peak; when the trailing-12
      profile decorrelates from the historical seasonal index, fall back to a trailing-12 level.
    """)
    return


if __name__ == "__main__":
    app.run()
