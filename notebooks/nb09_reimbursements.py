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

    NOTEBOOK_DIR = Path(__file__).parent
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb02_ynab_sync import connect

    # The reimbursable ledger is ONE category per side; the reimburser lives in the
    # memo PREFIX (text before the first ':'), e.g. "Acme: hotel". State is NOT
    # tracked in flags or "resolved" memos - it is DERIVED here from the balance.

    def reimburser(memo: str | None) -> str:
        """The reimburser for a row = the memo prefix before the first ':'.

        Peels a leading resolved/reimbursed marker first ("Resolved: Acme: flight"
        -> "Acme"). No prefix -> "(unattributed)".
        """
        m = (memo or "").strip()
        low = m.lower()
        for mk in ("resolved", "reimbursed"):
            if low == mk:
                return "(unattributed)"
            if low.startswith(mk + ":") or low.startswith(mk + " "):
                m = m[len(mk) :].lstrip(": ").strip()
                break
        if ":" not in m:
            return "(unattributed)"
        return m.split(":", 1)[0].strip()


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb09 - Reimbursements

    The reimbursable ledger, run on the **category balance** - the one signal that
    can't drift. One category per side (`Business`, `Personal`); the reimburser is the
    **memo prefix** (`Acme:`, `Globex:`...) on *both* the expense and the money back.
    Three reads, no writes:

    - `whole(category)` - the truth: the category balance (~0 = settled), plus a drift
      check (`flags_match`/`gap`: do the orange flags still net to the balance? a stale
      or missing flag shows up as a nonzero gap).
    - `outstanding(category)` - the chase list: open (orange-flagged) rows by reimburser.
      net < 0 = they owe you; net > 0 = overpaid (a fee that belongs in income).
    - `leaks(days)` - reimbursement money booked OUTSIDE the category (wires/refunds in
      income/RTA). The one failure mode the structure can't self-correct - re-categorize
      these into the reimbursable category (split off any fee to income).

    Per-reimburser netting of ALL rows is deliberately NOT used: on legacy data the two
    legs are inconsistently tagged, so it sums correctly but splits per-reimburser into
    garbage. Run `nb02.reconcile()` (not `sync()`) first if you just did bulk memo edits
    - the delta sync misses memo-only changes.
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Am I whole?
    """)
    return


@app.function(hide_code=True)
def whole(category: str = "Business") -> dict:
    """Category status. `balance` is the truth (~0 => settled). Plus a drift check:
    the orange-flagged rows should net to the balance; a nonzero `gap` (flags_match
    False) means a flag is stale (paid but still flagged) or missing (open, unflagged).
    """
    con = connect()
    bal, orange_n, orange_total = con.execute(
        """
        SELECT ROUND(SUM(amount_milli) / 1000.0, 2),
               COUNT(*) FILTER (WHERE flag_color = 'orange'),
               ROUND(SUM(amount_milli) FILTER (WHERE flag_color = 'orange') / 1000.0, 2)
        FROM transactions
        WHERE NOT deleted AND category_name = ?
        """,
        [category],
    ).fetchone()
    con.close()
    bal, orange_total = bal or 0.0, orange_total or 0.0
    gap = round(bal - orange_total, 2)
    return {
        "category": category,
        "balance": bal,
        "orange": orange_n,
        "orange_total": orange_total,
        "gap": gap,
        "flags_match": abs(gap) < 1.0,
    }


@app.cell
def _():
    _w = whole("Business")
    mo.md(
        f"### Business reimbursable\n\n"
        f"Balance **{_w['balance']:+.2f}**, {_w['orange']} still flagged open. "
        f"{'Settled.' if abs(_w['balance']) < 1 else 'Not yet whole - see who owes you below.'}"
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Who owes me? (the chase list)
    """)
    return


@app.function(hide_code=True)
def outstanding(category: str = "Business") -> pl.DataFrame:
    """The chase list: still-OPEN rows (orange-flagged) grouped by reimburser.

    Why orange and not a per-reimburser net of all rows: on the legacy data the
    expense and refund legs are inconsistently tagged, so netting all rows produces a
    correct TOTAL but a garbage per-reimburser split (verified). The orange flag is
    the user's curated "still open" marker, so the open set is reliable. `whole()`
    cross-checks it (the orange total should equal the balance; a gap = a stale/missing
    flag). Going forward (prefix on both legs), the flag becomes optional - watch
    `whole()` trend to zero and run `leaks()`.

    net < 0 => they owe you; net > 0 => overpaid (a fee that belongs in income).
    """
    con = connect()
    rows = con.execute(
        """
        SELECT amount_milli, memo FROM transactions
        WHERE NOT deleted AND category_name = ? AND flag_color = 'orange'
        """,
        [category],
    ).fetchall()
    con.close()

    agg: dict[str, dict] = {}
    for amt, memo in rows:
        d = agg.setdefault(reimburser(memo), {"net": 0.0, "n": 0})
        d["net"] += amt / 1000.0
        d["n"] += 1

    out = [{"reimburser": r, "net": round(d["net"], 2), "n_rows": d["n"]} for r, d in agg.items()]
    if not out:
        return pl.DataFrame(schema={"reimburser": pl.Utf8, "net": pl.Float64, "n_rows": pl.Int64})
    return pl.DataFrame(out).sort("net")


@app.cell
def _():
    _df = outstanding("Business")
    _w = whole("Business")
    _flag = (
        "flags match the balance (no drift)"
        if _w["flags_match"]
        else f"**flags drifted** - orange total {_w['orange_total']:+.2f} vs balance {_w['balance']:+.2f} (gap {_w['gap']:+.2f}); a flag is stale or missing"
    )
    mo.vstack(
        [
            mo.md(f"### Business - open items by reimburser (chase list)\n\n{_flag}"),
            mo.ui.table(_df, page_size=25, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## What leaked? (reimbursement money booked as income/RTA)
    """)
    return


@app.function(hide_code=True)
def leaks(days: int = 180) -> pl.DataFrame:
    """Inflows OUTSIDE the reimbursable categories that look like reimbursements.

    The one failure mode the category balance can't catch: a wire/refund booked as
    income or Ready-to-Assign never nets the reimbursable category, so it reads
    'owed' forever. These are candidates to re-categorize into Business/Personal.
    Heuristic (payee/memo keywords) - eyeball before acting.
    """
    con = connect()
    df = con.execute(
        """
        SELECT date, amount_milli / 1000.0 AS amount, account_name, category_name,
               payee_name, memo
        FROM transactions
        WHERE NOT deleted AND amount_milli > 0
          AND date >= current_date - (? * INTERVAL 1 DAY)
          AND COALESCE(category_name, '') NOT IN ('Business', 'Personal')
          AND (parent_id IS NULL OR has_splits = FALSE)
          AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
          AND lower(COALESCE(memo, '')) NOT LIKE 'resolved%'
          AND (
              regexp_matches(lower(COALESCE(payee_name, '')), 'remittance|wire|reimburs|refund|chips')
              OR regexp_matches(lower(COALESCE(memo, '')), 'reimburs|remittance|wire')
          )
        ORDER BY amount DESC
        """,
        [days],
    ).pl()
    con.close()
    return df


@app.cell
def _():
    _lk = leaks(days=180)
    mo.vstack(
        [
            mo.md(
                f"### Possible reimbursements booked elsewhere (last 180d): {_lk.height} rows\n\n"
                "If any of these are reimbursements, re-categorize into the reimbursable "
                "category with the reimburser prefix (split off any fee to income)."
            ),
            mo.ui.table(_lk, page_size=25, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## To extend

    - **Aging on the chase list.** Add days-open per reimburser to `outstanding()` so the
      stalest claims float to the top.
    - **Leak auto-plan.** Feed confirmed `leaks()` rows into an nb04-style dry-run plan that
      re-categorizes them into the reimbursable category (fee split off to income).
    - **Flag-drift alarm.** Track the `whole()` gap over time; a growing gap means memo/flag
      hygiene is slipping before the balance itself lies.
    """)
    return


if __name__ == "__main__":
    app.run()
