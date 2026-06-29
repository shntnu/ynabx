# /// script
# requires-python = ">=3.11"
# dependencies = [
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

    import marimo as mo
    import numpy as np
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb01_ynab_client import active_budget_id, get
    from nb02_ynab_sync import connect

    # Household cash-flow what-if. The hard-won model:
    #   operating cash flow = every leg on the CASH accounts at split-LEAF level (has_splits=FALSE,
    #   so capital hidden inside a split is read by its real category, not the lumped parent),
    #   EXCLUDING anything categorized 'Investment' - that one filter strips both discretionary
    #   investment moves AND the YNAB "accounting transfer" entries that ride those legs, which
    #   otherwise masquerade as income. It KEEPS the mandatory cash: spending, card payments, debt
    #   service. The headline statistic is the MEDIAN - lumpy capital months (a loan paydown funded
    #   by an investment pull) leave two-legged artifacts no single-leg rule removes, so the mean is
    #   unreliable; the median is stable across windows.
    # Account CLASS (cash vs credit vs tracking) has no column in the cache, so it comes from the
    # live API once per run, name-free and self-updating.


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb10 - Household cash-flow what-ifs

    "If an income source changes, what do we need to stay out of the red?"

    Reads your real **operating cash flow** from the cache and lets you turn **three knobs**:

    - **Monthly income change** - a recurring source rising or (usually) falling to $0.
    - **Monthly contribution** - new recurring help (a housemate, a side gig).
    - **One-time cash draw** - a lump leaving liquid now (e.g. moving cash to investments).

    It reports the **breakeven contribution**, the resulting **median net flow**, and **how long
    your liquid cash lasts** at that rate. Numbers use the median month and exclude asset moves,
    so investment shuffles and bookkeeping transfers don't masquerade as income.
    """)
    return


@app.function
def accounts() -> dict:
    """Classify live accounts -> on-budget id set (cash + credit, where flow lives) and the
    liquid-cash id set (checking/savings/cash). One API GET; avoids hardcoding bank names."""
    bid = active_budget_id()
    onbudget, liquid = [], []
    for a in get(f"/budgets/{bid}/accounts")["accounts"]:
        if a["closed"] or a["deleted"] or not a["on_budget"]:
            continue
        onbudget.append(a["id"])
        if a["type"] in ("checking", "savings", "cash"):
            liquid.append(a["id"])
    return {"onbudget": onbudget, "liquid": liquid}


@app.function
def baseline(window_months: int = 12) -> dict:
    """Monthly OPERATING cash flow over the trailing window (current partial month dropped).

    flows = per-month operating cash flow (see the app.setup note for the exact definition).
    The headline is net_median (robust); net_mean is shown only for contrast and is skewed by any
    capital month in the window. `sources` lists ONLY real recurring income (the YNAB income
    category), with months_seen to separate a steady paycheck from a one-off.
    """
    acct = accounts()
    con = connect()
    # operating cash flow per month: cash accounts, leaf level, anything categorized Investment dropped
    op = con.execute(
        """
        SELECT date_trunc('month', date) AS m, SUM(amount_milli) / 1000.0 AS op
        FROM transactions
        WHERE NOT deleted AND account_id IN ? AND has_splits = FALSE
          AND COALESCE(category_name, '') <> 'Investment'
        GROUP BY 1 ORDER BY 1
        """,
        [acct["liquid"]],
    ).fetchall()
    liquid_cash = con.execute(
        "SELECT COALESCE(SUM(amount_milli), 0) / 1000.0 FROM transactions "
        "WHERE NOT deleted AND account_id IN ? AND parent_id IS NULL",
        [acct["liquid"]],
    ).fetchone()[0]
    # real recurring income only: the YNAB income category, leaf level, with recurrence count
    sources = con.execute(
        """
        SELECT COALESCE(payee_name, '(unnamed)') AS source,
               COUNT(DISTINCT date_trunc('month', date)) AS months_seen,
               ROUND(SUM(amount_milli) / 1000.0) AS total_in
        FROM transactions
        WHERE NOT deleted AND account_id IN ? AND amount_milli > 0
          AND has_splits = FALSE
          AND category_name = 'Inflow: Ready to Assign'
          AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
          AND date >= (current_date - (? * INTERVAL 1 MONTH))
        GROUP BY 1 ORDER BY total_in DESC LIMIT 8
        """,
        [acct["onbudget"], window_months],
    ).pl()
    con.close()

    this_month = str(np.datetime64("today", "M"))
    op = [r for r in op if str(r[0])[:7] != this_month][-window_months:]
    flows = np.array([float(r[1]) for r in op])
    return {
        "window_months": len(flows),
        "flows": flows,
        "net_median": float(np.median(flows)),  # robust headline
        "net_mean": float(np.mean(flows)),  # contrast only; unreliable when a capital month is in-window
        "liquid_cash": round(float(liquid_cash), 2),
        "sources": sources,
    }


@app.function
def scenario(
    base: dict,
    monthly_income_change: float = 0.0,
    monthly_contribution: float = 0.0,
    one_time_draw: float = 0.0,
) -> dict:
    """Apply the three knobs. Pure (no I/O) so a sweep is cheap. Everything keys off the MEDIAN
    operating month. breakeven_contribution brings the median month to zero given the income
    change. months_to_zero = how long current liquid lasts if the median month stays negative
    (the literal "when do we go negative"); infinite if the median is >= 0.
    """
    delta = monthly_income_change + monthly_contribution
    net_median = base["net_median"] + delta
    breakeven = max(0.0, -(base["net_median"] + monthly_income_change))
    liquid_after = base["liquid_cash"] - one_time_draw
    months_to_zero = float("inf") if net_median >= 0 else round(liquid_after / -net_median, 1)
    return {
        "net_median": round(net_median, 0),
        "breakeven_contribution": round(breakeven, 0),
        "liquid_after": round(liquid_after, 2),
        "months_to_zero": months_to_zero,
    }


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Your baseline (read from the cache)
    """)
    return


@app.cell
def _():
    base = baseline(window_months=12)
    _src = base["sources"].with_columns(pl.col("total_in").cast(pl.Int64))
    mo.vstack(
        [
            mo.md(
                f"Over the last **{base['window_months']} months** your **operating cash flow** "
                f"(income minus living costs and debt service, asset moves excluded) ran a median of "
                f"**\\${base['net_median']:+,.0f}/mo** (mean \\${base['net_mean']:+,.0f}, skewed by "
                f"capital months); liquid cash now **\\${base['liquid_cash']:,.0f}**.\n\n"
                f"Real recurring income only - `months_seen` near {base['window_months']} is a steady "
                f"paycheck; 1-2 is a one-off that isn't dependable income:"
            ),
            mo.ui.table(_src, page_size=8, selection=None),
        ]
    )
    return (base,)


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Turn the knobs
    """)
    return


@app.cell
def _():
    income_change = mo.ui.number(start=-30000, stop=30000, step=250, value=0, label="Monthly income change ($)")
    contribution = mo.ui.number(start=0, stop=30000, step=250, value=0, label="New monthly contribution ($)")
    one_time_draw = mo.ui.number(start=0, stop=80000, step=1000, value=0, label="One-time cash draw now ($)")
    mo.vstack([income_change, contribution, one_time_draw])
    return contribution, income_change, one_time_draw


@app.cell
def _(base, contribution, income_change, one_time_draw):
    s = scenario(
        base,
        monthly_income_change=income_change.value,
        monthly_contribution=contribution.value,
        one_time_draw=one_time_draw.value,
    )
    _mz = "never (cash holds or grows)" if s["months_to_zero"] == float("inf") else f"{s['months_to_zero']} months"
    _verdict = "cash holds steady" if s["net_median"] >= 0 else "**cash erodes**"
    mo.md(
        f"### This scenario\n\n"
        f"- Median operating month: **\\${s['net_median']:+,.0f}/mo** - {_verdict}.\n"
        f"- Breakeven contribution (brings the median month to zero): **\\${s['breakeven_contribution']:,.0f}/mo**.\n"
        f"- Liquid after the one-time draw: **\\${s['liquid_after']:,.0f}**.\n"
        f"- At this rate, liquid runs out in: **{_mz}**.\n\n"
        f"_Median basis, robust to lumpy capital months. Breakeven is the floor; add margin for a "
        f"third person in the house and for bad months._"
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Contribution sweep (how much help is enough?)
    """)
    return


@app.cell
def _(base, income_change, one_time_draw):
    _rows = []
    for _c in (0, 4000, 6000, 8500, 10000):
        _s = scenario(
            base,
            monthly_income_change=income_change.value,
            monthly_contribution=_c,
            one_time_draw=one_time_draw.value,
        )
        _mz = _s["months_to_zero"]
        _rows.append(
            {
                "contribution/mo": _c,
                "median net/mo": _s["net_median"],
                "months to $0": ("never" if _mz == float("inf") else f"{_mz:.1f}"),
            }
        )
    mo.vstack(
        [
            mo.md(
                f"At the current income change of **\\${income_change.value:+,.0f}/mo**, "
                "how each contribution level lands:"
            ),
            mo.ui.table(pl.DataFrame(_rows), page_size=8, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## To extend

    - **Discretionary vs mandatory burn**: split living spend into fixed (mortgage/loans/utilities)
      and flexible, so you can see how far trimming stretches things in a real squeeze.
    - **Income recovery curve**: model the lost income returning after N months (a job search),
      not a permanent step, and read the dip rather than the steady state.
    - **Bonus-aware income**: separate base paychecks from bonus months so the recurring figure
      isn't lifted by a once-a-year spike.
    """)
    return


if __name__ == "__main__":
    app.run()
