# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb", "marimo", "polars", "pytest", "requests"]
# ///
"""Smoke tests that don't hit the YNAB API.

Run with: `uv run --with pytest pytest tests/`

These exercise the boring plumbing - schema creation, env-var overrides,
auth resolution - so a fresh clone can verify the catalog imports cleanly
before pointing it at a real token.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

NOTEBOOK_DIR = Path(__file__).resolve().parent.parent / "notebooks"
sys.path.insert(0, str(NOTEBOOK_DIR))


def test_get_token_reads_env_var(monkeypatch):
    """`YNAB_TOKEN` is the simplest path - no `op` involvement."""
    monkeypatch.setenv("YNAB_TOKEN", "test-token-abc")
    monkeypatch.delenv("YNAB_OP_REF", raising=False)

    import nb01_ynab_client as nb01

    if hasattr(nb01.get_token, "_cache"):
        del nb01.get_token._cache

    assert nb01.get_token() == "test-token-abc"


def test_get_token_raises_with_no_config(monkeypatch):
    """No env vars set = clear error message, not a confusing op failure."""
    monkeypatch.delenv("YNAB_TOKEN", raising=False)
    monkeypatch.delenv("YNAB_OP_REF", raising=False)

    import nb01_ynab_client as nb01

    if hasattr(nb01.get_token, "_cache"):
        del nb01.get_token._cache

    with pytest.raises(RuntimeError, match="YNAB_TOKEN"):
        nb01.get_token()


def test_get_token_uses_op_when_ref_set(monkeypatch):
    """`YNAB_OP_REF` -> `op read $YNAB_OP_REF`. Confirm subprocess is called correctly."""
    monkeypatch.delenv("YNAB_TOKEN", raising=False)
    monkeypatch.setenv("YNAB_OP_REF", "op://Vault/Item/Field")

    import nb01_ynab_client as nb01

    if hasattr(nb01.get_token, "_cache"):
        del nb01.get_token._cache

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="op-token-xyz\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nb01.get_token() == "op-token-xyz"
    assert captured["cmd"] == ["op", "read", "op://Vault/Item/Field"]


def test_db_path_env_override_creates_schema(tmp_path, monkeypatch):
    """`YNAB_DB_PATH` controls the DuckDB location and `connect()` ensures schema."""
    db = tmp_path / "ynab.db"
    monkeypatch.setenv("YNAB_DB_PATH", str(db))

    for mod in ("nb02_ynab_sync", "nb01_ynab_client"):
        sys.modules.pop(mod, None)

    import nb02_ynab_sync as nb02

    assert nb02.DB_PATH == db
    con = nb02.connect()
    try:
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        assert {"transactions", "meta"}.issubset(tables)
    finally:
        con.close()
    assert db.exists()
