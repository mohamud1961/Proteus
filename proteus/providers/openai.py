from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen


class OpenAIProvider:
    """Optional live provider boundary; no network call is made by replay mode."""

    def __init__(self, model: str = "gpt-5.6") -> None:
        self.model = model

    def availability(self) -> dict[str, str | bool]:
        return {"available": bool(os.environ.get("OPENAI_API_KEY")), "model": self.model, "mode": "live"}

    def require_credentials(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("live mode requires OPENAI_API_KEY; use replay mode for the offline demo")

    def complete(self, prompt: str) -> dict[str, object]:
        """Make one real Responses API call when the caller explicitly opts in."""
        self.require_credentials()
        if not prompt.strip():
            raise ValueError("prompt must be non-empty")
        body = json.dumps({"model": self.model, "input": prompt}).encode("utf-8")
        request = Request(
            "https://api.openai.com/v1/responses",
            data=body,
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
