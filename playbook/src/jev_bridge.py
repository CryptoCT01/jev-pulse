"""Optional typed answers. Studio Paper must not depend on LLM or OpenRouter.

injected_answers is for tests. Otherwise return unavailable and let main.py
use local_gate on 5m features.
"""
from typing import Any


def fetch_answers(state: dict[str, Any]) -> dict[str, Any]:
    """Return {status, answers, detail}. Never raises. Never calls a model."""
    injected = (state or {}).get("injected_answers") if isinstance(state, dict) else None
    if isinstance(injected, dict) and injected:
        return {"status": "ok", "answers": injected, "detail": "injected"}
    return {
        "status": "unavailable",
        "answers": {},
        "detail": "optional_jev_skipped — local_5m gate",
    }
