from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


class EvidenceLedger:
    """Append-only JSONL evidence; the verifier reads workspace state itself."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def record(self, event: str, payload: dict[str, Any]) -> None:
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
