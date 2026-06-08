# YNAB

Marimo notebook catalog over the YNAB API. Each notebook exposes
`@app.function` helpers; later notebooks import from earlier ones via
`sys.path` + plain `from nb01_ynab_client import get`. Each is
self-runnable: `uv run --with marimo marimo edit --no-token notebooks/nbNN_*.py`.

## Composing analyses

For any budget question - spending breakdowns, categorization fixes,
exports, payee analysis - **reuse the catalog instead of writing fresh
DuckDB queries against `data/ynab.db`.** The
[`compose-ynab-notebook`](../.claude/skills/compose-ynab-notebook/SKILL.md)
skill holds the full catalog table, the composition pattern, and the
SQL filters that keep aggregations correct (split-parent dedup,
transfer exclusion, deleted tombstones). It triggers on phrasings like
"what did I spend on X", "top payees", "categorize the recent stuff",
"export Q1".

Quick pointer: `nb01` (auth + HTTP), `nb02` (DuckDB cache + delta sync),
`nb03` (review with payee-history suggestions), `nb04` (bulk edit,
dry-run by default), `nb05` (parquet/csv exports + summaries),
`nb06` (reconcile a bank statement CSV against an account - amount-pooled
matching, read-only diff).

## Auth

`nb01.get_token()` resolves the YNAB Personal Access Token in this order:

1. `YNAB_TOKEN` env var - simplest; works in CI, one-off scripts, `.env` files.
2. `op read $YNAB_OP_REF` if `YNAB_OP_REF` is set (e.g. `op://Personal/YNAB/PAT`)
   - requires the 1Password CLI and desktop app integration enabled
   (Settings -> Developer -> "Integrate with 1Password CLI").

`ynab/.envrc` (direnv) auto-exports `YNAB_OP_REF` whenever the shell
enters this directory, so interactive use and Claude sessions don't
have to set it by hand. The file is gitignored (it encodes a personal
1P vault path); a tracked `.envrc.example` shows the format. Fresh
clone setup: `cp ynab/.envrc.example ynab/.envrc`, edit if your 1P
path differs, then `direnv allow ynab/`.

The token is **cached at module level** because biometric prompts time
out between calls and back-to-back `op` calls fail.

Get a token at app.ynab.com -> Account Settings -> Developer Settings ->
New Token.

## Running ad-hoc Python against the catalog

For one-shot exploration that imports catalog helpers (instead of
opening a notebook), use this incantation:

From the ynab/ root:

```bash
uv run --with marimo --with requests --with duckdb --with polars --with pyarrow python3 -c "
import sys
sys.path.insert(0, 'notebooks')
from nb02_ynab_sync import connect
from nb05_export import payee_totals
print(payee_totals(90, 10))
"
```

From a sibling directory (e.g. `admin/`), prefix the sys.path with
`../ynab/notebooks` instead.

Two reasons every dep is needed even for trivial calls:

- `nb01_ynab_client` imports `marimo` and `requests` at module top, so
  any import of any catalog file pulls them.
- `nb02` adds `duckdb` and `polars`; `nb05` adds `pyarrow` (via
  `polars.write_parquet` / DuckDB's `.pl()`).

`YNAB_OP_REF` is loaded automatically by `.envrc`, so no need to set it
inline.

## Data

Local cache: `data/ynab.db` (DuckDB, gitignored). Override the location
with `YNAB_DB_PATH` if you want it elsewhere. Refresh with `nb02.sync()`.
Idempotent and cheap when there are no changes.

## Things to remember

- **Amounts are in milliunits** (x1000). Always `amount_milli / 1000.0`
  for dollars.
- **Splits**: parent transactions have `has_splits=TRUE`; subtransactions
  are separate rows with `parent_id` set. The right filter depends on the
  question:
  - **Account balance / statement matching** (e.g. `nb06`): use
    `parent_id IS NULL`. This counts each split once at the parent's full
    amount and drops the children - which is exactly what the bank posts
    (one charge per split). `(parent_id IS NULL OR has_splits=FALSE)` is
    WRONG here: it also admits the children and double-counts, blowing the
    balance up by roughly an order of magnitude once every split child is
    summed on top of its parent.
  - **Category-scoped spend** (e.g. `nb05`, "what did I spend on X"): use
    `(parent_id IS NULL OR has_splits=FALSE)`. The double-count is harmless
    only because a `WHERE category_name = ?` filter excludes the split
    parent (its category is `Split`/null), leaving just the child. Don't
    use this filter for an unfiltered total - it double-counts.
- **Transfers**: payee starts with `Transfer :`. They don't carry
  categories on the budget side. Filter them out for review/spending
  analyses.
- **Budget**: `active_budget_id()` picks the most-recently-modified
  budget visible to the token. If you have multiple budgets and want a
  different one, look it up via `list_budgets()` and pass the id
  explicitly to functions that take `budget_id`.
- **Bulk edits**: build a plan, override individual rows in the
  `overrides` dict in `nb04`, then `apply_edits(plan, dry_run=False)`.
  Dry-run is the default.

## marimo workflow gotchas

- **Each `uv run marimo edit` spawns its own venv**, so deps need
  re-installing per kernel. From inside the kernel:

  ```python
  import marimo._code_mode as cm
  async with cm.get_context() as ctx:
      ctx.packages.add("requests")
  ```

  Don't `uv add` - it doesn't reach the kernel.

- **`@app.function` only applies to single-function cells.** Marimo
  auto-promotes a cell to `@app.function` (and thus importable from
  sibling notebooks) only when it contains exactly one top-level def.
  Multi-def cells become `@app.cell` (cell-private). Split helpers into
  one-cell-per-function when you want them importable.

- **Underscore-prefixed names are cell-private.** `_session` defined in
  cell A is renamed to `_cell_A_session` and won't be visible from cell
  B. Use non-underscore names for any helper that needs to be shared
  across cells.

- **Composed notebooks need transitive deps in PEP 723.** A new file
  that imports `nb02` transitively pulls in `nb01`'s `requests`. A
  file that uses `polars.write_parquet` or DuckDB's `.pl()` needs
  `pyarrow`. The compose-notebook skill has a safe-by-default block.

- **`op` returning "authorization timeout" = 1P desktop app is
  locked.** Unlock it (Touch ID prompt) and retry. The token is cached
  at module level after first success so subsequent calls within the
  same kernel won't re-prompt. If you don't use 1Password, set
  `YNAB_TOKEN` directly and skip `op` entirely.
