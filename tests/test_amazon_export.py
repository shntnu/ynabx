"""Static checks for the authenticated Amazon browser exporter."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORTER = ROOT / "scripts" / "export_amazon.mjs"


def test_amazon_exporter_is_valid_and_exports_public_entry_points() -> None:
    subprocess.run(["node", "--check", str(EXPORTER)], check=True, capture_output=True, text=True)
    program = f"""
        const module = await import({EXPORTER.as_uri()!r});
        const names = [
          'scrapePayments', 'scrapeOrders', 'scrapeAmazon',
          'writeAmazonSnapshot', 'writeAmazonDataSnapshot'
        ];
        if (!names.every((name) => typeof module[name] === 'function')) process.exit(1);
        if (module.makeSnapshotId(new Date('2026-08-23T12:34:56.000Z')) !== '20260823T123456Z') process.exit(2);
    """
    subprocess.run(
        ["node", "--input-type=module", "--eval", program],
        check=True,
        capture_output=True,
        text=True,
    )


def test_amazon_snapshot_writer_publishes_complete_directory(tmp_path: Path) -> None:
    program = f"""
        const module = await import({EXPORTER.as_uri()!r});
        await module.writeAmazonDataSnapshot({{
          payments: [{{ transaction_date: 'January 2, 2026', amount: -1.23 }}],
          orders: [{{ order_id: '111-1111111-1111111', items: [] }}],
        }}, {{
          outputRoot: {str(tmp_path)!r},
          snapshotId: '20260102T030405Z',
        }});
    """
    subprocess.run(
        ["node", "--input-type=module", "--eval", program],
        check=True,
        capture_output=True,
        text=True,
    )

    snapshot = tmp_path / "20260102T030405Z"
    assert (snapshot / "amazon_payment_transactions_20260102T030405Z.json").is_file()
    assert (snapshot / "amazon_orders_20260102T030405Z.json").is_file()
    assert (snapshot / "manifest.json").is_file()
    assert not list(tmp_path.glob(".building-*"))
