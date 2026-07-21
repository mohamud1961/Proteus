from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aether_next.receipts import ReceiptStore


def test_sqlite_receipts_survive_reopen(tmp_path: Path):
    path = tmp_path / "receipts.sqlite"
    store = ReceiptStore(path)
    receipt = store.append("action_result", {"stdout": "alpha", "exit_code": 0})
    store.close()

    reopened = ReceiptStore(path)
    assert reopened.get(receipt.receipt_id).payload["stdout"] == "alpha"
    assert reopened.query(kind="action_result")[0].sha256 == receipt.sha256
    reopened.close()


def test_deduplicated_receipts_reuse_exact_id():
    store = ReceiptStore()
    first = store.append_deduplicated("payload", {"value": "same"})
    second = store.append_deduplicated("payload", {"value": "same"})
    assert first.receipt_id == second.receipt_id
    assert len(store) == 1


def test_concurrent_receipt_appends_are_unique_and_complete():
    store = ReceiptStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda i: store.append("event", {"i": i}), range(200)))
    assert len({item.receipt_id for item in receipts}) == 200
    assert len(store) == 200
