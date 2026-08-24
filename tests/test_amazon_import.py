"""Tests for the deterministic Amazon JSON to SQLite importer."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORTER = ROOT / "scripts" / "import_amazon.py"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def payment(
    amount: float,
    *,
    kind: str = "charge",
    status: str = "Completed",
    transaction_date: str = "January 2, 2026",
) -> dict:
    return {
        "transaction_date": transaction_date,
        "payment_instrument": "Prime Store Card ****0000",
        "amount_text": f"${amount:+.2f}".replace("$-", "-$").replace("$+", "+$"),
        "amount": amount,
        "status": status,
        "kind": kind,
        "order_id": "111-1111111-1111111",
        "order_label": "Order #111-1111111-1111111",
        "merchant": "Example Merchant",
        "order_url": "https://example.test/order/111-1111111-1111111",
    }


def order(title: str) -> dict:
    return {
        "order_id": "111-1111111-1111111",
        "order_date": "January 1, 2026",
        "order_total_text": "$12.34",
        "order_total": 12.34,
        "statuses": ["Delivered"],
        "items": [{"title": title, "asin": "B000000000", "product_url": "https://example.test/item"}],
        "order_details_url": "https://example.test/details",
        "invoice_url": "https://example.test/invoice",
    }


def run_import(source: Path, database: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(IMPORTER), "--source", str(source), "--db", str(database)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_rebuild_deduplicates_overlapping_snapshots_and_keeps_provenance(tmp_path: Path) -> None:
    source = tmp_path / "raw"
    source.mkdir()
    database = tmp_path / "amazon.sqlite3"

    pending_charge = payment(-12.34, status="Pending")
    completed_charge = payment(-12.34, transaction_date="January 3, 2026")
    refund = payment(2.34, kind="refund_or_credit")
    completed_charge_page_0 = {**completed_charge, "source_page_index": 0}
    completed_charge_page_1 = {**completed_charge, "source_page_index": 1}
    write_json(source / "amazon_payment_transactions_2026-01-01.json", [pending_charge])
    write_json(
        source / "amazon_payment_transactions_2026-02-01.json",
        [completed_charge_page_0, completed_charge_page_0, completed_charge_page_1, refund],
    )
    write_json(source / "amazon_orders_2026-01-01.json", [order("Synthetic item")])
    write_json(source / "amazon_orders_2026-02-01.json", [order("Synthetic item, revised")])

    first = run_import(source, database)
    assert "payments=4" in first.stdout
    assert "current_payments=3" in first.stdout

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM payment_transactions").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM current_payment_transactions").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM payment_sources").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM order_sources").fetchone()[0] == 2
        detail = connection.execute(
            "SELECT amount_cents, order_total_cents, item_titles FROM transaction_details ORDER BY amount_cents LIMIT 1"
        ).fetchone()
        assert detail == (-1234, 1234, "Synthetic item, revised")
        statuses = connection.execute(
            "SELECT status, transaction_date FROM current_payment_transactions "
            "WHERE amount_cents = -1234 ORDER BY transaction_id"
        ).fetchall()
        assert statuses == [("Completed", "2026-01-03"), ("Completed", "2026-01-03")]

    second = run_import(source, database)
    assert "payments=4" in second.stdout
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_transactions").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM current_payment_transactions").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_import_rejects_an_order_without_an_id(tmp_path: Path) -> None:
    source = tmp_path / "raw"
    source.mkdir()
    database = tmp_path / "amazon.sqlite3"
    write_json(source / "amazon_orders_2026-01-01.json", [{"order_date": "January 1, 2026"}])

    result = subprocess.run(
        [sys.executable, str(IMPORTER), "--source", str(source), "--db", str(database)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "nonempty order_id" in result.stderr
    assert not database.exists()
