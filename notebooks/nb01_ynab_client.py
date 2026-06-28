# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "marimo",
#     "requests",
# ]
# ///

import marimo

__generated_with = "0.23.3"
app = marimo.App(width="medium")

with app.setup:
    import marimo as mo
    import os
    import subprocess
    import requests

    YNAB_BASE = "https://api.ynab.com/v1"


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # nb01 - YNAB API client

    Thin wrapper around the [YNAB REST API](https://api.ynab.com/) for use by
    the rest of the catalog. Handles three things and nothing more:

    1. **Auth** - reads the Personal Access Token from `YNAB_TOKEN` env var,
       or from 1Password via the `op` CLI when `YNAB_OP_REF` is set. Cached
       at module level so repeated calls don't re-prompt biometrics.
    2. **HTTP** - `get(path, **params)` and `patch(path, body)` return the
       parsed `.data` payload. Path is everything after `/v1`.
    3. **Budgets** - `list_budgets()` and `active_budget_id()`. The "active"
       budget is the most-recently-modified one.

    Other notebooks (e.g. `nb02_ynab_sync`) import these helpers via
    `from nb01_ynab_client import get, active_budget_id`. The `notebooks/`
    directory is added to `sys.path` in their setup cell.

    Constant: `YNAB_BASE` (API base URL).
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Auth
    """)
    return


@app.function(hide_code=True)
def get_token() -> str:
    """Read the YNAB Personal Access Token.

    Resolution order:
      1. `YNAB_TOKEN` env var (simplest; works in CI and one-off scripts).
      2. `op read $YNAB_OP_REF` if `YNAB_OP_REF` is set (e.g. `op://Vault/Item/Field`).

    Cached for the lifetime of the kernel: 1Password's biometric prompt
    times out between calls, so we read once and reuse.
    """
    if not hasattr(get_token, "_cache"):
        token = os.environ.get("YNAB_TOKEN")
        if not token:
            op_ref = os.environ.get("YNAB_OP_REF")
            if op_ref:
                res = subprocess.run(
                    ["op", "read", op_ref],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                token = res.stdout.strip()
        if not token:
            raise RuntimeError(
                "No YNAB token. Set YNAB_TOKEN, or set YNAB_OP_REF (e.g. op://Vault/Item/Field) to read from 1Password."
            )
        get_token._cache = token
    return get_token._cache


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## HTTP wrappers
    """)
    return


@app.function(hide_code=True)
def get(path: str, **params) -> dict:
    """GET https://api.ynab.com/v1/{path} -> parsed .data payload.

    Leading slash on `path` is optional.
    """
    url = f"{YNAB_BASE.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {get_token()}"}
    r = requests.get(url, headers=headers, params=params or None, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


@app.function(hide_code=True)
def patch(path: str, body: dict) -> dict:
    """PATCH the given path with a JSON body. Returns .data payload.

    Leading slash on `path` is optional.
    """
    url = f"{YNAB_BASE.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {get_token()}"}
    r = requests.patch(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Budget discovery
    """)
    return


@app.function(hide_code=True)
def list_budgets() -> list[dict]:
    """Return all budgets visible to the token, newest-first by last_modified_on."""
    budgets = get("/budgets")["budgets"]
    return sorted(budgets, key=lambda b: b["last_modified_on"] or "", reverse=True)


@app.function(hide_code=True)
def active_budget_id() -> str:
    """The most-recently-modified budget id. The 'active' one in practice."""
    return list_budgets()[0]["id"]


@app.cell(hide_code=True)
def _():
    mo.md("""
    ## Demo: list budgets
    """)
    return


@app.cell
def _():
    import datetime as _dt

    _budgets = list_budgets()
    mo.md(
        "### Budgets\n\n"
        + "\n".join(
            f"- **{b['name']}** | id `{b['id']}` | last modified `{b['last_modified_on']}`"
            + (" - **(active)**" if i == 0 else "")
            for i, b in enumerate(_budgets)
        )
    )
    return


if __name__ == "__main__":
    app.run()
