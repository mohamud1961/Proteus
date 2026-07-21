import pytest

from aether_next.receipts import ReceiptStore


def test_receipts_are_immutable_and_exactly_retrievable():
    store = ReceiptStore()
    payload = {"stdout": "x" * 10000, "exit_code": 0}
    receipt = store.append("command_result", payload)
    assert store.get(receipt.receipt_id).payload == payload
    assert receipt.sha256


def test_receipt_query_by_kind_and_text():
    store = ReceiptStore()
    store.append("command_result", {"stdout": "alpha"})
    store.append("verifier_outcome", {"summary": "beta"})
    assert len(store.query(kind="command_result")) == 1
    assert store.query(text="beta")[0].kind == "verifier_outcome"


def test_unknown_receipt_has_explicit_error():
    store = ReceiptStore()
    with pytest.raises(KeyError, match="unknown receipt"):
        store.get("receipt:999999")


def test_external_payload_mutation_cannot_change_stored_receipt():
    store = ReceiptStore()
    payload = {"nested": {"value": 1}}
    receipt = store.append("state", payload)
    payload["nested"]["value"] = 99
    receipt.payload["nested"]["value"] = 42
    assert store.get(receipt.receipt_id).payload["nested"]["value"] == 1
