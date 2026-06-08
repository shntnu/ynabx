---
name: compose-ynab-notebook
description: Compose a new marimo notebook (or a quick scratch script) by reusing @app.function helpers from the ynab catalog (notebooks/nb01_ynab_client.py through nb05_export.py) to answer a YNAB / personal-finance question end-to-end - e.g. "what did I spend on camps last year", "show me top payees for the last 90 days", "find transactions where the category disagrees with payee history", "categorize the recent uncategorized stuff", "monthly breakdown by category", "export Q1 to a parquet". Trigger whenever the user asks for a budget number, a spending breakdown, a categorization fix, a transaction search, an export, or any analysis touching their YNAB data - even if they don't say "marimo" or "reuse the catalog". Use this instead of opening data/ynab.db with raw duckdb and writing a fresh query, and instead of duplicating logic that already lives in nb02-nb05.
---

# Compose a YNAB analysis from the ynab catalog

## What this skill is for

This `ynab/` folder is a catalog of marimo notebooks whose `@app.function`
helpers handle all the plumbing for the user's personal YNAB data:
auth via 1Password, delta sync into a local DuckDB, payee-history
suggestions, bulk patches, exports. When the user asks a budget
question - "how much did I spend on X", "what's my top payee", "fix
these uncategorized ones" - **compose from the catalog, don't reinvent
the query.**

Two reasons:

1. **The local DuckDB cache is the source of truth for analysis.** It
   already has split-handling (`parent_id`, `has_splits`), milliunit
   amounts, transfer-payee filtering rules, and 12+ years of history.
   Writing a fresh `duckdb.connect("data/ynab.db")` script silently
   skips all that.
2. **Writes go through the catalog, not direct API calls.** `nb04`'s
   `apply_edits()` is dry-run by default, batches via the YNAB bulk
   PATCH endpoint, and re-runs `nb02.sync()` afterwards. Skipping it is
   how you accidentally desync the cache.

## The catalog at a glance

Every catalog file uses `with app.setup` + top-level `@app.function`s
(safe to import) + UI cells exercising the helpers. The functions are
the contract.

| Module | Reusable functions | What they do |
|---|---|---|
| `nb01_ynab_client` | `get_token()`, `get(path, **params)`, `patch(path, body)`, `list_budgets()`, `active_budget_id()` | Auth via `op` CLI (token cached at module level); thin HTTP wrappers over `https://api.ynab.com/v1`; budget discovery (active = most-recently-modified). |
| `nb02_ynab_sync` | `connect()`, `last_knowledge(con, budget_id)`, `sync(budget_id=None)`, `status()` | Open DuckDB at `data/ynab.db`, ensure schema, delta-sync via YNAB's `server_knowledge` cursor (idempotent), report cache state. |
| `nb03_review` | `payee_top_categories(con, payee_id, days=180)`, `suggest_category(con, payee_id, days=180)`, `pending(days=30, history_days=180)` | Pending-review table (uncategorized / unapproved / uncleared, transfers excluded) with payee-history suggestions. Read-only. |
| `nb04_bulk_edit` | `propose_apply_suggestions(days=30, history_days=180, min_history=2)`, `apply_edits(plan, dry_run=True)` | Build a patch plan from `pending()`, optionally apply via YNAB bulk PATCH. **Default is dry-run.** Auto re-syncs cache after a real apply. |
| `nb05_export` | `export_transactions(path, since, until, format)`, `monthly_category_summary(year)`, `payee_totals(days, top_n)` | Parquet/csv dumps to `data/exports/`, month x category spend, top payees by absolute spend. |
| `nb06_reconcile` | `load_statement(path, bank='fidelity')`, `reconcile(account_name, statement, since, until)` | Parse a bank statement CSV (per-bank column map in `BANK_FORMATS`) and diff it against an account: amount-pooled matching returns charges missing from YNAB, YNAB rows not on the statement, and the cleared-vs-bank delta. Read-only. Balance/matching uses `parent_id IS NULL` (not the spend filter) - see the splits note in CLAUDE.md. |

When the question isn't obviously one of these, **read the catalog file
itself** (not just this table) before inventing new code. Helpers have
docstrings and the UI cells are worked examples.

## The composition pattern

A composed analysis is a new file - either a marimo notebook in
`notebooks/` (e.g. `nb_camps_2025.py`) or a one-off script - that
imports catalog helpers as plain Python and glues them.

### Setup cell - plain Python imports

Each catalog file uses an `nbNN_` prefix so the file is a valid Python
module name. Import them by adding `notebooks/` to `sys.path` and using
a regular `from ...` line. No `importlib`, no dynamic loading - marimo's
`@app.function` decorator exposes the functions at module top level.

```python
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
    from nb03_review import pending, payee_top_categories
    from nb05_export import monthly_category_summary, payee_totals
```

For a one-off script (no marimo, just `uv run --script`), the same
`sys.path` + import pattern works - just put it at module top instead
of `with app.setup`.

### PEP 723 dependency block - declare transitive deps

Each catalog notebook ships its own deps, but a *composed* notebook
needs the union of every catalog file it imports. The current
transitive set:

- `nb01` brings in: `requests`
- `nb02` brings in: `duckdb`, `polars`, `requests` (via nb01)
- `nb03`, `nb04` import nb01+nb02, so include `requests` in your block
- `nb05` uses DuckDB's `.pl()` and `polars.write_parquet`, both go
  through `pyarrow` - include it if you write parquet

A safe-by-default block for any composed notebook touching the cache
plus parquet:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb", "marimo", "polars", "pyarrow", "requests"]
# ///
```

### Dry-run is the default for writes

Anything that mutates YNAB lives behind `apply_edits(plan,
dry_run=False)`. Compose a plan first, show it to the user, get
explicit confirmation, then flip the flag. Never call `nb01.patch(...)`
directly to write transactions - go through `apply_edits` so the cache
stays in sync.

If the user asks for a one-shot edit that doesn't fit the
payee-history-suggestion shape, build a `pl.DataFrame` with `id` and
`suggested_category_id` columns and pass it to `apply_edits` - the
function only needs those two columns.

### Read directly from the cache for analysis

For any "how much / how many / top N" question, open `connect()` and
write SQL against the `transactions` table. Don't go to the YNAB API
for analysis - the cache is faster, history-complete, and identical to
what the API would return at last sync.

```python
@app.function
def spend_on_category(category: str, since: str) -> float:
    con = connect()
    row = con.execute(
        """
        SELECT SUM(amount_milli)/1000.0
        FROM transactions
        WHERE NOT deleted
          AND (parent_id IS NULL OR has_splits = FALSE)
          AND category_name = ?
          AND date >= ?
          AND COALESCE(payee_name, '') NOT LIKE 'Transfer :%'
        """,
        [category, since],
    ).fetchone()
    con.close()
    return row[0] or 0.0
```

Three things that are *always* in the WHERE clause for spend
aggregations:

- `NOT deleted` - YNAB soft-deletes; tombstones stay in the table.
- `(parent_id IS NULL OR has_splits = FALSE)` - prevents double-counting
  split parents alongside their subtransactions.
- `COALESCE(payee_name, '') NOT LIKE 'Transfer :%'` - inter-account
  transfers don't carry categories on the budget side and skew sums.

Skip these and the number will be wrong in subtle ways.

### Interactive UI - widgets, not raw prints

If you're composing an actual notebook (not a one-off script), lean on
marimo widgets so the user can change the question without editing
code:

- `mo.ui.dropdown` for category / account / month pickers (build the
  options list with a quick `SELECT DISTINCT` against the cache).
- `mo.ui.text` for free-text payee search.
- `mo.ui.slider` for `days` / `top_n` / `min_history`.
- `mo.ui.date_range` for since/until windows.
- `mo.ui.run_button` + `mo.stop(not run.value)` to gate any cell that
  hits the YNAB API or applies edits. Reads from the local cache are
  cheap enough not to need a button.
- `mo.ui.table(df, selection="single")` when the user might click a
  row to drill in (e.g. show split detail, jump to payee history).
- Consolidate controls in `mo.sidebar([...])` so they stay visible
  while scrolling results.

### Selection + paging

When a widget's `.value` drives both a control and a display, split
into two cells - marimo doesn't let a cell read `.value` from a widget
it also creates.

## Things to remember (YNAB-specific gotchas)

- **Amounts are in milliunits** (x1000). Always `amount_milli / 1000.0`
  for dollars. The cache stores raw milliunits.
- **Splits**: parent rows have `has_splits=TRUE` and an
  `amount_milli` equal to the sum of children. Subtransactions have
  `parent_id` set. Aggregating without the parent-filter
  double-counts.
- **Transfers**: payee starts with `Transfer :`. They don't carry
  categories on the budget side. Filter them out for review and spend
  analyses (but include them if the question is literally "how much
  moved between accounts").
- **Multiple budgets exist.** `active_budget_id()` picks the
  most-recently-modified one. If the user asks about a different budget
  by name, look it up in `list_budgets()` and pass the id explicitly.
- **`server_knowledge` cursor.** `nb02.sync()` is incremental and
  cheap. Call it before any analysis that needs to be current. Don't
  re-implement sync; just call `sync()`.
- **Auth.** `get_token()` reads `YNAB_TOKEN` env var first, then falls
  back to `op read $YNAB_OP_REF` if that env var is set. If `op` returns
  "authorization timeout", the 1P desktop app is locked - unlock and
  retry. The token is cached at module level after first success.
- **Marimo + `@app.function` rules.** `@app.function` only applies to
  cells with exactly one top-level `def` (no other code, no other
  defs). Underscore-prefixed names (`_helper`, `_session`) are
  cell-private and won't import across notebooks. Drop the underscore
  to share.

## Process for a new composition

1. **Turn the English question into catalog calls.** Which `nb0N`
   function gives you each step? If something's missing, read the
   catalog file before deciding to write fresh code.
2. **Sync the cache first** if the question is about recent activity:
   `nb02.sync()` is idempotent and fast.
3. **For analysis**, open `connect()`, write SQL against
   `transactions`, remember the three always-on filters above. Cast
   `amount_milli/1000.0` as `amount` in the SELECT.
4. **For categorization fixes**, start from `nb03.pending()` and
   `nb04.propose_apply_suggestions()`. If the auto-suggestion is
   wrong, layer a manual override dict on top of the plan (see nb04's
   UI cell for the pattern) before calling `apply_edits`.
5. **For exports**, prefer `nb05.export_transactions()` over rolling
   your own write-parquet loop - it handles the deleted/split/transfer
   filters consistently.
6. **Show the plan / preview to the user before any write**, even when
   they sound sure. The cost of a wrong dry-run is a re-render; the
   cost of a wrong real edit is hand-fixing N transactions in YNAB.

## When *not* to use this skill

- Editing a catalog notebook itself (e.g. fixing a bug in
  `nb02_ynab_sync.py`) - edit that file directly.
- Anything that doesn't touch the user's YNAB data: pure infra (venv,
  CI), unrelated personal-finance topics (taxes, investments outside
  YNAB), or general budgeting advice not grounded in their actual
  transactions.
- Pure CLI tasks (`op` setup, DuckDB CLI inspection) - just run the
  command.
