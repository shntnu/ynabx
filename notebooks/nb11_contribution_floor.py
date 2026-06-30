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

__generated_with = "0.23.11"
app = marimo.App(width="medium")

with app.setup:
    import sys
    from pathlib import Path

    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).resolve().parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb01_ynab_client import active_budget_id, get
    from nb02_ynab_sync import connect

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
    EXCLUDE_CATEGORIES = (
        "Uncategorized",
        "Inflow: Ready to Assign",
        "Deferred Income SubCategory",
        "Investment",
    )


@app.function(hide_code=True)
def ynab_meta(refresh: bool = False) -> None:
    """Cache the category groups, liability-transfer payees, and liquid account names."""
    con = connect()
    have = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('cat_group', 'debt_acct', 'liquid_acct')"
    ).fetchone()[0]
    if have == 3 and not refresh:
        con.close()
        return
    bid = active_budget_id()
    accounts = [a for a in get(f"/budgets/{bid}/accounts")["accounts"] if not a["closed"] and not a["deleted"]]
    category_groups_payload = get(f"/budgets/{bid}/categories")["category_groups"]
    groups = {
        category["name"]: group["name"]
        for group in category_groups_payload
        if not group["deleted"] and not group["hidden"]
        for category in group["categories"]
        if not category["deleted"] and not category["hidden"]
    }
    debt = {f"Transfer : {account['name']}" for account in accounts if account["type"] in LIABILITY_TYPES}
    liquid = {account["name"] for account in accounts if account["type"] in ("checking", "savings", "cash")}
    con.execute("CREATE OR REPLACE TABLE cat_group (category VARCHAR, grp VARCHAR)")
    if groups:
        con.executemany("INSERT INTO cat_group VALUES (?, ?)", list(groups.items()))
    con.execute("CREATE OR REPLACE TABLE debt_acct (payee VARCHAR)")
    if debt:
        con.executemany("INSERT INTO debt_acct VALUES (?)", [(payee,) for payee in debt])
    con.execute("CREATE OR REPLACE TABLE liquid_acct (name VARCHAR)")
    if liquid:
        con.executemany("INSERT INTO liquid_acct VALUES (?)", [(name,) for name in liquid])
    con.close()


@app.function(hide_code=True)
def category_groups() -> dict[str, str]:
    """Return category-name to group-name mappings."""
    ynab_meta()
    con = connect()
    groups = dict(con.execute("SELECT category, grp FROM cat_group").fetchall())
    con.close()
    return groups


@app.function(hide_code=True)
def category_costs(window_months: int = 12) -> pl.DataFrame:
    """Monthly-equivalent net and gross categorized costs over complete months."""
    con = connect()
    df = con.execute(
        """
        SELECT category_name AS category,
               SUM(CASE WHEN amount_milli < 0 THEN -amount_milli ELSE 0 END)
                 / 1000.0 / ?::DOUBLE AS monthly_gross,
               GREATEST(0, -SUM(amount_milli) / 1000.0 / ?::DOUBLE) AS monthly_net,
               COUNT(DISTINCT CASE WHEN amount_milli < 0 THEN date_trunc('month', date) END) AS months_active
        FROM transactions
        WHERE NOT deleted AND has_splits = FALSE AND category_name IS NOT NULL
          AND category_name NOT IN ?
          AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
          AND date >= date_trunc('month', current_date) - (? * INTERVAL 1 MONTH)
          AND date < date_trunc('month', current_date)
        GROUP BY category_name
        HAVING SUM(CASE WHEN amount_milli < 0 THEN -amount_milli ELSE 0 END) > 0
        ORDER BY monthly_net DESC
        """,
        [window_months, window_months, list(EXCLUDE_CATEGORIES), window_months],
    ).pl()
    con.close()
    return df


@app.function(hide_code=True)
def debt_service(window_months: int = 12) -> pl.DataFrame:
    """Median monthly cash payment per active liability account."""
    ynab_meta()
    con = connect()
    payees = [row[0] for row in con.execute("SELECT payee FROM debt_acct").fetchall()]
    liquid = [row[0] for row in con.execute("SELECT name FROM liquid_acct").fetchall()]
    if not payees or not liquid:
        con.close()
        return pl.DataFrame(
            schema={
                "category": pl.String,
                "monthly_net": pl.Float64,
                "monthly_gross": pl.Float64,
                "months_active": pl.Int64,
            }
        )
    df = con.execute(
        """
        WITH monthly AS (
          SELECT payee_name AS payee, date_trunc('month', date) AS month,
                 SUM(-amount_milli) / 1000.0 AS payment
          FROM transactions
          WHERE NOT deleted AND has_splits = FALSE AND amount_milli < 0
            AND payee_name IN ? AND account_name IN ?
            AND date >= date_trunc('month', current_date) - (? * INTERVAL 1 MONTH)
            AND date < date_trunc('month', current_date)
          GROUP BY 1, 2
        )
        SELECT regexp_replace(payee, '^Transfer : ', '') AS category,
               MEDIAN(payment) AS monthly_net,
               MEDIAN(payment) AS monthly_gross,
               COUNT(*) AS months_active
        FROM monthly
        GROUP BY payee
        HAVING MEDIAN(payment) > 0
        ORDER BY monthly_net DESC
        """,
        [payees, liquid, window_months],
    ).pl()
    con.close()
    return df


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # Contribution floor

    This notebook answers one question: **what recurring contribution keeps household cash from
    shrinking after an income change?**

    The calculation is deliberately small:

    `observed monthly burn - dependable income that remains - planned cuts + safety margin`

    It does **not** decide what is fair for another person to pay. That is a separate negotiation.
    Category refunds and reimbursements are netted against spending; recurring loan payments are
    added because YNAB records them as transfers rather than categories.
    """)
    return


@app.function
def income_sources(window_months: int = 12) -> pl.DataFrame:
    """Recurring income sources over complete months, with the median as the dependable estimate.

    Sources below $200/month are hidden when they total at most $500/month; otherwise they remain
    visible so a meaningful collection of small incomes is not discarded.
    """
    con = connect()
    df = con.execute(
        """
        WITH monthly AS (
          SELECT COALESCE(payee_name, '(unnamed)') AS source,
                 date_trunc('month', date) AS month,
                 SUM(amount_milli) / 1000.0 AS amount
          FROM transactions
          WHERE NOT deleted AND has_splits = FALSE AND amount_milli > 0
            AND category_name = 'Inflow: Ready to Assign'
            AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
            AND date >= date_trunc('month', current_date) - (? * INTERVAL 1 MONTH)
            AND date < date_trunc('month', current_date)
          GROUP BY 1, 2
        )
        SELECT source,
               COUNT(*) AS months_seen,
               MEDIAN(amount) AS dependable_monthly,
               AVG(amount) AS average_monthly
        FROM monthly
        GROUP BY source
        HAVING COUNT(*) >= GREATEST(3, ? // 2)
        ORDER BY dependable_monthly DESC
        """,
        [window_months, window_months],
    ).pl()
    con.close()
    small = df.filter(pl.col("dependable_monthly") < 200)
    if float(small["dependable_monthly"].sum()) <= 500:
        df = df.filter(pl.col("dependable_monthly") >= 200)
    return df


@app.function
def burn_snapshot(window_months: int = 12) -> dict:
    """Net categorized burn plus recurring debt service, expressed as monthly equivalents."""
    groups = category_groups()
    categories = category_costs(window_months).with_columns(
        pl.col("category").replace_strict(groups, default="(ungrouped)").alias("group"),
        pl.lit("category").alias("kind"),
    )
    debt = debt_service(window_months).with_columns(
        pl.lit("Debt service").alias("group"),
        pl.lit("debt").alias("kind"),
    )
    columns = ["category", "group", "kind", "monthly_net", "monthly_gross", "months_active"]
    items = pl.concat([categories.select(columns), debt.select(columns)], how="vertical").sort(
        "monthly_net", descending=True
    )
    return {
        "window_months": window_months,
        "items": items,
        "monthly_burn": float(items["monthly_net"].sum()),
    }


@app.cell
def _():
    snapshots = {months: burn_snapshot(months) for months in (6, 12, 24)}
    primary = snapshots[12]
    incomes = income_sources(12)
    return incomes, primary, snapshots


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## 1. Inspect the burn

    The 12-month column is the primary estimate. `net $/mo` is what remained after refunds and
    reimbursements within that category. The table makes the historical spending assumption visible;
    nothing is automatically called mandatory or discretionary.
    """)
    return


@app.cell
def _(primary):
    fair_items = primary["items"].select(
        "category",
        "group",
        "kind",
        "monthly_net",
        pl.col("monthly_net").round(0).alias("net $/mo"),
        pl.col("monthly_gross").round(0).alias("gross $/mo"),
        "months_active",
    )
    share_pct = mo.ui.slider(
        steps=[0, 25, 33, 50, 67, 75, 100],
        value=50,
        label="Contributor share of selected costs (%)",
        show_value=True,
    )
    coverage_pct = mo.ui.slider(
        start=0,
        stop=100,
        step=5,
        value=80,
        label="Burn coverage shown (%)",
        show_value=True,
    )
    mo.vstack(
        [
            mo.md(
                f"Observed 12-month monthly-equivalent burn: **\\${primary['monthly_burn']:,.0f}/mo**.\n\n"
                "Choose how much cumulative burn to show, then select only costs the contributor should "
                "fairly share. Changing burn coverage clears the selections."
            ),
            mo.hstack([share_pct, coverage_pct], justify="start", gap=2),
        ]
    )
    return coverage_pct, fair_items, share_pct


@app.cell(hide_code=True)
def _(coverage_pct, fair_items):
    _total = float(fair_items["monthly_net"].sum())
    _target = float(coverage_pct.value) / 100 * _total
    _running = 0.0
    _top_count = 0
    if coverage_pct.value >= 100:
        _top_count = fair_items.height
    elif coverage_pct.value > 0:
        for _value in fair_items["monthly_net"]:
            _running += float(_value)
            _top_count += 1
            if _running >= _target:
                break
    _visible = fair_items.head(_top_count)
    _covered = float(_visible["monthly_net"].sum()) / _total * 100 if _total else 0.0
    shared_items = mo.ui.table(
        _visible,
        page_size=45,
        selection="multi",
        initial_selection=[],
        hidden_columns=["monthly_net"],
        label="Select costs that should be shared",
    )
    mo.vstack(
        [
            mo.md(
                f"Showing **{_visible.height} of {fair_items.height}** items covering "
                f"**{_covered:.0f}% of burn** (target {coverage_pct.value}%)."
            ),
            shared_items,
        ]
    )
    return (shared_items,)


@app.cell
def _(incomes):
    _income_rows = incomes.select(
        "source",
        "dependable_monthly",
        "months_seen",
        pl.col("dependable_monthly").round(0).alias("dependable $/mo"),
        pl.col("average_monthly").round(0).alias("average $/mo"),
    )
    income_table = mo.ui.table(
        _income_rows,
        page_size=10,
        selection="multi",
        initial_selection=[],
        hidden_columns=["dependable_monthly"],
        label="Select income sources that remain",
    )
    mo.vstack(
        [
            mo.md(
                "## 2. Select dependable income that remains\n\n"
                "Select only sources expected to continue. The notebook uses each source's median complete month, "
                "which avoids treating a bonus or three-paycheck month as dependable monthly income."
            ),
            income_table,
        ]
    )
    return (income_table,)


@app.cell(hide_code=True)
def _(share_pct, shared_items):
    _selected_shared = shared_items.value
    _shared_total = float(_selected_shared["monthly_net"].sum()) if _selected_shared.height else 0.0
    fair_contribution = _shared_total * float(share_pct.value) / 100
    mo.md(
        f"### Fair-share estimate: **\\${fair_contribution:,.0f}/mo**\n\n"
        f"Selected shared costs: **\\${_shared_total:,.0f}/mo** at **{share_pct.value}%**. "
        "This is a negotiated cost share, not the household solvency requirement.\n\n"
        "## 3. State the plan\n\n"
        "Enter cuts you are actually prepared to make, not everything that could theoretically be cut. "
        "The safety margin is the monthly amount you want left after bills rather than targeting exactly $0."
    )
    return (fair_contribution,)


@app.cell
def _():
    planned_cuts = mo.ui.number(start=0, stop=30000, step=100, value=0, label="Planned monthly cuts ($)")
    safety_margin = mo.ui.number(start=0, stop=10000, step=100, value=0, label="Monthly safety margin ($)")
    mo.hstack([planned_cuts, safety_margin], justify="start", gap=2)
    return planned_cuts, safety_margin


@app.cell
def _(
    fair_contribution,
    income_table,
    planned_cuts,
    primary,
    safety_margin,
    snapshots,
):
    _selected_income = income_table.value
    mo.stop(
        not _selected_income.height,
        mo.md("Select at least one dependable income source to calculate the contribution."),
    )
    _retained = float(_selected_income["dependable_monthly"].sum())
    _cuts = min(float(planned_cuts.value), primary["monthly_burn"])
    _margin = float(safety_margin.value)
    required_contribution = max(0.0, primary["monthly_burn"] - _cuts - _retained + _margin)
    _difference = fair_contribution - required_contribution
    _comparison = (
        f"fair share is \\${_difference:,.0f}/mo above the solvency floor"
        if _difference >= 0
        else f"fair share leaves a \\${-_difference:,.0f}/mo household gap"
    )
    _sensitivity = pl.DataFrame(
        [
            {
                "window": f"{months} months",
                "observed burn": round(snapshot["monthly_burn"]),
                "required contribution": round(max(0.0, snapshot["monthly_burn"] - _cuts - _retained + _margin)),
            }
            for months, snapshot in snapshots.items()
        ]
    )
    mo.vstack(
        [
            mo.md(
                f"## Solvency floor: **\\${required_contribution:,.0f}/mo**\n\n"
                f"- Observed 12-month burn: **\\${primary['monthly_burn']:,.0f}/mo**\n"
                f"- Dependable income retained: **\\${_retained:,.0f}/mo**\n"
                f"- Planned cuts: **\\${_cuts:,.0f}/mo**\n"
                f"- Safety margin: **\\${_margin:,.0f}/mo**\n"
                f"- Fair-share estimate: **\\${fair_contribution:,.0f}/mo** - {_comparison}.\n\n"
                "The solvency floor and fair share answer different questions; the comparison makes any "
                "remaining household decision explicit."
            ),
            mo.md("### Window sensitivity"),
            mo.ui.table(_sensitivity, page_size=3, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## To extend

    - Compare the resulting solvency floor with a separately negotiated fair-share proposal.
    - Add a specific incremental-cost estimate if the contributor changes household occupancy.
    """)
    return


if __name__ == "__main__":
    app.run()
