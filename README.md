# ynabx - YNAB eXplore

An experiment in agent-driven personal-data exploration, built around [YNAB](https://www.youneedabudget.com/) and its [REST API](https://api.ynab.com/).

## The hypothesis

Same shape as [jx](https://github.com/broadinstitute/jx) and [fgx](https://github.com/broadinstitute/fgx) - a catalog of marimo notebooks plus thin operational skills - applied to personal-finance data instead of scientific data.
Like fgx, ynabx hits a REST API rather than local files; unlike fgx, it delta-syncs into a local DuckDB so most analysis runs against the cache.

Each notebook exposes `@app.function` helpers; later notebooks import from earlier ones.
It rides the shared [vignette-catalog-skills](https://github.com/carpenter-singh-lab/vignette-catalog-skills) engine: the `vignette-catalog-compose-notebook` skill reads `catalog.toml` and tells an agent what's in the catalog and how to compose new analyses from it - "plan next month's assignments", "measure income stability", or "find open reimbursements" - rather than reinventing the SQL each time.

## What's in here

The notebook map - each notebook, its importable helpers, and what it does - lives in [`catalog.toml`](catalog.toml) (the `[[vignette]]` table), the single source both agents and humans read.
Browse it there rather than a second copy that drifts.

## Quickstart

You need [`just`](https://just.systems/), [`uv`](https://docs.astral.sh/uv/), and a YNAB Personal Access Token (app.ynab.com -> Account Settings -> Developer Settings -> New Token).

```bash
export YNAB_TOKEN=...                # your PAT
just nb 1                            # settle the cell graph, then open it
```

That's it.
Use `just nb 8`; the argument may also be written as `08` or `nb08` without supplying the full filename.
The command starts or reuses the catalog's sandboxed marimo session, runs the cell graph, waits for every initially runnable cell to settle, prints its status, and then opens the ready URL in the default browser.
Cells guarded by an interactive choice remain waiting until that choice is made.
Run `nb02_ynab_sync.py` next to populate the local cache, then use `nb07`, `nb08`, `nb09`, or `nb11` for the question at hand.

## Configuration (env vars)

| Var | Purpose | Default |
|---|---|---|
| `YNAB_TOKEN` | YNAB Personal Access Token. | (required, unless `YNAB_OP_REF` is set) |
| `YNAB_OP_REF` | 1Password secret reference (e.g. `op://Vault/Item/Field`). Read via the `op` CLI when `YNAB_TOKEN` is unset. | unset |
| `YNAB_DB_PATH` | Where to put the DuckDB cache. | `./data/ynab.db` |

## Amazon transaction database

Amazon transaction data uses a separate SQLite file because it comes from authenticated browser snapshots rather than the YNAB API.
The saved JSON snapshots are the audit record, and `data/amazon.sqlite3` is a derived index that can be deleted and rebuilt.

Place dated payment and order snapshots under `data/external/raw/amazon/` using these filename prefixes:

- `amazon_payment_transactions_*.json`
- `amazon_orders_*.json`

Then rebuild the database:

```bash
just amazon-db
```

The importer searches subdirectories, so future refreshes may use one directory per scrape date.
It hashes every source file, records source provenance, deduplicates overlapping payment snapshots, and atomically replaces the database only after validation succeeds.
The importer collapses rows repeated across overlapping Amazon result pages, while it preserves identical charges that appear more than once on the same page.
The `current_payment_transactions` view hides a pending row after a matching completed charge appears in a later snapshot.
Money is stored as integer cents, and dates are stored as ISO `YYYY-MM-DD` text.

Use `transaction_details` for the joined payment and item view, or `store_card_transaction_details` for Store Card rows only.
For example:

```bash
sqlite3 -header -column data/amazon.sqlite3 \
  "SELECT transaction_date, amount_cents / 100.0 AS amount, item_titles FROM store_card_transaction_details LIMIT 20;"
```

To refresh it, save the new raw snapshots alongside the old ones and run `just amazon-db` again.
Older snapshots stay untouched, so a schema or importer change can always rebuild the same logical database from the original observations.

### Authenticated Amazon refresh

The authenticated browser exporter is `scripts/export_amazon.mjs`.
It accepts either a Codex Chrome tab or a standard Playwright Page, and it never reads cookies or browser storage.
It uses the signed-in page to collect Your Payments, regular orders, and digital orders.

First, open Amazon Your Orders in Chrome and sign in.
Then ask Codex to claim that tab and run `writeAmazonSnapshot()` from the exporter.
The module writes an immutable timestamped directory under `data/external/raw/amazon/snapshots/`, including SHA256 hashes in `manifest.json`.
After the exporter finishes, run `just amazon-db` to rebuild SQLite.

For a Codex browser session where the claimed tab is named `amazonTab`, scrape in stages so each browser call stays below the tool deadline:

```javascript
const { pathToFileURL } = await import("node:url");
const exporter = await import(pathToFileURL(`${nodeRepl.cwd}/scripts/export_amazon.mjs`).href);
const payments = await exporter.scrapePayments(amazonTab);
const recentOrders = await exporter.scrapeOrders(amazonTab, {
  years: [2026, 2025, 2024, 2023],
});
const olderOrders = await exporter.scrapeOrders(amazonTab, {
  years: [2022, 2021, 2020],
});
const orders = [...new Map([...recentOrders, ...olderOrders].map((order) => [order.order_id, order])).values()];
await exporter.writeAmazonDataSnapshot({ payments, orders }, {
  outputRoot: `${nodeRepl.cwd}/data/external/raw/amazon/snapshots`,
  onProgress: (event) => nodeRepl.write(event),
});
```

## Design notes

- **Each notebook is self-runnable.** PEP 723 inline dependency blocks mean `uv run` handles the venv per file.
- **`@app.function` helpers are importable.** Marimo promotes single-def cells to module-level functions.
  Sibling notebooks `from nb01_ynab_client import get` after adding `notebooks/` to `sys.path`.
  No package install required.
- **DuckDB is the source of truth for analysis.** `nb01` provides thin live HTTP wrappers, `nb02` syncs the cache, and `nb07` and `nb11` fetch budget data where needed.
- **There is no transaction write path.** Make transaction changes in the YNAB UI; `nb07` can write budget assignments and defaults to a dry run.

### Always-on filters for spend aggregations

Three predicates appear in every "how much / how many" SQL query - skip them and the number will be wrong:

- `NOT deleted` - YNAB soft-deletes; tombstones stay in the table.
- `(parent_id IS NULL OR has_splits = FALSE)` - prevents double-counting split parents alongside their subtransactions.
- `COALESCE(payee_name, '') NOT LIKE 'Transfer :%'` - inter-account transfers don't carry categories on the budget side.

This split filter is for **category-scoped spend** ("what did I spend on X"), where a `WHERE category_name = ?` clause excludes the split parent.
For an **account balance or statement match** use `parent_id IS NULL` instead - the spend filter also admits split children and double-counts, inflating the balance by roughly an order of magnitude.
See the splits note in `AGENTS.md`.

Amounts are stored in **milliunits** (x1000); divide by 1000.0 for dollars.

## Composing analyses

For Claude Code and Codex users: the catalog rides the shared [vignette-catalog-skills](https://github.com/carpenter-singh-lab/vignette-catalog-skills) engine.
The skills are recorded in `skills-lock.json` but not vendored, so restore them once after cloning:

```bash
npx skills@1.5.20 add vercel-labs/agent-browser -s agent-browser -a claude-code -a codex -y
npx skills@1.5.20 add marimo-team/skills -s anywidget-generator -s marimo-notebook -a claude-code -a codex -y
npx skills@1.5.20 add marimo-team/marimo-pair -s marimo-pair -a claude-code -a codex -y
npx skills@1.5.20 add 'carpenter-singh-lab/vignette-catalog-skills#v0.5.1' -s vignette-catalog-compose-notebook -s vignette-catalog-scaffold -a claude-code -a codex -y
```

Then the `vignette-catalog-compose-notebook` skill walks Claude through reusing the catalog (it reads `catalog.toml` for the helper inventory and `AGENTS.md` for the domain invariants) instead of writing fresh DuckDB queries.
See [AGENTS.md](AGENTS.md) for the full agent contract.

## License

MIT - see `LICENSE`.
