# ynabx - YNAB eXplore

An experiment in agent-driven personal-data exploration, built around
[YNAB](https://www.youneedabudget.com/) and its
[REST API](https://api.ynab.com/).

## The hypothesis

Same shape as [jx](https://github.com/broadinstitute/jx) and
[fgx](https://github.com/broadinstitute/fgx) - a catalog of marimo
notebooks plus thin operational skills - applied to personal-finance
data instead of scientific data. Like fgx, ynabx hits a REST API rather
than local files; unlike fgx, it delta-syncs into a local DuckDB so
analysis runs against the cache and only sync/writes touch the network.

Each notebook exposes `@app.function` helpers; later notebooks import
from earlier ones. A thin `compose-ynab-notebook` skill tells an agent
(Claude Code) what's in the catalog and how to compose new analyses
from it - "what did I spend on camps last year", "categorize the recent
uncategorized stuff", "export Q1 to parquet" - rather than reinventing
the SQL each time.

## What's in here

| Notebook | What it does |
|---|---|
| `nb01_ynab_client.py` | Auth + thin HTTP wrappers (`get`, `patch`) + budget discovery. |
| `nb02_ynab_sync.py` | Delta-sync transactions into a local DuckDB cache (idempotent). |
| `nb03_review.py` | Pending-review table with payee-history category suggestions. |
| `nb04_bulk_edit.py` | Build a patch plan and apply via the YNAB bulk endpoint. Dry-run by default. |
| `nb05_export.py` | Parquet/CSV exports, monthly category summaries, top payees. |

## Quickstart

You need [`uv`](https://docs.astral.sh/uv/) and a YNAB Personal Access
Token (app.ynab.com -> Account Settings -> Developer Settings ->
New Token).

```bash
export YNAB_TOKEN=...                # your PAT
uv run --with marimo marimo edit --no-token notebooks/nb01_ynab_client.py
```

That's it. Run `nb02_ynab_sync.py` next to populate the local cache,
then explore `nb03`/`nb04`/`nb05`.

## Configuration (env vars)

| Var | Purpose | Default |
|---|---|---|
| `YNAB_TOKEN` | YNAB Personal Access Token. | (required, unless `YNAB_OP_REF` is set) |
| `YNAB_OP_REF` | 1Password secret reference (e.g. `op://Personal/YNAB/PAT`). Read via the `op` CLI when `YNAB_TOKEN` is unset. | unset |
| `YNAB_DB_PATH` | Where to put the DuckDB cache. | `./data/ynab.db` |

## Design notes

- **Each notebook is self-runnable.** PEP 723 inline dependency blocks
  mean `uv run` handles the venv per file.
- **`@app.function` helpers are importable.** Marimo promotes
  single-def cells to module-level functions. Sibling notebooks
  `from nb01_ynab_client import get` after adding `notebooks/` to
  `sys.path`. No package install required.
- **DuckDB is the source of truth for analysis.** The API is hit only
  for sync (`nb02`) and writes (`nb04`).
- **Writes are dry-run by default.** `nb04.apply_edits(plan,
  dry_run=False)` is the only thing that mutates YNAB.

### Always-on filters for spend aggregations

Three predicates appear in every "how much / how many" SQL query - skip
them and the number will be wrong:

- `NOT deleted` - YNAB soft-deletes; tombstones stay in the table.
- `(parent_id IS NULL OR has_splits = FALSE)` - prevents
  double-counting split parents alongside their subtransactions.
- `COALESCE(payee_name, '') NOT LIKE 'Transfer :%'` - inter-account
  transfers don't carry categories on the budget side.

Amounts are stored in **milliunits** (×1000); divide by 1000.0 for
dollars.

## Composing analyses

For Claude Code users: the `compose-ynab-notebook` skill (under
`.claude/skills/`) walks Claude through reusing the catalog instead of
writing fresh DuckDB queries.

## License

MIT - see `LICENSE`.
