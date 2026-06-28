# GOALS.md - the ynabx loop spine

This file is the durable backbone of an agent-driven personal-finance loop.
The notebooks are stateless tools; this file is what carries intent across sessions, so session N+1 knows where session N left off.

Your real numbers do not live here.
This file is tracked (public), so it holds only the framework.
The measured snapshot of where you actually stand lives in `goals.local.md`, which is gitignored.

## The loop

Each session runs the same cycle:

1. **Orient.** Read this file and `goals.local.md`. Pick the one intermediate goal you are furthest from, or the one you decided to work next.
2. **Compose.** Build a throwaway marimo notebook (or a one-off script) that measures the gap and moves the goal. Reuse the catalog; do not reinvent the SQL (see `AGENTS.md` for the domain invariants).
3. **Act.** Make the actual change in YNAB through the catalog (`nb04.apply_edits`, dry-run first), or record the decision.
4. **Retro.** Answer the three retro questions below. Update `goals.local.md`. Decide what, if anything, graduates.

The point is two ledgers per session: a financial outcome, and maybe a reusable artifact. Most sessions move the first and graduate nothing on the second. That is correct.

## North star

> Being in control of my finances.

The north star is deliberately fuzzy. The intermediate goals below are how you make it measurable. Rephrase the north star in `goals.local.md` in your own words if "in control" is not it.

## Intermediate goals

These are YNAB's four rules plus a hygiene goal, each pinned to a signal you can measure from the cache or one live API call. Targets and current values go in `goals.local.md`.

| # | Goal | Signal | How to measure | Healthy target |
|---|---|---|---|---|
| 1 | Give every dollar a job | `to_be_budgeted` for the current month | `nb01.get(f"/budgets/{active_budget_id()}/months/current")["to_be_budgeted"]` (milliunits) | `0` |
| 2 | Fund true expenses | count of goal categories still underfunded | same month payload: categories where `goal_under_funded > 0` | `0` |
| 3 | Roll with the punches | count of overspent categories | same month payload: non-hidden categories where `balance < 0` | `0` |
| 4 | Age your money | `age_of_money` (days) | same month payload: `age_of_money` | `>= 30` |
| 5 | No categorization backlog | rows awaiting review | `len(nb03.pending())` | trending to `0` |

Goal 5 already has a catalog engine (`nb03`/`nb04`). Goals 1-4 read the current-month payload, which the cache does not store - one live `nb01.get(...)` call fetches it. If you measure rules 1-4 every session, that call is a candidate to graduate into a small `nb06_budget_health.py`.

## Retro convention

At session end, answer these three, in order, and write the answers into `goals.local.md`:

1. **Did we move an intermediate goal?** Record the before/after signal value for the goal you worked.
2. **Did a question recur often enough to deserve a durable artifact?** If yes, what kind (see rubric)? If no, say so explicitly - graduating nothing is the default.
3. **What is next session's goal?** Name it so the next Orient step is trivial.

The global `/retro` skill can drive this; the three questions are the contract regardless of how you run it.

## Graduation rubric (what earns a durable artifact)

A throwaway composition graduates only when one of these holds. When none do, it stays throwaway and you note the question in `goals.local.md`.

- **Recurrence.** The same question showed up in two or more sessions. Then add a helper to an existing notebook, or a new `nbNN`, whichever is smaller.
- **A safe write path.** The action mutates YNAB and you will do it again. It belongs behind a dry-run-by-default helper like `nb04.apply_edits`, never an ad-hoc `patch`.
- **A measurement you check every loop.** Like rules 1-4 above - a recurring read deserves one tested function, not a re-typed query.

Bias against graduating. A catalog of twelve notebooks you never reopen is the failure mode, not the goal. Boring, few, reused.
