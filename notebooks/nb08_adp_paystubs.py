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
    import json
    import sys
    from pathlib import Path

    import marimo as mo
    import polars as pl

    NOTEBOOK_DIR = Path(__file__).resolve().parent
    DATA_DIR = NOTEBOOK_DIR.parent / "data"
    if str(NOTEBOOK_DIR) not in sys.path:
        sys.path.insert(0, str(NOTEBOOK_DIR))

    from nb02_ynab_sync import connect


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb08 - ADP income stability and paycheck splits

    Determine whether recurring income is stable, explain why take-home pay changes between checks, and connect
    each direct deposit to the corresponding YNAB inflow.
    The private JSON export produced by `scripts/import_adp.js` is normalized into three reusable tables:

    - `checks` - one row per pay statement, including gross pay, net pay, and take-home rate
    - `components` - earnings and deductions, one row per named component
    - `deposits` - direct-deposit allocations without account or routing numbers

    The analysis separates the most frequent earning component from exceptional earnings.
    It reports cadence and observed variation across the available history without imposing a stability threshold
    or selecting a fixed number of recent checks.

    Real payroll data remains under the gitignored `data/` directory.
    """)
    return


@app.function(hide_code=True)
def latest_adp_export(raw_dir: str | Path = DATA_DIR / "raw" / "adp") -> Path:
    """Return the newest immutable MyADP JSON export."""
    exports = sorted(Path(raw_dir).glob("adp-pay-statements-*.json"))
    if not exports:
        raise FileNotFoundError(f"no ADP exports found under {raw_dir}")
    return exports[-1]


@app.function(hide_code=True)
def load_adp(path: str | Path | None = None) -> dict[str, pl.DataFrame]:
    """Load a MyADP JSON export into checks, components, and deposits tables."""

    def amount(value: dict | None) -> float | None:
        raw = (value or {}).get("amountValue")
        return None if raw is None else float(raw)

    source = Path(path) if path else latest_adp_export()
    payload = json.loads(source.read_text())
    checks: list[dict] = []
    components: list[dict] = []
    deposits: list[dict] = []
    for statement_index, statement in enumerate(payload["statements"], start=1):
        pay_date = statement["payDate"]
        statement_id = (statement.get("_adp") or {}).get("statementID") or f"{pay_date}:{statement_index}"
        gross = amount(statement.get("grossPayAmount"))
        net = amount(statement.get("netPayAmount"))
        period = statement.get("payPeriod") or {}
        checks.append(
            {
                "statement_id": statement_id,
                "pay_date": pay_date,
                "period_start": period.get("startDate"),
                "period_end": period.get("endDate"),
                "gross": gross,
                "net": net,
                "gross_ytd": amount(statement.get("grossPayYTDAmount")),
                "take_home_rate": net / gross if net is not None and gross else None,
            }
        )
        for earning in statement.get("earnings") or []:
            components.append(
                {
                    "statement_id": statement_id,
                    "pay_date": pay_date,
                    "kind": "earning",
                    "name": earning.get("earningCodeName"),
                    "amount": amount(earning.get("earningAmount")),
                    "amount_ytd": amount(earning.get("earningYTDAmount")),
                    "hours": earning.get("payPeriodHours"),
                }
            )
        for deduction in statement.get("deductions") or []:
            components.append(
                {
                    "statement_id": statement_id,
                    "pay_date": pay_date,
                    "kind": "deduction",
                    "name": deduction.get("codeName"),
                    "amount": amount(deduction.get("deductionAmount")),
                    "amount_ytd": amount(deduction.get("deductionYTDAmount")),
                    "hours": None,
                }
            )
        for index, deposit in enumerate(statement.get("directDeposits") or [], start=1):
            deposits.append(
                {
                    "statement_id": statement_id,
                    "pay_date": pay_date,
                    "deposit": index,
                    "account_type": deposit.get("financialAccountTypeName"),
                    "amount": amount(deposit.get("depositAmount")),
                }
            )
    return {
        "checks": pl.DataFrame(checks).sort("pay_date"),
        "components": pl.DataFrame(components).sort("pay_date", "kind", "name"),
        "deposits": pl.DataFrame(deposits).sort("pay_date", "deposit"),
    }


@app.function(hide_code=True)
def export_adp_csv(payroll: dict[str, pl.DataFrame], out_dir: str | Path = DATA_DIR / "interim" / "adp") -> list[Path]:
    """Write normalized payroll tables as CSV and return their paths."""
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, frame in payroll.items():
        path = destination / f"{name}.csv"
        frame.write_csv(path)
        paths.append(path)
    return paths


@app.function(hide_code=True)
def validate_adp(
    payroll: dict[str, pl.DataFrame], csv_paths: list[Path], source: str | Path | None = None
) -> pl.DataFrame:
    """Return explicit acceptance checks for a complete, internally consistent import."""
    source_path = Path(source) if source else latest_adp_export()
    raw_count = len(json.loads(source_path.read_text())["statements"])
    checks = payroll["checks"]
    components = payroll["components"]
    deposit_totals = payroll["deposits"].group_by("pay_date").agg(pl.col("amount").sum().alias("deposited"))
    net_totals = checks.group_by("pay_date").agg(pl.col("net").sum())
    deposit_mismatches = (
        net_totals.join(deposit_totals, on="pay_date", how="left")
        .filter(pl.col("deposited").is_null() | (pl.col("net").round(2) != pl.col("deposited").round(2)))
        .height
    )
    core_nulls = checks.select(
        pl.sum_horizontal(pl.col("pay_date", "period_start", "period_end", "gross", "net").null_count())
    ).item()
    component_nulls = components.select(pl.sum_horizontal(pl.col("name", "amount").null_count())).item()
    csv_counts_match = all(
        path.exists() and pl.read_csv(path).height == payroll[path.stem].height for path in csv_paths
    )
    return pl.DataFrame(
        [
            {
                "check": "Every raw statement imported",
                "passed": checks.height == raw_count,
                "detail": f"{checks.height} of {raw_count}",
            },
            {
                "check": "Statement identifiers unique",
                "passed": checks["statement_id"].n_unique() == checks.height,
                "detail": f"{checks['statement_id'].n_unique()} unique",
            },
            {
                "check": "Required fields complete",
                "passed": core_nulls + component_nulls == 0,
                "detail": f"{core_nulls + component_nulls} missing",
            },
            {
                "check": "Deposits equal net pay",
                "passed": deposit_mismatches == 0,
                "detail": f"{deposit_mismatches} mismatches",
            },
            {"check": "CSV row counts match", "passed": csv_counts_match, "detail": f"{len(csv_paths)} files checked"},
        ]
    )


@app.function(hide_code=True)
def income_history(payroll: dict[str, pl.DataFrame]) -> tuple[str, pl.DataFrame]:
    """Build one row per check and identify the most frequent earning as recurring income."""
    earnings = payroll["components"].filter(pl.col("kind") == "earning")
    base_name = (
        earnings.group_by("name")
        .agg(pl.col("statement_id").n_unique().alias("checks"))
        .sort("checks", descending=True)["name"][0]
    )
    earning_totals = earnings.group_by("statement_id").agg(
        pl.when(pl.col("name") == base_name).then(pl.col("amount")).otherwise(0.0).sum().alias("base_earnings"),
        pl.when(pl.col("name") != base_name).then(pl.col("amount")).otherwise(0.0).sum().alias("other_earnings"),
    )
    history = (
        payroll["checks"]
        .join(earning_totals, on="statement_id", how="left")
        .with_columns(pl.col("pay_date").str.to_date(), pl.col("other_earnings").fill_null(0.0))
        .with_columns((pl.col("other_earnings") == 0).alias("ordinary_check"))
        .sort("pay_date")
    )
    return base_name, history


@app.function(hide_code=True)
def income_stability(history: pl.DataFrame) -> tuple[dict, pl.DataFrame]:
    """Measure cadence and variation across every ordinary check in the available history."""
    intervals = history.with_columns(pl.col("pay_date").diff().dt.total_days().alias("interval_days"))[
        "interval_days"
    ].drop_nulls()
    cadence_days = int(intervals.median())
    ordinary = history.filter(pl.col("ordinary_check"))
    year_index = (
        ordinary.with_columns(pl.col("pay_date").dt.year().alias("year"))
        .group_by("year")
        .agg(pl.col("base_earnings").median().alias("median_base_earnings"), pl.len().alias("checks"))
        .sort("year")
        .with_columns(
            (pl.col("median_base_earnings") / pl.col("median_base_earnings").first()).alias("relative_earnings")
        )
        .select("year", "checks", pl.col("relative_earnings").round(3))
    )
    return {
        "regular": intervals.n_unique() == 1 and bool((history["base_earnings"] > 0).all()),
        "ordinary_checks": ordinary.height,
        "cadence_days": cadence_days,
        "cadence_rate": float((intervals == cadence_days).mean()),
        "base_cv": float(ordinary["base_earnings"].std() / ordinary["base_earnings"].mean()),
        "base_range": float(ordinary["base_earnings"].max() / ordinary["base_earnings"].min() - 1),
        "ordinary_net_cv": float(ordinary["net"].std() / ordinary["net"].mean()),
        "exceptional_checks": history.filter(~pl.col("ordinary_check")).height,
    }, year_index


@app.function(hide_code=True)
def split_variation(payroll: dict[str, pl.DataFrame], history: pl.DataFrame) -> pl.DataFrame:
    """Rank deductions by variation as a percentage of recurring base earnings."""
    ordinary = history.filter(pl.col("ordinary_check")).select("statement_id", "base_earnings")
    deductions = (
        payroll["components"]
        .filter(pl.col("kind") == "deduction")
        .group_by("statement_id", "name")
        .agg(pl.col("amount").sum())
    )
    names = deductions.join(ordinary, on="statement_id", how="inner").select("name").unique()
    complete = (
        ordinary.join(names, how="cross")
        .join(deductions, on=["statement_id", "name"], how="left")
        .with_columns(pl.col("amount").fill_null(0.0))
        .with_columns((100 * pl.col("amount") / pl.col("base_earnings")).alias("percent_of_base"))
    )
    return (
        complete.group_by("name")
        .agg(
            (pl.col("amount") != 0).sum().alias("checks_present"),
            pl.col("percent_of_base").median().round(2).alias("median_percent"),
            pl.col("percent_of_base").min().round(2).alias("min_percent"),
            pl.col("percent_of_base").max().round(2).alias("max_percent"),
            pl.col("percent_of_base").std().round(2).alias("variability"),
        )
        .sort("variability", descending=True)
    )


@app.function(hide_code=True)
def match_ynab_deposits(payroll: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Match ADP deposits to local YNAB inflows by exact date and milliunit amount."""
    rows = (
        connect()
        .execute(
            """
        SELECT id, date, amount_milli, account_name, payee_name
        FROM transactions
        WHERE NOT deleted
          AND parent_id IS NULL
          AND amount_milli > 0
          AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
        """
        )
        .fetchall()
    )
    ynab = pl.DataFrame(
        rows, schema=["ynab_id", "pay_date", "amount_milli", "ynab_account", "ynab_payee"], orient="row"
    )
    deposits = payroll["deposits"].with_columns(
        pl.col("pay_date").str.to_date(),
        (pl.col("amount") * 1000).round().cast(pl.Int64).alias("amount_milli"),
    )
    candidates = deposits.join(ynab, on=["pay_date", "amount_milli"], how="left")
    counts = candidates.group_by("statement_id", "deposit").agg(pl.col("ynab_id").count().alias("match_count"))
    return (
        candidates.join(counts, on=["statement_id", "deposit"])
        .with_columns(
            pl.when(pl.col("match_count") == 1)
            .then(pl.lit("matched"))
            .when(pl.col("match_count") == 0)
            .then(pl.lit("missing"))
            .otherwise(pl.lit("ambiguous"))
            .alias("match_status")
        )
        .sort("pay_date", "deposit")
    )


@app.cell
def _():
    payroll = load_adp()
    csv_paths = export_adp_csv(payroll)
    validation = validate_adp(payroll, csv_paths)
    import_valid = validation["passed"].all()
    assert import_valid, validation.filter(~pl.col("passed"))
    return csv_paths, import_valid, payroll, validation


@app.cell(hide_code=True)
def _(csv_paths, import_valid, payroll):
    mo.md(f"""
    ## Imported history

    **Import valid: {"YES" if import_valid else "NO"}**

    - Pay statements: **{payroll["checks"].height}**
    - Named earnings and deductions: **{payroll["components"].height}**
    - Direct-deposit allocations: **{payroll["deposits"].height}**
    - CSV files: `{", ".join(str(path) for path in csv_paths)}`
    """)
    return


@app.cell
def _(validation):
    validation
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Is recurring income stable?

    The instrument is the recurring earning printed on each pay statement, not gross or net pay.
    Gross can include exceptional earnings, while net also reflects taxes, benefits, and voluntary deductions.
    Separating those quantities prevents one unusual check from being mistaken for unstable salary.
    """)
    return


@app.cell
def _(payroll):
    base_name, history = income_history(payroll)
    stability, annual_base_index = income_stability(history)
    split_drivers = split_variation(payroll, history)
    return annual_base_index, base_name, history, split_drivers, stability


@app.cell(hide_code=True)
def _(base_name, stability):
    mo.md(f"""
    **Conclusion: recurring income is {"regular" if stability["regular"] else "irregular"} in the available history.**

    - Recurring earning: `{base_name}`
    - Cadence: every **{stability["cadence_days"]} days**, with **{stability["cadence_rate"]:.0%}** of intervals on cadence
    - All {stability["ordinary_checks"]} ordinary checks: recurring-earning CV **{stability["base_cv"]:.1%}** and range **{stability["base_range"]:.1%}**
    - Take-home CV over the same checks: **{stability["ordinary_net_cv"]:.1%}**
    - Checks containing exceptional earnings: **{stability["exceptional_checks"]}**

    `Regular` means the recurring earning is present on every check and the observed pay intervals agree.
    The CV and range are reported without a cutoff so the notebook does not manufacture a stability verdict from an arbitrary threshold.
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Recurring earning over time

    `Relative earnings` divides each year's median recurring earning by the first observed year's median.
    It shows changes in the income level without embedding salary amounts in the notebook source.
    """)
    return


@app.cell
def _(annual_base_index):
    annual_base_index
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## What makes the paycheck split vary?

    Each deduction is expressed as a percentage of recurring earnings across all ordinary checks in the export.
    `Variability` is the standard deviation of that percentage; the largest values are the strongest observed drivers of changing take-home pay.
    This ranks associations in the pay statements, not policy causes - benefit elections and tax rules must be checked separately.
    """)
    return


@app.cell
def _(split_drivers):
    split_drivers
    return


@app.cell(hide_code=True)
def _(split_drivers):
    strongest = split_drivers.row(0, named=True)
    mo.md(f"""
    The strongest observed split driver is `{strongest["name"]}`.
    Across the analysis window it ranges from **{strongest["min_percent"]:.2f}%** to
    **{strongest["max_percent"]:.2f}%** of recurring earnings.
    That identifies where to investigate; it does not establish whether the change came from an election,
    an annual limit, or payroll timing.
    """)
    return


@app.cell
def _(history):
    history.select(
        "pay_date",
        "base_earnings",
        "other_earnings",
        "gross",
        "net",
        "take_home_rate",
        "ordinary_check",
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Do the deposits appear in YNAB?

    A match requires the same pay date and exact milliunit amount.
    Deleted transactions, transfer rows, and split children are excluded.
    No date tolerance is imposed: a posting-date difference appears as `missing` instead of being silently accepted.
    """)
    return


@app.cell
def _(payroll):
    ynab_matches = match_ynab_deposits(payroll)
    ynab_match_summary = (
        ynab_matches.select("statement_id", "deposit", "match_status")
        .unique()
        .group_by("match_status")
        .len(name="deposits")
        .sort("match_status")
    )
    return ynab_match_summary, ynab_matches


@app.cell
def _(ynab_match_summary):
    ynab_match_summary
    return


@app.cell
def _(ynab_matches):
    ynab_matches.select(
        "pay_date",
        "amount",
        "account_type",
        "ynab_account",
        "ynab_payee",
        "match_status",
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Inspect one paycheck

    Select a pay date to see every earning and deduction as both dollars and a percentage of gross pay.
    """)
    return


@app.cell
def _(history):
    pay_date = mo.ui.dropdown(
        options=history["pay_date"].cast(pl.String).reverse().to_list(),
        value=str(history["pay_date"].max()),
        label="Pay date",
    )
    pay_date
    return (pay_date,)


@app.cell
def _(pay_date, payroll):
    selected_gross = payroll["checks"].filter(pl.col("pay_date") == pay_date.value)["gross"].sum()
    paycheck_split = (
        payroll["components"]
        .filter(pl.col("pay_date") == pay_date.value)
        .with_columns((100 * pl.col("amount") / selected_gross).round(2).alias("percent_of_gross"))
        .select("kind", "name", "amount", "percent_of_gross")
    )
    paycheck_split
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## To extend

    - Why do any future deposits appear as missing or ambiguous in YNAB?
    - Do benefit-election changes explain the highest-variability deductions?
    - Should regularity be evaluated over a user-selected date range instead of the full export?
    """)
    return


if __name__ == "__main__":
    app.run()
