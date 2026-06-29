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
- `data/` (the DuckDB cache and the `external/raw/interim/processed` tiers) is gitignored wholesale. Never commit anything under it.
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
- **Filters for spend aggregation** (skip any and the number is wrong):
  - `NOT deleted` - YNAB soft-deletes; tombstones stay in the table.
  - `COALESCE(payee_name, '') NOT LIKE 'Transfer :%'` - inter-account transfers carry no category on the budget side.
  - **Split filter is question-dependent.** For category-scoped spend ("what did I spend on X", e.g. nb05) use `(parent_id IS NULL OR has_splits = FALSE)` - the `WHERE category_name = ?` clause excludes the split parent. For an account balance or statement match (e.g. nb06) use `parent_id IS NULL` instead; the spend filter also admits split children and double-counts, inflating the balance by ~10x.
- **Writes are dry-run by default.** Anything that mutates YNAB goes through `nb04.apply_edits(plan, dry_run=False)` - never `nb01.patch(...)` directly, or the cache desyncs. Show the plan and get confirmation before flipping the flag.
- **Budgets.** `active_budget_id()` picks the most-recently-modified budget; pass an explicit id from `list_budgets()` for any other.
- **Auth.** `get_token()` reads `YNAB_TOKEN`, else `op read $YNAB_OP_REF`. Token is cached at module level (1Password biometric prompts time out between calls). `op` "authorization timeout" = 1Password app locked; unlock and retry.

## Reconciliation & import mechanics (hard-won; this recurs every reconcile)

The catalog reads and analyzes; reconciliation itself is mostly native-app plus irreducible human verification the API can't automate. Know these before touching `cleared`/`reconciled` state, deleting transfer legs, or "correcting" a balance.

- **Three states.** `uncleared` (entered, bank hasn't confirmed) -> `cleared` (confirmed/matched) -> `reconciled` (locked against a statement). `import_id` present = bank-fed/real; `import_id IS NULL` = manual entry OR a scheduled-transaction placeholder. Null-import rows are not backlog - they are predictions awaiting a bank match.
- **Transfers have two legs.** The bank-linked side clears via import; the manual/tracking side (loan, 401k, asset accounts) never auto-clears - mark it cleared by hand when reconciling those accounts. Deleting one leg via the API deletes both.
- **CC-payment double-entry trap.** When both the paying checking account and the credit card are linked, one payment imports from *both* sides. If the two imports post a few days apart, YNAB fails to merge them, treats each as its own transfer, and invents the missing counter-leg - guessing the source checking account, often wrong. Result: a duplicate payment with a phantom leg. Prevention: **match** the two imported legs into one transfer; never approve them as two separate transfers, and don't let a manual/scheduled CC-payment entry compete with the imports.
- **Matched-import absorption.** When YNAB matches an import into an existing transaction, the imported row 404s on single-GET but lingers in the DuckDB cache as a separate `import_id` row. `nb02.reconcile()` prunes these (full `since_date` pull, prune-by-absence, size-guarded). Confirm a suspected twin with a single-GET (404 = absorbed, prune it; 200 = live, leave it).
- **Statement balance != current balance.** The statement balance is a snapshot at the close date; the current balance includes everything posted since. Reconcile to the *current* balance. A YNAB-vs-statement "mismatch" is often just a legitimate post-statement charge.
- **Connection tiers.** `direct_import_linked` says an account is connected, but the API does NOT expose the bank-feed balance. In-app, connected accounts split into balance-confirmed (green auto-match - trust the click) and transaction-only ("Is your balance X?" - YNAB just echoes its own number; verify against the real account yourself).
- **Tracking/investment accounts reconcile by adjust-to-value** (market drift is expected; you don't record trades). The API cannot create the native "Reconciliation Balance Adjustment" payee (it is a blocked internal name); use the in-app Reconcile button, or post a plain transaction for the delta.
- **A missing recurring charge may be an import GAP, not a cancelled sub.** Check import continuity (date gaps among `import_id` rows) before concluding a subscription lapsed; File-Import the statement (OFX/QFX/CSV) to backfill - YNAB dedupes by `import_id`.
- **Don't blind-adjust an on-budget checking discrepancy.** A large gap there is missing transactions (import them - the real activity matters), not market drift; an on-budget reconciliation adjustment silently distorts the budget.
- **Verify against external truth before destructive ops.** The cache only reflects what YNAB imported, which lags or gaps versus the bank; absence in the cache != absence in reality. Confirm against the statement / current balance / a single-GET before deleting or "fixing", and weight the account owner's domain knowledge heavily.

## Conventions

Semantic line breaks in markdown. ASCII-only. Conventional Commits. `ruff line-length = 120` is Python only.

## Canonical contract (read before editing)

The full contract lives in the installed `vignette-catalog-compose-notebook` skill's `references/` - notebook conventions, the data contract, indexing, the `catalog.toml` schema, and marimo gotchas. Read the relevant one before authoring or editing a notebook. Restore with `npx skills update` if the skill store is empty.

## When the question fits the catalog

The notebook-to-question routing lives in the `[[vignette]]` table in `catalog.toml` - the single source the compose skill reads. Do not mirror it here.
