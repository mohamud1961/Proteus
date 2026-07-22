"""Durable, exact receipt and output-handle storage.

This store is intentionally separate from :class:`ExecutionLedger`: the
ledger remains the runtime event projection while this class is the lossless
authoritative payload store used by context/output handles.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterable, Mapping


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


@dataclass(frozen=True)
class StoredReceipt:
    receipt_id: str
    kind: str
    payload: Mapping[str, Any]
    sha256: str


class ReceiptStore:
    """Immutable exact receipt store with optional SQLite persistence."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = RLock()
        self._receipts: dict[str, StoredReceipt] = {}
        self._dedup: dict[str, str] = {}
        self._counter = 0
        self._connection: sqlite3.Connection | None = None
        if path is not None:
            db_path = Path(path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(db_path, check_same_thread=False)
            # Independent ReceiptStore instances may append to the same file.
            # Make SQLite wait for the writer owning the serialized append
            # transaction instead of failing spuriously with "database is
            # locked" under normal concurrent use.
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS aether_receipts ("
                "ordinal INTEGER PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, "
                "payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, dedup_key TEXT)"
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS aether_receipts_dedup "
                "ON aether_receipts(dedup_key) WHERE dedup_key IS NOT NULL"
            )
            self._connection.commit()
            self._load()

    def _load(self) -> None:
        assert self._connection is not None
        rows = self._connection.execute(
            "SELECT ordinal, receipt_id, kind, payload_json, payload_sha256, dedup_key "
            "FROM aether_receipts ORDER BY ordinal"
        ).fetchall()
        for ordinal, receipt_id, kind, payload_json, payload_sha256, dedup_key in rows:
            payload = json.loads(payload_json)
            receipt = StoredReceipt(str(receipt_id), str(kind), payload, str(payload_sha256))
            self._receipts[receipt.receipt_id] = receipt
            if dedup_key:
                self._dedup[str(dedup_key)] = receipt.receipt_id
            self._counter = max(self._counter, int(ordinal))

    @staticmethod
    def _copy(receipt: StoredReceipt) -> StoredReceipt:
        return StoredReceipt(receipt.receipt_id, receipt.kind, deepcopy(dict(receipt.payload)), receipt.sha256)

    def _append(self, kind: str, payload: Mapping[str, Any], *, dedup_key: str | None) -> StoredReceipt:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("receipt kind must be a non-empty string")
        if not isinstance(payload, Mapping):
            raise TypeError("receipt payload must be a mapping")
        serialised = _canonical(payload)
        with self._lock:
            # The ordinal is part of the stable receipt id, so allocating it
            # from a process-local counter is unsafe when two store instances
            # share a SQLite file.  IMMEDIATE serializes allocation and the
            # reload reconciles each instance with rows committed by peers.
            if self._connection is not None:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    self._load()
                    if dedup_key is not None and dedup_key in self._dedup:
                        existing = self._copy(self._receipts[self._dedup[dedup_key]])
                        self._connection.commit()
                        return existing
                    self._counter += 1
                    receipt_id = f"receipt:{self._counter:06d}"
                    stored = deepcopy(dict(payload))
                    digest = sha256(serialised.encode("utf-8")).hexdigest()
                    receipt = StoredReceipt(receipt_id, kind, stored, digest)
                    self._connection.execute(
                        "INSERT INTO aether_receipts(ordinal, receipt_id, kind, payload_json, payload_sha256, dedup_key) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (self._counter, receipt_id, kind, serialised, digest, dedup_key),
                    )
                    self._connection.commit()
                except Exception:
                    self._connection.rollback()
                    raise
            else:
                if dedup_key is not None and dedup_key in self._dedup:
                    return self.get(self._dedup[dedup_key])
                self._counter += 1
                receipt_id = f"receipt:{self._counter:06d}"
                stored = deepcopy(dict(payload))
                digest = sha256(serialised.encode("utf-8")).hexdigest()
                receipt = StoredReceipt(receipt_id, kind, stored, digest)
            self._receipts[receipt_id] = receipt
            if dedup_key is not None:
                self._dedup[dedup_key] = receipt_id
            return self._copy(receipt)

    def append(self, kind: str, payload: Mapping[str, Any]) -> StoredReceipt:
        return self._append(kind, payload, dedup_key=None)

    def append_deduplicated(self, kind: str, payload: Mapping[str, Any]) -> StoredReceipt:
        dedup_key = sha256(f"{kind}\x1f{_canonical(payload)}".encode("utf-8")).hexdigest()
        return self._append(kind, payload, dedup_key=dedup_key)

    def get(self, receipt_id: str) -> StoredReceipt:
        with self._lock:
            if receipt_id not in self._receipts and self._connection is not None:
                self._load()
            try:
                return self._copy(self._receipts[receipt_id])
            except KeyError as exc:
                raise KeyError(f"unknown receipt: {receipt_id}") from exc

    def query(self, *, kind: str | None = None, text: str | None = None) -> tuple[StoredReceipt, ...]:
        with self._lock:
            if self._connection is not None:
                self._load()
            values: Iterable[StoredReceipt] = tuple(self._receipts.values())
            if kind is not None:
                values = (item for item in values if item.kind == kind)
            if text is not None:
                needle = text.lower()
                values = (item for item in values if needle in _canonical(item.payload).lower())
            return tuple(self._copy(item) for item in values)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __len__(self) -> int:
        with self._lock:
            if self._connection is not None:
                self._load()
            return len(self._receipts)


class OutputHandleStore:
    """Lossless content store. Handles are content-deduplicated and exact."""

    def __init__(self, receipts: ReceiptStore | None = None) -> None:
        # ``ReceiptStore`` implements ``__len__`` for diagnostics, so an
        # empty-but-valid store is false-y.  Preserve the explicitly supplied
        # store by testing identity with ``None`` rather than truthiness;
        # otherwise output handles silently diverge into a private receipt
        # stream before the first ordinary receipt is written.
        self.receipts = receipts if receipts is not None else ReceiptStore()

    def put(self, content: str | bytes, *, kind: str = "output") -> str:
        if not isinstance(content, (str, bytes)):
            raise TypeError("output content must be str or bytes")
        is_bytes = isinstance(content, bytes)
        encoded = content.decode("utf-8", errors="surrogateescape") if is_bytes else content
        receipt = self.receipts.append_deduplicated(
            "output_payload",
            {
                "kind": kind,
                "content": encoded,
                "content_type": "bytes" if is_bytes else "str",
                "bytes": len(content.encode("utf-8", "surrogateescape") if isinstance(content, str) else content),
            },
        )
        return f"output:{receipt.receipt_id}"

    def get(self, handle: str) -> str | bytes:
        if not handle.startswith("output:"):
            raise KeyError(f"invalid output handle: {handle}")
        receipt = self.receipts.get(handle.removeprefix("output:"))
        content = str(receipt.payload["content"])
        if receipt.payload.get("content_type") == "bytes":
            return content.encode("utf-8", errors="surrogateescape")
        return content

    def describe(self, handle: str) -> dict[str, Any]:
        receipt = self.receipts.get(handle.removeprefix("output:"))
        content = receipt.payload["content"]
        return {"handle": handle, "chars": len(str(content)), "bytes": receipt.payload.get("bytes", 0)}
