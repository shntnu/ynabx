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
    import csv
    import datetime as dt
    import sys
    from collections import defaultdict
    from pathlib import Path

    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb02_ynab_sync import connect

    # Per-bank CSV column maps. Each entry says which header holds the
    # transaction date, the payee/description, and the signed amount
    # (negative = outflow/charge), plus the date format to parse.
    #
    # Only Fidelity is verified against a real export. Add a bank by
    # dropping its statement in and filling one of these in; everything
    # downstream (parsing, matching) is bank-agnostic once the columns
    # are named.
    BANK_FORMATS: dict[str, dict] = {
        "fidelity": {
            "date": "Date",
            "payee": "Name",
            "amount": "Amount",
            "date_fmt": "%Y-%m-%d",
        },
        # "bofa": {"date": ..., "payee": ..., "amount": ..., "date_fmt": ...},
        # "chase": {...},
        # "amex": {...},
    }


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb06 - Statement reconciliation

    Answers *"does my YNAB account agree with the bank's statement, line
    by line?"* - the monthly chore of squaring a downloaded statement CSV
    against what YNAB has.

    A naive match by `(date, amount)` breaks on two things this notebook
    handles:

    - **Repeated amounts alias.** Five `$8.22` coffees in a window will
      mis-pair against each other and report phantom mismatches. We pool
      transactions *by amount* and pair them off within each bucket, so
      only genuine count differences survive.
    - **Dates drift.** A charge made on the 19th may post on the 21st.
      Pooling by amount sidesteps date-by-date matching entirely; date is
      only carried along for display.

    The output is three things:

    - **Missing from YNAB** - bank charges with no YNAB counterpart. These
      are what you add (YNAB File-Based Import dedupes, so re-importing the
      statement is the safe way).
    - **In YNAB, not on statement** - usually legitimately pending charges
      that haven't posted yet; occasionally a duplicate or wrong-amount
      entry to fix.
    - **Cleared-vs-bank delta** - how far YNAB's cleared balance sits from
      the statement balance. A small residual after the line items match
      is pre-window drift; clear it with YNAB's Reconcile adjustment.

    This notebook is **read-only**. It tells you what's off; you add /
    delete / clear in the YNAB app. Baking destructive writes into a
    reconcile tool is how you delete a real transaction to force a
    balance - don't.
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Statement parsing
    """)
    return


@app.function(hide_code=True)
def load_statement(
    path: str, bank: str = "fidelity"
) -> list[tuple[dt.date, str, float]]:
    """Parse a bank statement CSV into `(date, payee, amount)` rows.

    `amount` is signed: negative for charges/outflows, positive for
    payments/credits, matching YNAB's `amount_milli` sign convention.
    `bank` selects a column map from `BANK_FORMATS`; only the columns
    named there are read, so format differences between institutions are
    isolated to that one dict.
    """
    if bank not in BANK_FORMATS:
        raise ValueError(
            f"unknown bank {bank!r}; known: {sorted(BANK_FORMATS)}. "
            "Add a column map to BANK_FORMATS."
        )
    fmt = BANK_FORMATS[bank]
    rows: list[tuple[dt.date, str, float]] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            d = dt.datetime.strptime(r[fmt["date"]].strip(), fmt["date_fmt"]).date()
            payee = r[fmt["payee"]].strip()
            amount = round(float(r[fmt["amount"]]), 2)
            rows.append((d, payee, amount))
    return rows


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Reconciliation

    `reconcile()` returns a dict with the two diff tables (`missing`,
    `pending`) and the balance figures. The match is exact-amount,
    pooled-by-bucket: for each distinct amount we pair bank rows against
    YNAB rows in date order and report the leftover on whichever side has
    more.
    """)
    return


@app.function(hide_code=True)
def reconcile(
    account_name: str,
    statement: list[tuple[dt.date, str, float]],
    since: str,
    until: str,
) -> dict:
    """Diff a parsed statement against YNAB for one account over a window.

    `since`/`until` are ISO dates bounding both sides; pass the statement's
    own period. Returns:

    - `missing`  - DataFrame of bank rows with no YNAB match (add these)
    - `pending`  - DataFrame of YNAB rows with no bank match (pending/verify)
    - `bank_balance`, `ynab_cleared`, `ynab_working` - floats
    - `cleared_delta` - ynab_cleared - bank_balance (0.0 = reconciled)

    `bank_balance` here is the *net of the window* (sum of statement
    rows), not the statement's closing balance - the CSV has no opening
    balance, so use `cleared_delta` against the figure the bank shows you
    on screen, not this number, for the final tie-out.
    """
    con = connect()

    # Balance/matching uses `parent_id IS NULL`, NOT the category-spend
    # filter `(parent_id IS NULL OR has_splits = FALSE)`. The latter also
    # admits split *children* (parent_id set, has_splits false), which
    # double-counts a split alongside its parent and blows up the balance.
    # The bank posts a split as one charge = the parent's full amount, so
    # the parent row at `parent_id IS NULL` is exactly what we match.
    yn: dict[float, list[tuple[dt.date, str, str]]] = defaultdict(list)
    for d, payee, amount, cleared in con.execute(
        """
        SELECT date, payee_name, amount_milli/1000.0 AS amount, cleared
        FROM transactions
        WHERE account_name = ?
          AND NOT deleted
          AND parent_id IS NULL
          AND date BETWEEN ? AND ?
        """,
        [account_name, since, until],
    ).fetchall():
        yn[round(amount, 2)].append((d, payee, cleared))

    ynab_working = con.execute(
        """
        SELECT COALESCE(SUM(amount_milli), 0)/1000.0
        FROM transactions
        WHERE account_name = ? AND NOT deleted AND parent_id IS NULL
        """,
        [account_name],
    ).fetchone()[0]
    ynab_cleared = con.execute(
        """
        SELECT COALESCE(SUM(amount_milli), 0)/1000.0
        FROM transactions
        WHERE account_name = ? AND NOT deleted AND parent_id IS NULL
          AND cleared IN ('cleared', 'reconciled')
        """,
        [account_name],
    ).fetchone()[0]
    con.close()

    bank: dict[float, list[tuple[dt.date, str]]] = defaultdict(list)
    for d, payee, amount in statement:
        bank[amount].append((d, payee))

    missing: list[tuple[dt.date, float, str]] = []  # on bank, not in YNAB
    pending: list[tuple[dt.date, float, str, str]] = []  # in YNAB, not on bank
    for amount in sorted(set(bank) | set(yn)):
        b = sorted(bank.get(amount, []))
        y = sorted(yn.get(amount, []))
        n = min(len(b), len(y))
        for d, payee in b[n:]:
            missing.append((d, amount, payee))
        for d, payee, cleared in y[n:]:
            pending.append((d, amount, payee, cleared))

    missing_df = pl.DataFrame(
        sorted(missing), schema=["date", "amount", "payee"], orient="row"
    )
    pending_df = pl.DataFrame(
        sorted(pending),
        schema=["date", "amount", "payee", "cleared"],
        orient="row",
    )
    bank_balance = round(sum(a for _, _, a in statement), 2)
    return {
        "missing": missing_df,
        "pending": pending_df,
        "bank_balance": bank_balance,
        "ynab_cleared": round(ynab_cleared, 2),
        "ynab_working": round(ynab_working, 2),
        "cleared_delta": round(ynab_cleared - bank_balance, 2),
    }


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Interactive

    Pick the YNAB account, point at a downloaded statement CSV, set the
    statement's period and bank format, then enter the closing balance the
    bank shows you on screen to get the final tie-out.
    """)
    return


@app.cell
def _():
    _con = connect()
    _accounts = [
        r[0]
        for r in _con.execute(
            "SELECT DISTINCT account_name FROM transactions WHERE NOT deleted ORDER BY 1"
        ).fetchall()
    ]
    _con.close()
    account = mo.ui.dropdown(
        _accounts,
        value="Fidelity CC" if "Fidelity CC" in _accounts else _accounts[0],
        label="Account",
    )
    bank_fmt = mo.ui.dropdown(
        list(BANK_FORMATS.keys()), value="fidelity", label="Bank format"
    )
    csv_path = mo.ui.text(
        placeholder="/path/to/statement.csv", label="Statement CSV", full_width=True
    )
    since = mo.ui.text(value="2026-01-01", label="Since (YYYY-MM-DD)")
    until = mo.ui.text(value="2026-01-31", label="Until (YYYY-MM-DD)")
    closing = mo.ui.text(
        placeholder="e.g. 1234.56",
        label="Statement closing balance (amount owed, positive)",
    )
    run = mo.ui.run_button(label="Reconcile")
    controls = mo.vstack([account, bank_fmt, csv_path, since, until, closing, run])
    mo.sidebar([mo.md("### Reconcile"), controls])
    return account, bank_fmt, closing, csv_path, run, since, until


@app.cell
def _(account, bank_fmt, closing, csv_path, run, since, until):
    mo.stop(
        not run.value,
        mo.md("Set the controls in the sidebar and click **Reconcile**."),
    )
    mo.stop(
        not csv_path.value,
        mo.md("Provide a statement CSV path in the sidebar."),
    )

    _stmt = load_statement(csv_path.value, bank=bank_fmt.value)
    _res = reconcile(account.value, _stmt, since.value, until.value)

    _missing = _res["missing"]
    _pending = _res["pending"]
    _miss_total = float(_missing["amount"].sum()) if _missing.height else 0.0
    _pend_total = float(_pending["amount"].sum()) if _pending.height else 0.0

    # Tie-out against the on-screen closing balance if provided. YNAB
    # tracks credit-card debt as a negative working balance, so a
    # statement "amount owed" of 1234.56 compares to ynab_cleared of
    # -1234.56.
    _tie = ""
    if closing.value:
        _target = -abs(float(closing.value))
        _delta = round(_res["ynab_cleared"] - _target, 2)
        _tie = (
            f"\n\n**Tie-out:** YNAB cleared `{_res['ynab_cleared']:,.2f}` vs "
            f"statement `{_target:,.2f}` -> delta **`{_delta:+,.2f}`**. "
            + (
                "Reconciled."
                if abs(_delta) < 0.005
                else "Residual is pre-window drift once the line items below match; "
                "clear it with YNAB's Reconcile adjustment."
            )
        )

    mo.vstack(
        [
            mo.md(
                f"### {account.value}: {since.value} -> {until.value}\n\n"
                f"- **{_missing.height}** charges on the statement missing from YNAB "
                f"(`{_miss_total:+,.2f}`) - add these (re-import the CSV; YNAB dedupes).\n"
                f"- **{_pending.height}** YNAB rows not on the statement "
                f"(`{_pend_total:+,.2f}`) - pending charges, or duplicates/wrong amounts to fix.\n"
                f"- YNAB working `{_res['ynab_working']:,.2f}` | "
                f"cleared `{_res['ynab_cleared']:,.2f}`" + _tie
            ),
            mo.md("#### Missing from YNAB"),
            mo.ui.table(_missing, page_size=25, selection=None)
            if _missing.height
            else mo.md("_None - every statement charge has a YNAB match._"),
            mo.md("#### In YNAB, not on statement"),
            mo.ui.table(_pending, page_size=25, selection=None)
            if _pending.height
            else mo.md("_None._"),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
