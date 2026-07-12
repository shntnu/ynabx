# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anywidget==0.11.0",
#     "duckdb",
#     "marimo",
#     "polars",
#     "pyarrow",
#     "requests",
#     "traitlets==5.15.1",
# ]
# ///

import marimo

__generated_with = "0.23.13"
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
    # Shareability for a temporary co-resident. Rule of thumb: "1 shelter" if a
    # landlord's rent would embed the cost, "2 car"/"2 consumables" if it scales
    # with use or headcount. Everything unmapped falls to "3 personal" (specific
    # to the resident household, not the contributor) - including reimbursables
    # like Consulting/Business/Personal, deliberately. Household value calls, not
    # YNAB data; keys are literal category names, so a YNAB rename drops a row to
    # personal.
    SHARE_TIER = {
        "MortgageNW30": "1 shelter",
        "Property tax": "1 shelter",
        "Home Insurance": "1 shelter",
        "Home Maintenance": "1 shelter",
        "Landscaping": "1 shelter",
        "Cleaning": "1 shelter",
        "Electricity": "1 shelter",
        "Water/Trash": "1 shelter",
        "Internet": "1 shelter",
        "TV": "2 consumables",
        "Home Supplies": "1 shelter",
        "CRV loan": "2 car",
        "Car Maintenance": "2 car",
        "Car Insurance": "2 car",
        "Fuel/Toll": "2 car",
        "Parking": "2 car",
        "Groceries": "2 consumables",
        "Household Goods": "2 consumables",
        "Restaurants": "2 consumables",
        "Entertainment": "2 consumables",
    }
    SHARE_TIER_DEFAULT = "3 personal"
    # An upstream cell re-run recreates the board widget (marimo mints a new
    # frontend model), wiping trait state; an observer mirrors the last
    # selection here so the new instance can be seeded with it.
    LAST_BOARD = {"selected": [], "share_pct": 50}


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
    reimbursements within that category. The board and table make the historical spending assumption
    visible; nothing is automatically called mandatory or discretionary.
    """)
    return


@app.cell
def _(primary):
    fair_items = (
        primary["items"]
        .with_columns(pl.col("category").replace_strict(SHARE_TIER, default=SHARE_TIER_DEFAULT).alias("tier"))
        .sort(["tier", "monthly_net"], descending=[False, True])
        .select(
            "tier",
            "category",
            "group",
            "kind",
            # A category and a liability account can share a name (e.g. HVAC
            # Wells Fargo); uid keeps their rows independently selectable.
            pl.concat_str([pl.col("kind"), pl.lit(":"), pl.col("category")]).alias("uid"),
            "monthly_net",
            pl.col("monthly_net").round(0).alias("net $/mo"),
            pl.col("monthly_gross").round(0).alias("gross $/mo"),
            "months_active",
        )
    )
    _known = set(fair_items["category"].to_list()) | set(category_groups())
    _stale = sorted(set(SHARE_TIER) - _known)
    _stale_note = (
        "\n\n**Stale SHARE_TIER keys** (no matching category or account; the renamed cost now "
        "falls to personal): " + ", ".join(_stale)
        if _stale
        else ""
    )
    mo.md(
        f"Observed 12-month monthly-equivalent burn: **\\${primary['monthly_burn']:,.0f}/mo**.\n\n"
        "Costs are grouped by shareability tier: **1 shelter** (a landlord's rent would embed it), "
        "**2 car** and **2 consumables** (scale with use or headcount) are the usual shared set; "
        "**3 personal** (specific to the resident household, including reimbursables) sinks to the "
        "bottom. Click costs on the board to build the shared set; the buttons set the contributor's "
        "share of it. Bar length is the cost's monthly-equivalent net; the strip on top is the "
        f"selected share of total burn.{_stale_note}"
    )
    return (fair_items,)


@app.cell(hide_code=True)
def _(fair_items):
    import anywidget
    import traitlets

    class ContributionBoard(anywidget.AnyWidget):
        """Click-to-select cost board grouped by shareability tier, with a live contribution readout."""

        _esm = r"""
        function render({ model, el }) {
          // Tiers derive from the data ("1 "/"2 " prefixes self-sort), so a
          // tier added in Python renders without touching this file.
          const TIERS = [...new Set(model.get("items").map((item) => item.tier))].sort();
          const PCTS = [0, 25, 33, 50, 67, 75, 100];
          const fmt = (v) => "$" + Math.round(v).toLocaleString("en-US");
          const esc = (s) =>
            String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

          function save(trait, value) {
            model.set(trait, value);
            model.save_changes();
          }

          function toggle(id) {
            const next = new Set(model.get("selected"));
            next.has(id) ? next.delete(id) : next.add(id);
            save("selected", [...next]);
          }

          function setTier(tier, on) {
            const next = new Set(model.get("selected"));
            for (const item of model.get("items")) {
              if (item.tier === tier) on ? next.add(item.id) : next.delete(item.id);
            }
            save("selected", [...next]);
          }

          function draw() {
            const items = model.get("items");
            const selected = new Set(model.get("selected"));
            const pct = model.get("share_pct");
            const total = items.reduce((sum, item) => sum + item.amount, 0) || 1;
            const maxAmount = Math.max(...items.map((item) => item.amount), 1);
            const sharedTotal = items
              .filter((item) => selected.has(item.id))
              .reduce((sum, item) => sum + item.amount, 0);

            // The full rebuild below destroys the focused button; remember it
            // by key so keyboard toggling does not dump focus back to <body>.
            const active = el.getRootNode().activeElement;
            const focusKey = active && active.dataset ? active.dataset.key : null;
            el.innerHTML = "";
            const root = document.createElement("div");
            root.className = "cb";

            const head = document.createElement("div");
            head.className = "cb-head";
            const headline = document.createElement("div");
            headline.innerHTML =
              `<div class="cb-hero">${fmt((sharedTotal * pct) / 100)}` +
              `<span class="cb-unit">/mo contribution</span></div>` +
              `<div class="cb-sub">${fmt(sharedTotal)}/mo selected shared costs ` +
              `(${Math.round((sharedTotal / total) * 100)}% of burn) at ${pct}%</div>`;
            head.appendChild(headline);
            const pcts = document.createElement("div");
            pcts.className = "cb-pcts";
            pcts.setAttribute("role", "group");
            pcts.setAttribute("aria-label", "Contributor share of selected costs");
            for (const p of PCTS) {
              const btn = document.createElement("button");
              btn.type = "button";
              btn.className = "cb-pct" + (p === pct ? " on" : "");
              btn.dataset.key = "pct:" + p;
              btn.textContent = p + "%";
              btn.addEventListener("click", () => save("share_pct", p));
              pcts.appendChild(btn);
            }
            head.appendChild(pcts);
            root.appendChild(head);

            const meter = document.createElement("div");
            meter.className = "cb-meter";
            meter.title = "Selected share of total burn, by tier";
            TIERS.forEach((tier, index) => {
              const amount = items
                .filter((item) => item.tier === tier && selected.has(item.id))
                .reduce((sum, item) => sum + item.amount, 0);
              if (!amount) return;
              const seg = document.createElement("div");
              seg.className = `cb-seg tier-${(index % 4) + 1}`;
              seg.style.width = (amount / total) * 100 + "%";
              meter.appendChild(seg);
            });
            root.appendChild(meter);

            TIERS.forEach((tier, index) => {
              const rows = items.filter((item) => item.tier === tier);
              if (!rows.length) return;
              const section = document.createElement("div");
              section.className = `cb-tier tier-${(index % 4) + 1}`;

              const tierSelected = rows
                .filter((row) => selected.has(row.id))
                .reduce((sum, row) => sum + row.amount, 0);
              const tierTotal = rows.reduce((sum, row) => sum + row.amount, 0);
              const header = document.createElement("div");
              header.className = "cb-tier-head";
              header.innerHTML =
                `<span class="cb-dot"></span><span class="cb-tier-name">${esc(tier)}</span>` +
                `<span class="cb-tier-sum">${fmt(tierSelected)} of ${fmt(tierTotal)}</span>`;
              for (const [label, on] of [["all", true], ["none", false]]) {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "cb-mini";
                btn.dataset.key = "mini:" + tier + ":" + label;
                btn.textContent = label;
                btn.addEventListener("click", () => setTier(tier, on));
                header.appendChild(btn);
              }
              section.appendChild(header);

              for (const row of rows) {
                const on = selected.has(row.id);
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "cb-row" + (on ? " on" : "");
                btn.dataset.key = "row:" + row.id;
                btn.setAttribute("aria-pressed", String(on));
                btn.title = `${row.group} - active ${row.months} of last 12 months`;
                btn.innerHTML =
                  `<span class="cb-name">${esc(row.category)}</span>` +
                  `<span class="cb-bar"><span class="cb-fill" ` +
                  `style="width:${Math.max((row.amount / maxAmount) * 100, 1)}%"></span></span>` +
                  `<span class="cb-amt">${fmt(row.amount)}</span>`;
                btn.addEventListener("click", () => toggle(row.id));
                section.appendChild(btn);
              }
              root.appendChild(section);
            });

            el.appendChild(root);
            if (focusKey) {
              const again = el.querySelector(`[data-key="${CSS.escape(focusKey)}"]`);
              if (again) again.focus();
            }
          }

          model.on("change:selected", draw);
          model.on("change:share_pct", draw);
          draw();
          // marimo remounts views against the same model and only runs the
          // cleanup render returns; without this, draw listeners accumulate.
          return () => {
            model.off("change:selected", draw);
            model.off("change:share_pct", draw);
          };
        }
        export default { render };
        """
        _css = """
        .cb {
          --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
          --line: #e1e0d9; --track: #f0efec;
          --t1: #2a78d6; --t2: #1baf7a; --t3: #eda100; --t4: #008300;
          font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
          background: var(--surface); color: var(--ink);
          border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px;
        }
        @media (prefers-color-scheme: dark) {
          .cb {
            --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7;
            --line: #2c2c2a; --track: #383835;
            --t1: #3987e5; --t2: #199e70; --t3: #c98500; --t4: #008300;
          }
        }
        /* marimo's theme is class-driven (a .dark/.light wrapper inside the
           plugin shadow root), not media-query-driven; these override the OS
           fallback above so the board follows the app theme. */
        .dark .cb {
          --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7;
          --line: #2c2c2a; --track: #383835;
          --t1: #3987e5; --t2: #199e70; --t3: #c98500; --t4: #008300;
        }
        .light .cb {
          --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e;
          --line: #e1e0d9; --track: #f0efec;
          --t1: #2a78d6; --t2: #1baf7a; --t3: #eda100; --t4: #008300;
        }
        .cb .tier-1 { --tier: var(--t1); } .cb .tier-2 { --tier: var(--t2); }
        .cb .tier-3 { --tier: var(--t3); } .cb .tier-4 { --tier: var(--t4); }
        .cb-head { display: flex; justify-content: space-between; align-items: flex-start;
          gap: 12px; flex-wrap: wrap; }
        .cb-hero { font-size: 28px; font-weight: 650; line-height: 1.1; }
        .cb-unit { font-size: 13px; font-weight: 400; color: var(--ink2); margin-left: 4px; }
        .cb-sub { font-size: 12px; color: var(--ink2); margin-top: 2px; }
        .cb-pcts { display: flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
        .cb-pct { font: inherit; font-size: 12px; padding: 4px 8px; border: 0;
          background: transparent; color: var(--ink2); cursor: pointer; }
        .cb-pct + .cb-pct { border-left: 1px solid var(--line); }
        .cb-pct.on { background: var(--ink); color: var(--surface); font-weight: 600; }
        .cb-meter { display: flex; gap: 2px; height: 10px; background: var(--track);
          border-radius: 4px; overflow: hidden; margin: 12px 0 4px; }
        .cb-seg { background: var(--tier); border-radius: 2px; }
        .cb-tier-head { display: flex; align-items: center; gap: 8px; margin: 12px 0 4px;
          font-size: 12px; }
        .cb-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--tier); }
        .cb-tier-name { font-weight: 600; }
        .cb-tier-sum { color: var(--muted); margin-left: auto;
          font-variant-numeric: tabular-nums; }
        .cb-mini { font: inherit; font-size: 11px; border: 0; background: transparent;
          color: var(--muted); cursor: pointer; padding: 2px 4px; }
        .cb-mini:hover { color: var(--ink); }
        .cb-row { display: grid; grid-template-columns: minmax(120px, 180px) 1fr 72px;
          gap: 10px; align-items: center; width: 100%; font: inherit; font-size: 12.5px;
          padding: 3px 6px; margin: 1px 0; border: 0; border-radius: 4px;
          background: transparent; color: var(--ink2); cursor: pointer; text-align: left; }
        .cb-row:hover { background: var(--track); }
        .cb-row.on { color: var(--ink); }
        .cb-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .cb-bar { height: 8px; background: var(--track); border-radius: 4px; overflow: hidden; }
        .cb-fill { display: block; height: 100%; border-radius: 4px 2px 2px 4px;
          background: var(--tier); opacity: 0.25; }
        .cb-row.on .cb-fill { opacity: 1; }
        .cb-amt { text-align: right; font-variant-numeric: tabular-nums; }
        .cb-row.on .cb-amt { font-weight: 600; }
        """
        items = traitlets.List([]).tag(sync=True)
        selected = traitlets.List([]).tag(sync=True)
        share_pct = traitlets.Int(50).tag(sync=True)

    _uids = set(fair_items["uid"].to_list())
    board = mo.ui.anywidget(
        ContributionBoard(
            items=[
                {
                    "id": row["uid"],
                    "category": row["category"],
                    "tier": row["tier"],
                    "group": row["group"],
                    # Unrounded so the board hero and the Python fair-share
                    # cell sum identical amounts; fmt() rounds display-side.
                    "amount": row["monthly_net"],
                    "months": row["months_active"],
                }
                for row in fair_items.iter_rows(named=True)
            ],
            selected=[uid for uid in LAST_BOARD["selected"] if uid in _uids],
            share_pct=int(LAST_BOARD["share_pct"]),
        )
    )

    def _remember(change):
        LAST_BOARD[change["name"]] = change["new"]

    board.widget.observe(_remember, names=["selected", "share_pct"])
    board
    return (board,)


@app.cell(hide_code=True)
def _(fair_items):
    mo.vstack(
        [
            mo.md(f"All **{fair_items.height}** cost items, sorted by tier then monthly net."),
            mo.ui.table(
                fair_items,
                page_size=45,
                selection=None,
                hidden_columns=["monthly_net", "uid"],
                label="Cost detail (reference; select on the board above)",
            ),
        ]
    )
    return


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
def _(board, fair_items):
    _chosen = board.value["selected"]
    _pct = int(board.value["share_pct"])
    _shared_total = float(fair_items.filter(pl.col("uid").is_in(_chosen))["monthly_net"].sum())
    fair_contribution = _shared_total * _pct / 100
    mo.md(
        f"### Fair-share estimate: **\\${fair_contribution:,.0f}/mo**\n\n"
        f"Selected shared costs: **\\${_shared_total:,.0f}/mo** at **{_pct}%**. "
        "This is a cost share, separate from the break-even contribution below.\n\n"
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
        f"fair share is \\${_difference:,.0f}/mo above break-even"
        if _difference >= 0
        else f"fair share leaves a \\${-_difference:,.0f}/mo monthly shortfall"
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
                f"## Break-even contribution: **\\${required_contribution:,.0f}/mo**\n\n"
                "The smallest recurring contribution that keeps household cash from shrinking each month, "
                "given the income kept and the plan above.\n\n"
                f"- Observed 12-month burn: **\\${primary['monthly_burn']:,.0f}/mo**\n"
                f"- Dependable income retained: **\\${_retained:,.0f}/mo**\n"
                f"- Planned cuts: **\\${_cuts:,.0f}/mo**\n"
                f"- Safety margin: **\\${_margin:,.0f}/mo**\n"
                f"- Fair-share estimate: **\\${fair_contribution:,.0f}/mo** - {_comparison}."
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

    - Compare the resulting break-even contribution with a fair-share proposal.
    - Add a specific incremental-cost estimate if household occupancy changes.
    """)
    return


if __name__ == "__main__":
    app.run()
