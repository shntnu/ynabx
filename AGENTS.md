# AGENTS.md - ynabx

Project-specific guidance for agents working in this catalog.
`README.md` is the human entry point.
This catalog uses the shared vignette-catalog-skills (`vignette-catalog-setup`, `vignette-catalog-compose-notebook`); its specifics live in `catalog.toml`.

ynabx holds **real personal-finance data**. Treat every output as private - see "Privacy" below.

## Skills (restore after clone)

The catalog skills are installed via `npx skills add`, recorded in the tracked `skills-lock.json`, but **not vendored** -
the install stores (`.agents/`, `.claude/skills/*`) are gitignored. A fresh clone has only the lock. Run once, from the repo root:

    npx skills update

This reconstitutes every skill the lock pins. Do this before relying on the skills or the validation rule.

## Launching notebooks

Always use `--sandbox` so PEP 723 inline metadata is provisioned:

    uvx marimo edit --sandbox notebooks/nbNN_*.py

Do not improvise alternative launch commands.

## Privacy (this catalog is the exception)

Sibling catalogs (dmx, jx, fgx) commit molab session snapshots because their data is public. **ynabx must not.**

- `notebooks/__marimo__/` is gitignored. Never force-add it.
- `data/` (the DuckDB cache and exports) is gitignored wholesale. Never commit anything under it.
- Clear cell outputs before committing any notebook. `marimo check --fix` does not strip outputs, so eyeball the diff of any notebook you ran for embedded payees, amounts, or account names before `git add`.
- Docs and tests use only synthetic placeholders (`op://Vault/Item/Field`, fake tokens). Keep it that way - no real payees, amounts, or budget ids in tracked files.

## Validation rule

After composing or editing any notebook, run the `validate-notebook.sh` bundled with the installed `vignette-catalog-compose-notebook` skill, passing the notebook path, then open it and look at the outputs.
Static checks do not catch wrong outputs, empty tables, stale endpoints, broken plots, or sign-convention mistakes.
Then clear outputs before commit (see Privacy).

## Architecture

- Catalog over library. Helpers are top-level `@app.function` cells in numbered notebooks; later notebooks import them via `sys.path` + plain `from nb01_ynab_client import get`.
- Data surface: a local **DuckDB cache** (`data/ynab.db`) is the source of truth for analysis; `nb02.sync()` delta-syncs it from the **YNAB REST API** (`api.ynab.com/v1`) via the `server_knowledge` cursor. Only sync (nb02) and writes (nb04) touch the network.
- Do not add a Python package until repeated cross-notebook imports make it painful.

## Domain invariants (get these wrong and the numbers are wrong)

- **Milliunits.** Amounts are stored x1000. Always `amount_milli / 1000.0` for dollars.
- **Three always-on filters** for any spend aggregation:
  - `NOT deleted` - YNAB soft-deletes; tombstones stay in the table.
  - `(parent_id IS NULL OR has_splits = FALSE)` - prevents double-counting split parents alongside their subtransactions.
  - `COALESCE(payee_name, '') NOT LIKE 'Transfer :%'` - inter-account transfers carry no category on the budget side.
- **Writes are dry-run by default.** Anything that mutates YNAB goes through `nb04.apply_edits(plan, dry_run=False)` - never `nb01.patch(...)` directly, or the cache desyncs. Show the plan and get confirmation before flipping the flag.
- **Budgets.** `active_budget_id()` picks the most-recently-modified budget; pass an explicit id from `list_budgets()` for any other.
- **Auth.** `get_token()` reads `YNAB_TOKEN`, else `op read $YNAB_OP_REF`. Token is cached at module level (1Password biometric prompts time out between calls). `op` "authorization timeout" = 1Password app locked; unlock and retry.

## Conventions

Semantic line breaks in markdown. ASCII-only. Conventional Commits. `ruff line-length = 120` is Python only.

## Canonical contract (read before editing)

The full contract lives in the installed `vignette-catalog-compose-notebook` skill's `references/` - notebook conventions, the data contract, indexing, the `catalog.toml` schema, and marimo gotchas. Read the relevant one before authoring or editing a notebook. Restore with `npx skills update` if the skill store is empty.

## When the question fits the catalog

The notebook-to-question routing lives in the `[[vignette]]` table in `catalog.toml` - the single source the compose skill reads. Do not mirror it here.
