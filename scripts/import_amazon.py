#!/usr/bin/env python3
"""Build a normalized SQLite database from Amazon JSON snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from time import strptime
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;

CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE source_files (
    source_file_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('orders', 'payments')),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    import_order INTEGER NOT NULL CHECK (import_order >= 0),
    UNIQUE (kind, path)
) STRICT;

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    order_date TEXT,
    order_total_cents INTEGER,
    order_total_text TEXT,
    statuses_json TEXT NOT NULL,
    order_details_url TEXT,
    invoice_url TEXT,
    raw_json TEXT NOT NULL,
    canonical_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id)
) STRICT;

CREATE TABLE order_items (
    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    title TEXT NOT NULL,
    asin TEXT,
    product_url TEXT,
    PRIMARY KEY (order_id, item_index)
) STRICT;

CREATE TABLE order_sources (
    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id),
    PRIMARY KEY (order_id, source_file_id)
) STRICT;

CREATE TABLE payment_transactions (
    transaction_id TEXT PRIMARY KEY,
    transaction_date TEXT,
    amount_cents INTEGER,
    amount_text TEXT,
    status TEXT,
    kind TEXT,
    payment_instrument TEXT,
    merchant TEXT,
    order_id TEXT,
    order_label TEXT,
    order_url TEXT,
    raw_json TEXT NOT NULL,
    canonical_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id)
) STRICT;

CREATE TABLE payment_sources (
    transaction_id TEXT NOT NULL REFERENCES payment_transactions(transaction_id) ON DELETE CASCADE,
    source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id),
    PRIMARY KEY (transaction_id, source_file_id)
) STRICT;

CREATE INDEX payment_transactions_date_idx ON payment_transactions(transaction_date);
CREATE INDEX payment_transactions_order_idx ON payment_transactions(order_id);
CREATE INDEX payment_transactions_instrument_idx ON payment_transactions(payment_instrument);
CREATE INDEX order_items_asin_idx ON order_items(asin);

CREATE VIEW current_payment_transactions AS
SELECT p.*
FROM payment_transactions AS p
WHERE p.status <> 'Pending'
   OR p.order_id IS NULL
   OR NOT EXISTS (
       SELECT 1
       FROM payment_transactions AS completed
       WHERE completed.status = 'Completed'
         AND completed.order_id = p.order_id
         AND completed.amount_cents IS p.amount_cents
         AND completed.payment_instrument IS p.payment_instrument
         AND completed.merchant IS p.merchant
         AND completed.kind IS p.kind
         AND ABS(JULIANDAY(completed.transaction_date) - JULIANDAY(p.transaction_date)) <= 14
   );

CREATE VIEW transaction_details AS
SELECT
    p.transaction_id,
    p.transaction_date,
    p.amount_cents,
    p.amount_text,
    p.status,
    p.kind,
    p.payment_instrument,
    p.merchant,
    p.order_id,
    o.order_date,
    o.order_total_cents,
    COALESCE(i.item_count, 0) AS item_count,
    i.item_titles,
    i.asins,
    p.order_url,
    o.order_details_url,
    o.invoice_url
FROM current_payment_transactions AS p
LEFT JOIN orders AS o USING (order_id)
LEFT JOIN (
    SELECT
        order_id,
        COUNT(*) AS item_count,
        GROUP_CONCAT(title, ' | ') AS item_titles,
        GROUP_CONCAT(asin, ' | ') FILTER (WHERE asin IS NOT NULL) AS asins
    FROM order_items
    GROUP BY order_id
) AS i USING (order_id);

CREATE VIEW store_card_transaction_details AS
SELECT *
FROM transaction_details
WHERE payment_instrument LIKE '%Store Card%';
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("data/external/raw/amazon"),
        help="Directory containing amazon_payment_transactions_*.json and amazon_orders_*.json snapshots.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/amazon.sqlite3"),
        help="SQLite file to replace atomically after a successful build.",
    )
    return parser.parse_args()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    try:
        parsed = strptime(text, "%B %d, %Y")
        return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday).isoformat()
    except ValueError as error:
        raise ValueError(f"unsupported date: {text!r}") from error


def money_cents(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).replace("$", "").replace(",", "").strip()
    try:
        return int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation as error:
        raise ValueError(f"unsupported money value: {value!r}") from error


def load_snapshot(path: Path) -> tuple[list[dict[str, Any]], bytes]:
    payload = path.read_bytes()
    rows = json.loads(payload)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path} must contain a JSON array of objects")
    return rows, payload


def discover(source: Path) -> list[tuple[str, Path]]:
    files = [("orders", path) for path in source.rglob("amazon_orders_*.json")]
    files.extend(("payments", path) for path in source.rglob("amazon_payment_transactions_*.json"))
    return sorted(files, key=lambda item: (item[1].relative_to(source).as_posix(), item[0]))


def source_file_id(kind: str, relative_path: str, digest: str) -> str:
    return sha256_bytes(f"{kind}\0{relative_path}\0{digest}".encode())


def payment_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "transaction_date": parse_date(row.get("transaction_date")),
        "amount_cents": money_cents(row.get("amount", row.get("amount_text"))),
        "status": row.get("status"),
        "kind": row.get("kind"),
        "payment_instrument": row.get("payment_instrument"),
        "merchant": row.get("merchant"),
        "order_id": row.get("order_id"),
        "order_label": row.get("order_label"),
    }


def import_orders(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    file_id: str,
) -> None:
    for row in rows:
        order_id = row.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("every order must have a nonempty order_id")
        statuses = row.get("statuses") or []
        if not isinstance(statuses, list):
            raise TypeError(f"order {order_id} has a non-list statuses field")
        raw_json = canonical_json(row)
        connection.execute(
            """
            INSERT INTO orders (
                order_id, order_date, order_total_cents, order_total_text, statuses_json,
                order_details_url, invoice_url, raw_json, canonical_source_file_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (order_id) DO UPDATE SET
                order_date = excluded.order_date,
                order_total_cents = excluded.order_total_cents,
                order_total_text = excluded.order_total_text,
                statuses_json = excluded.statuses_json,
                order_details_url = excluded.order_details_url,
                invoice_url = excluded.invoice_url,
                raw_json = excluded.raw_json,
                canonical_source_file_id = excluded.canonical_source_file_id
            """,
            (
                order_id,
                parse_date(row.get("order_date")),
                money_cents(row.get("order_total", row.get("order_total_text"))),
                row.get("order_total_text"),
                canonical_json(statuses),
                row.get("order_details_url"),
                row.get("invoice_url"),
                raw_json,
                file_id,
            ),
        )
        connection.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        items = row.get("items") or []
        if not isinstance(items, list):
            raise TypeError(f"order {order_id} has a non-list items field")
        for item_index, item in enumerate(items):
            title = item.get("title") if isinstance(item, dict) else None
            if not isinstance(title, str) or not title:
                raise ValueError(f"order {order_id} item {item_index} has no title")
            connection.execute(
                """
                INSERT INTO order_items (order_id, item_index, title, asin, product_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, item_index, title, item.get("asin"), item.get("product_url")),
            )
        connection.execute(
            "INSERT OR IGNORE INTO order_sources (order_id, source_file_id) VALUES (?, ?)",
            (order_id, file_id),
        )


def import_payments(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    file_id: str,
) -> None:
    occurrences: defaultdict[tuple[Any, str], int] = defaultdict(int)
    for row in rows:
        identity = payment_identity(row)
        identity_json = canonical_json(identity)
        source_page = row.get("source_page_index", "whole-file")
        occurrence_key = (source_page, identity_json)
        occurrence = occurrences[occurrence_key]
        occurrences[occurrence_key] += 1
        transaction_id = sha256_bytes(f"{identity_json}\0{occurrence}".encode())
        raw_json = canonical_json(row)
        connection.execute(
            """
            INSERT INTO payment_transactions (
                transaction_id, transaction_date, amount_cents, amount_text, status, kind,
                payment_instrument, merchant, order_id, order_label, order_url, raw_json,
                canonical_source_file_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (transaction_id) DO UPDATE SET
                amount_text = excluded.amount_text,
                order_url = excluded.order_url,
                raw_json = excluded.raw_json,
                canonical_source_file_id = excluded.canonical_source_file_id
            """,
            (
                transaction_id,
                identity["transaction_date"],
                identity["amount_cents"],
                row.get("amount_text"),
                identity["status"],
                identity["kind"],
                identity["payment_instrument"],
                identity["merchant"],
                identity["order_id"],
                identity["order_label"],
                row.get("order_url"),
                raw_json,
                file_id,
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO payment_sources (transaction_id, source_file_id) VALUES (?, ?)",
            (transaction_id, file_id),
        )


def build_database(source: Path, database: Path) -> dict[str, int]:
    source = source.resolve()
    database = database.resolve()
    files = discover(source)
    if not files:
        raise FileNotFoundError(f"no Amazon snapshots found under {source}")

    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{database.name}.", suffix=".tmp", dir=database.parent)
    os.close(descriptor)
    temporary_path = Path(temporary_name)

    try:
        connection = sqlite3.connect(temporary_path)
        try:
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            for import_order, (kind, path) in enumerate(files):
                rows, payload = load_snapshot(path)
                relative_path = path.relative_to(source).as_posix()
                digest = sha256_bytes(payload)
                file_id = source_file_id(kind, relative_path, digest)
                connection.execute(
                    """
                    INSERT INTO source_files (source_file_id, kind, path, sha256, row_count, import_order)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (file_id, kind, relative_path, digest, len(rows), import_order),
                )
                if kind == "orders":
                    import_orders(connection, rows, file_id)
                else:
                    import_payments(connection, rows, file_id)
            connection.commit()
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError(f"foreign key check failed: {foreign_key_errors[:5]}")
            summary = {
                "source_files": connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
                "orders": connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                "order_items": connection.execute("SELECT COUNT(*) FROM order_items").fetchone()[0],
                "payments": connection.execute("SELECT COUNT(*) FROM payment_transactions").fetchone()[0],
                "current_payments": connection.execute("SELECT COUNT(*) FROM current_payment_transactions").fetchone()[
                    0
                ],
                "payments_with_items": connection.execute(
                    "SELECT COUNT(*) FROM transaction_details WHERE item_count > 0"
                ).fetchone()[0],
            }
            connection.execute("VACUUM")
        finally:
            connection.close()
        os.replace(temporary_path, database)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return summary


def main() -> None:
    args = parse_args()
    summary = build_database(args.source, args.db)
    print(f"database={args.db.resolve()}")
    for key, value in summary.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
