# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "altair==6.2.2",
#     "duckdb",
#     "marimo",
#     "numpy",
#     "polars",
#     "pyarrow",
#     "requests",
# ]
# ///

import marimo

__generated_with = "0.23.11"
app = marimo.App(width="medium")

with app.setup:
    import sys
    from pathlib import Path

    import altair as alt
    import marimo as mo
    import numpy as np
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb01_ynab_client import active_budget_id, get
    from nb02_ynab_sync import connect

    # Category burn decomposition. Name-free and public: no category, group, or bank names are
    # hardcoded - every label comes from the cache at runtime. Two robustness rules, for two
    # different lumpiness problems:
    #   - CATEGORIES use amort_monthly = window outflow / window_months. This spreads a real but
    #     infrequent bill (an annual insurance premium) into its true monthly-equivalent, so the
    #     floor counts it without spiking the month it lands.
    #   - DEBT SERVICE uses the MEDIAN monthly payment per loan. A one-time paydown/refinance is not
    #     a recurring obligation, so the median (the regular installment) is right and the mean/amort
    #     would massively overstate it.
    # Debt service is transfers OUT to a liability account (mortgage/auto/student/personal loan). It
    # never shows as a category, so a category-only burn silently omits the mortgage - the single
    # biggest committed cost. Credit-card payments are excluded: that spend is already itemized in the
    # categories, so counting the payment too would double-count.
    # The transactions cache has no group or account-type column, so ynab_meta() fetches the
    # category->group map and the liability-account set from the API ONCE, stores them in DuckDB
    # (tables cat_group/debt_acct), and every run after reads the cache - cache-first, no rate limit.
    #
    # net_ratio = |net| / gross_outflow on a category. Low => PASS-THROUGH (reimbursable/escrow):
    # money in ~ money out, not real burn. (Under-detects when the refund leaks to income instead of
    # back to the category - that's why those are shown, not silently dropped.)

    # YNAB account types that are liabilities -> a transfer to one is debt service.
    LIABILITY_TYPES = (
        "mortgage",
        "autoLoan",
        "studentLoan",
        "personalLoan",
        "medicalDebt",
        "otherDebt",
        "lineOfCredit",
        "otherLiability",
    )
    # Categories that are never real burn (transfers, income, savings sinks, bookkeeping).
    EXCLUDE_CATEGORIES = (
        "Uncategorized",
        "Inflow: Ready to Assign",
        "Deferred Income SubCategory",
        "Investment",
    )


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb11 - Category burn & what's affordable

    Breaks your monthly **burn** into categories plus **debt service**, splits it into a
    **committed floor** (bills and loans you can't easily cut) and a **flexible cushion** (your trim
    room), and lets you **drill into any category** to see what's driving it. Then it grounds the
    affordability question - *if an income changes, what must a contribution cover?* - in that detail
    instead of one top-line number.

    Two things most category breakdowns get wrong, handled here: **debt service** (mortgage, car, and
    student loans are transfers, not categories - omit them and the floor is missing its biggest piece)
    and **lumpiness** (an annual insurance bill is amortized in; a one-time loan paydown is taken at the
    median so it doesn't masquerade as a monthly obligation).

    The committed/flexible split is **data-driven and tunable**: regularity isn't the same as
    necessity, so the auto-split is a **starting guess** - the knobs move any category between
    committed, flexible, and excluded.
    """)
    return


@app.function
def debt_payees() -> set[str]:
    """Liability "Transfer : <loan>" payee set. Cache-first via ynab_meta."""
    ynab_meta()
    con = connect()
    out = {r[0] for r in con.execute("SELECT payee FROM debt_acct").fetchall()}
    con.close()
    return out


@app.function(hide_code=True)
def operating_flow(window_months: int = 12) -> float:
    """Median monthly operating cash flow (nb10's measure): every leg on the LIQUID accounts at
    split-leaf level, excluding category='Investment'. Cache-first (liquid set from ynab_meta). The
    robust read of whether the household runs above or below water month to month. NaN if no liquid
    accounts are known."""
    liquid = liquid_accounts()
    if not liquid:
        return float("nan")
    con = connect()
    rows = con.execute(
        """
        SELECT date_trunc('month', date) AS m, SUM(amount_milli) / 1000.0 AS net
        FROM transactions
        WHERE NOT deleted AND has_splits = FALSE
          AND account_name IN ?
          AND COALESCE(category_name, '') <> 'Investment'
          AND date < date_trunc('month', current_date)
        GROUP BY 1 ORDER BY 1
        """,
        [list(liquid)],
    ).fetchall()
    con.close()
    flows = np.array([float(r[1]) for r in rows][-window_months:])
    return float(np.median(flows)) if len(flows) else float("nan")


@app.function(hide_code=True)
def liquid_accounts() -> set[str]:
    """Checking/savings/cash account names (for operating cash flow). Cache-first via ynab_meta."""
    ynab_meta()
    con = connect()
    out = {r[0] for r in con.execute("SELECT name FROM liquid_acct").fetchall()}
    con.close()
    return out


@app.function(hide_code=True)
def ynab_meta(refresh: bool = False) -> None:
    """Ensure the cached metadata tables exist - the only structure the transactions cache lacks:
    cat_group (category->group), debt_acct (liability "Transfer :" payees), liquid_acct
    (checking/savings/cash account names). Classified by account TYPE, so no names are hardcoded.
    Fetched from the API ONCE and stored in DuckDB; every run after reads the cache, so the notebook
    is cache-first and never trips the rate limit. refresh=True re-pulls (after adding accounts/cats)."""
    con = connect()
    _have = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('cat_group', 'debt_acct', 'liquid_acct')"
    ).fetchone()[0]
    if _have == 3 and not refresh:
        con.close()
        return
    bid = active_budget_id()
    accts = [a for a in get(f"/budgets/{bid}/accounts")["accounts"] if not a["closed"] and not a["deleted"]]
    cat_groups = get(f"/budgets/{bid}/categories")["category_groups"]
    groups = {
        c["name"]: g["name"]
        for g in cat_groups
        if not g["deleted"] and not g["hidden"]
        for c in g["categories"]
        if not c["deleted"] and not c["hidden"]
    }
    debt = {f"Transfer : {a['name']}" for a in accts if a["type"] in LIABILITY_TYPES}
    liquid = {a["name"] for a in accts if a["type"] in ("checking", "savings", "cash")}
    con.execute("CREATE OR REPLACE TABLE cat_group (category VARCHAR, grp VARCHAR)")
    if groups:
        con.executemany("INSERT INTO cat_group VALUES (?, ?)", list(groups.items()))
    con.execute("CREATE OR REPLACE TABLE debt_acct (payee VARCHAR)")
    if debt:
        con.executemany("INSERT INTO debt_acct VALUES (?)", [(p,) for p in debt])
    con.execute("CREATE OR REPLACE TABLE liquid_acct (name VARCHAR)")
    if liquid:
        con.executemany("INSERT INTO liquid_acct VALUES (?)", [(n,) for n in liquid])
    con.close()


@app.function
def category_burn(window_months: int = 12) -> pl.DataFrame:
    """Per-category burn signals over the trailing window (current partial month dropped).

    One row per category with any outflow. amort_monthly = window outflow / window (annual lumps
    amortize), months_active, cv (monthly-outflow variability), gross, net, net_ratio (|net|/gross;
    low => pass-through). Cache-only, name-free. Strict has_splits=FALSE so capital hidden inside a
    split reads by its real leaf category. Transfers excluded here - debt service is added separately.
    """
    con = connect()
    df = con.execute(
        """
        WITH mo AS (
          SELECT date_trunc('month', date) AS m, category_name AS cat,
                 SUM(CASE WHEN amount_milli < 0 THEN -amount_milli ELSE 0 END) / 1000.0 AS gout,
                 SUM(amount_milli) / 1000.0 AS net
          FROM transactions
          WHERE NOT deleted AND has_splits = FALSE AND category_name IS NOT NULL
            AND category_name NOT IN ?
            AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
            AND date >= date_trunc('month', current_date) - (? * INTERVAL 1 MONTH)
            AND date <  date_trunc('month', current_date)
          GROUP BY 1, 2
        )
        SELECT cat AS category,
               SUM(gout) / ?::DOUBLE AS amort_monthly,
               COUNT(*) FILTER (WHERE gout > 0) AS months_active,
               COALESCE(stddev_pop(gout) / NULLIF(avg(gout), 0), 0) AS cv,
               SUM(gout) AS gross,
               SUM(net) AS net,
               ABS(SUM(net)) / NULLIF(SUM(gout), 0) AS net_ratio
        FROM mo
        GROUP BY cat
        HAVING SUM(gout) > 0
        ORDER BY amort_monthly DESC
        """,
        [list(EXCLUDE_CATEGORIES), window_months, window_months],
    ).pl()
    con.close()
    return df


@app.function
def debt_service(window_months: int = 12) -> pl.DataFrame:
    """Median monthly payment per loan over the window. Shaped like category_burn so it merges into
    the committed floor: `category` = the loan name (the "Transfer : " prefix stripped), bucket fixed
    to 'debt'. Median (not amort) so a one-time paydown/refinance doesn't inflate the recurring
    obligation. Returns empty if there are no liability accounts."""
    payees = debt_payees()
    if not payees:
        return pl.DataFrame(schema={"category": pl.String, "amort_monthly": pl.Float64, "months_active": pl.Int64})
    con = connect()
    df = con.execute(
        """
        WITH mo AS (
          SELECT payee_name AS p, date_trunc('month', date) AS m,
                 SUM(-amount_milli) / 1000.0 AS gout
          FROM transactions
          WHERE NOT deleted AND has_splits = FALSE AND amount_milli < 0
            AND payee_name IN ?
            AND date >= date_trunc('month', current_date) - (? * INTERVAL 1 MONTH)
            AND date <  date_trunc('month', current_date)
          GROUP BY 1, 2
        )
        SELECT regexp_replace(p, '^Transfer : ', '') AS category,
               median(gout) AS amort_monthly,
               COUNT(*) AS months_active
        FROM mo
        GROUP BY p
        HAVING median(gout) > 0
        ORDER BY amort_monthly DESC
        """,
        [list(payees), window_months],
    ).pl()
    con.close()
    return df.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("cv"),
        pl.lit(None, dtype=pl.Float64).alias("net_ratio"),
        pl.lit("debt").alias("bucket"),
    )


@app.function
def classify(
    df: pl.DataFrame,
    cv_max: float = 0.5,
    passthru_max: float = 0.35,
    min_active: int = 6,
    overrides: dict | None = None,
) -> pl.DataFrame:
    """Add a `bucket` column to category rows by a transparent default rule, then apply overrides.

    Default: net_ratio < passthru_max => 'pass-through'; else 'committed' if it recurs
    (months_active >= min_active) AND is predictable (cv <= cv_max); else 'flexible'. `overrides`
    maps a category name to a forced bucket ('committed' | 'flexible' | 'exclude') - where you encode
    the value calls the data can't make (a regular restaurant habit looks committed but is cuttable).
    Pure.
    """
    auto = (
        pl.when(pl.col("net_ratio") < passthru_max)
        .then(pl.lit("pass-through"))
        .when((pl.col("months_active") >= min_active) & (pl.col("cv") <= cv_max))
        .then(pl.lit("committed"))
        .otherwise(pl.lit("flexible"))
    )
    out = df.with_columns(auto.alias("bucket"))
    if overrides:
        out = out.with_columns(
            pl.col("category").replace_strict(overrides, default=None).fill_null(pl.col("bucket")).alias("bucket")
        )
    return out


@app.function
def drill(category: str, window_months: int = 12) -> dict:
    """What drives one category: top payees by spend + a DENSE monthly outflow series (every month in
    the window, zeros filled) so a sporadic category's empty months show honestly. Cache-only."""
    con = connect()
    payees = con.execute(
        """
        SELECT COALESCE(payee_name, '(unnamed)') AS payee,
               COUNT(*) AS n_txns,
               ROUND(SUM(-amount_milli) / 1000.0, 2) AS spent,
               MIN(date) AS first_seen, MAX(date) AS last_seen
        FROM transactions
        WHERE NOT deleted AND has_splits = FALSE AND category_name = ?
          AND amount_milli < 0 AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
          AND date >= date_trunc('month', current_date) - (? * INTERVAL 1 MONTH)
        GROUP BY 1 ORDER BY spent DESC LIMIT 15
        """,
        [category, window_months],
    ).pl()
    trend = con.execute(
        """
        WITH spine AS (
            SELECT date_trunc('month', current_date) - (n * INTERVAL 1 MONTH) AS month
            FROM generate_series(1, ?) t(n)
        ),
        spend AS (
            SELECT date_trunc('month', date) AS month, SUM(-amount_milli) / 1000.0 AS outflow
            FROM transactions
            WHERE NOT deleted AND has_splits = FALSE AND category_name = ?
              AND amount_milli < 0 AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
              AND date >= date_trunc('month', current_date) - (? * INTERVAL 1 MONTH)
              AND date <  date_trunc('month', current_date)
            GROUP BY 1
        )
        SELECT s.month, ROUND(COALESCE(sp.outflow, 0), 2) AS outflow
        FROM spine s LEFT JOIN spend sp USING (month)
        ORDER BY s.month
        """,
        [window_months, category, window_months],
    ).pl()
    con.close()
    return {"payees": payees, "trend": trend}


@app.function
def category_groups() -> dict:
    """{category_name: group_name}. Cache-first via ynab_meta."""
    ynab_meta()
    con = connect()
    out = dict(con.execute("SELECT category, grp FROM cat_group").fetchall())
    con.close()
    return out


@app.function
def monthly_burn(window_months: int = 12) -> pl.DataFrame:
    """Total categorized outflow per month - the real burn SHAPE the amortized figure averages away.
    Transfers and bookkeeping excluded; debt service (~flat) sits on top. Reveals seasonal bills (a
    winter heating spike) so you can budget for the peak month, not the average."""
    con = connect()
    df = con.execute(
        """
        SELECT date_trunc('month', date) AS month,
               ROUND(SUM(CASE WHEN amount_milli < 0 THEN -amount_milli ELSE 0 END) / 1000.0, 0) AS burn
        FROM transactions
        WHERE NOT deleted AND has_splits = FALSE AND category_name IS NOT NULL
          AND category_name NOT IN ?
          AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
          AND date >= date_trunc('month', current_date) - (? * INTERVAL 1 MONTH)
          AND date <  date_trunc('month', current_date)
        GROUP BY 1 ORDER BY 1
        """,
        [list(EXCLUDE_CATEGORIES), window_months],
    ).pl()
    con.close()
    return df


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Who covers what
    One slider per item = **the share the contributor covers**, 0-100%. Debt and bills are included;
    reimbursable categories are auto-excluded. The total contribution is just the sum of
    (their % x each item's cost) - so you can be specific: split the mortgage, cover none of the dining out.
    """)
    return


@app.cell
def _():
    window = mo.ui.number(start=6, stop=36, step=3, value=12, label="Window (months)")
    window
    return (window,)


@app.cell
def _(window):
    burn = category_burn(window_months=int(window.value))
    debt = debt_service(window_months=int(window.value))
    return burn, debt


@app.cell
def _(burn, debt):
    # Per-item slider = % of this item the CONTRIBUTOR covers. Debt included. Seeded as a starting
    # suggestion (necessities 50, discretionary 0). Reimbursable auto-excluded.
    _groups = category_groups()
    _auto = classify(burn, cv_max=0.5)
    _cat = _auto.filter(pl.col("bucket").is_in(["committed", "flexible"])).with_columns(
        pl.col("category").replace_strict(_groups, default="(ungrouped)").alias("group"),
        pl.when(pl.col("bucket") == "committed").then(50).otherwise(0).alias("default_cov"),
    )
    _dbt = debt.with_columns(
        pl.lit("Debt service").alias("group"),
        pl.lit(50).alias("default_cov"),
    )
    _cols0 = ["category", "group", "amort_monthly", "months_active", "cv", "net_ratio", "default_cov"]
    slidable = pl.concat([_cat.select(_cols0), _dbt.select(_cols0)], how="vertical").sort(
        "amort_monthly", descending=True
    )
    coverage = mo.ui.array(
        [
            mo.ui.slider(steps=[0, 25, 50, 75, 100], value=int(v), show_value=True)
            for v in slidable["default_cov"].to_list()
        ]
    )
    _rows = [
        mo.hstack(
            [_s, mo.md(f"**{_r['group']}: {_r['category']}** - \\${_r['amort_monthly']:,.0f}/mo")],
            justify="start",
            align="center",
            gap=0.5,
        )
        for _s, _r in zip(coverage, slidable.iter_rows(named=True))
    ]
    _pass = [
        f"{_groups.get(c, '(ungrouped)')}: {c}"
        for c in _auto.filter(pl.col("bucket") == "pass-through")["category"].to_list()
    ]
    mo.vstack(
        [
            mo.md(
                "Slide each item to **the % the contributor covers** (100 = they pay it all, 0 = you do). Suggested start: necessities 50%, discretionary 0%."
            ),
            *_rows,
            mo.md(f"_Auto-excluded as pass-through (reimbursable, nets ~0): {', '.join(_pass) or 'none'}._"),
        ]
    )
    return coverage, slidable


@app.cell
def _(coverage, slidable):
    _cov = dict(zip(slidable["category"].to_list(), coverage.value))
    full = slidable.with_columns(
        pl.col("category").replace_strict(_cov).cast(pl.Float64).alias("coverage_pct")
    ).with_columns(
        (pl.col("amort_monthly") * pl.col("coverage_pct") / 100).alias("contrib_amt"),
        (pl.col("amort_monthly") * (1 - pl.col("coverage_pct") / 100)).alias("your_amt"),
        pl.when(pl.col("group") == "Debt service").then(pl.lit("debt")).otherwise(pl.lit("spend")).alias("bucket"),
    )
    return (full,)


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## The breakdown
    """)
    return


@app.cell
def _(full):
    _show = full.select(
        "category",
        "group",
        pl.col("amort_monthly").round(0).alias("$/mo"),
        pl.col("coverage_pct").round(0).alias("they cover %"),
        pl.col("contrib_amt").round(0).alias("they pay $"),
        pl.col("your_amt").round(0).alias("you pay $"),
    ).sort("$/mo", descending=True)
    mo.ui.table(_show, page_size=45, selection=None)
    return


@app.cell
def _(full):
    _contrib = float(full["contrib_amt"].sum())
    _yours = float(full["your_amt"].sum())
    _total = _contrib + _yours
    _share = (_contrib / _total * 100) if _total else 0
    mo.md(
        f"### Who pays what (amortized monthly)\n\n"
        f"- **Contributor pays:** \\${_contrib:,.0f}/mo ({_share:.0f}% of the \\${_total:,.0f} burn).\n"
        f"- **You pay:** \\${_yours:,.0f}/mo.\n\n"
        f"_Set per item above; reimbursable excluded (nets ~0)._"
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Drill into a category
    """)
    return


@app.cell
def _(burn):
    pick = mo.ui.dropdown(options=burn["category"].to_list(), value=burn["category"][0], label="Category")
    pick
    return (pick,)


@app.cell
def _(pick, window):
    _d = drill(pick.value, window_months=int(window.value))
    _chart = (
        alt.Chart(_d["trend"], height=220)
        .mark_bar(color="#4c78a8")
        .encode(
            x=alt.X("month:T", title=None, axis=alt.Axis(format="%b %y"), scale=alt.Scale(nice=False)),
            y=alt.Y("outflow:Q", title="$/mo"),
            tooltip=[alt.Tooltip("month:T", title="month"), alt.Tooltip("outflow:Q", title="$", format=",.0f")],
        )
        .properties(title=f"{pick.value} - monthly spend ({int(window.value)}mo)")
    )
    mo.vstack(
        [
            _chart,
            mo.md(f"**{pick.value} - top payees ({int(window.value)}mo)**"),
            mo.ui.table(_d["payees"], page_size=15, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## What's reasonable to contribute?

    Grounds nb10's breakeven in the floor/cushion split: a contribution has to cover the **mandatory
    floor** (debt + committed bills) minus whatever income survives; the **flexible cushion** is the
    most you could trim before touching the floor.
    """)
    return


@app.cell
def _():
    retained_income = mo.ui.number(start=0, stop=40000, step=250, value=0, label="Monthly income that survives ($)")
    retained_income
    return (retained_income,)


@app.cell
def _(full, retained_income, window):
    _contrib = float(full["contrib_amt"].sum())
    _yours = float(full["your_amt"].sum())
    _total = _contrib + _yours
    _ret = retained_income.value
    _net = _ret - _yours
    _verdict = "**you're covered**" if _net >= 0 else f"you're **\\${-_net:,.0f}/mo short**"
    _opcf = operating_flow(int(window.value))
    _be = "near breakeven" if abs(_opcf) < 1500 else ("a surplus" if _opcf > 0 else "a deficit")
    mo.md(
        f"### Does it work for you?\n\n"
        f"- Contributor pays **\\${_contrib:,.0f}/mo**; your share is **\\${_yours:,.0f}/mo** of the \\${_total:,.0f} burn.\n"
        f"- With **\\${_ret:,.0f}/mo** of your income surviving: {_verdict} (net **\\${_net:+,.0f}/mo**).\n\n"
        f"_Reimbursable excluded (nets ~0). Over your {int(window.value)}-month window the household's median "
        f"operating cash flow is **\\${_opcf:+,.0f}/mo** ({_be}), so your share needs real income behind it._"
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Who pays, by group
    The contributor-vs-you split rolled up by your YNAB groups (loans as "Debt service").
    """)
    return


@app.cell
def _(full):
    by_group = (
        full.group_by("group")
        .agg(
            pl.col("contrib_amt").sum().round(0).alias("they pay $"),
            pl.col("your_amt").sum().round(0).alias("you pay $"),
            pl.col("amort_monthly").sum().round(0).alias("total $"),
        )
        .sort("total $", descending=True)
    )
    mo.ui.table(by_group, page_size=15, selection=None)
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Seasonality - budget for the peak, not the average
    """)
    return


@app.cell
def _(window):
    mb = monthly_burn(window_months=int(window.value))
    _peak = float(mb["burn"].max())
    _med = float(mb["burn"].median())
    mo.vstack(
        [
            mo.md(
                f"Categorized burn by month (transfers excluded; debt service ~flat on top). "
                f"Median month **\\${_med:,.0f}**, **peak \\${_peak:,.0f}** - a squeeze has to survive the "
                f"peak (\\${_peak - _med:,.0f} above median), which the amortized floor averages away."
            ),
            mo.ui.table(mb, page_size=18, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## To extend

    - **Per-category trim**: trim each flexible category by its own %, not one global knob - some
      cushions (a subscription) cut to zero, others (groceries you tagged flexible) only partway.
    - **Income-side detail**: break income by source the way burn is broken by category, so you can see
      which paycheck covers the floor vs the cushion (pairs with nb08's paystub data).
    - **Read-only cache mode**: these analysis notebooks open the DuckDB cache read-write, taking an
      exclusive lock, so a second notebook (or a leftover kernel) blocks on it. A read-only connection
      for read-only notebooks would let them run alongside a sync. (ponytail: shared `connect()` change,
      do it when the lock contention recurs.)
    """)
    return


if __name__ == "__main__":
    app.run()
