"""Map Jev answers → writer intent. No network. No prose thesis."""

from typing import Any

CAGE_STRESS_THRESHOLD = 0.85
REENTRY_THRESHOLD = 0.35
SCORE_THRESHOLD = 0.35


def _noul(answer: Any) -> float:
    if not isinstance(answer, dict):
        return 0.0
    try:
        return float(answer.get("noul", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _choice(answer: Any) -> str:
    if not isinstance(answer, dict):
        return "HOLD"
    raw = str(answer.get("choice") or "HOLD").upper()
    if raw not in {"HOLD", "BUY", "SELL", "REDUCE"}:
        return "HOLD"
    return raw


def _score(answer: Any) -> float:
    if not isinstance(answer, dict):
        return 0.0
    try:
        return float(answer.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def apply_gate(answers: dict[str, Any] | None) -> dict[str, Any]:
    """Return writer_action payload from Jev answers (or empty → HOLD)."""
    answers = answers or {}
    action = _choice(answers.get("action"))
    cage = _noul(answers.get("cage_stress"))
    reentry = _noul(answers.get("same_side_reentry"))
    conf = _score(answers.get("change_confidence"))

    vetoes: list[str] = []
    final = action

    if cage >= CAGE_STRESS_THRESHOLD:
        final = "REDUCE" if action in {"BUY", "SELL", "REDUCE"} else "HOLD"
        if action in {"BUY", "SELL"}:
            final = "REDUCE"
        vetoes.append("cage_stress")

    if final in {"BUY", "SELL"} and conf < SCORE_THRESHOLD:
        final = "HOLD"
        vetoes.append("low_change_confidence")

    if final in {"BUY", "SELL"} and reentry < REENTRY_THRESHOLD:
        vetoes.append("same_side_reentry_soft")

    return {
        "action": final,
        "raw_action": action,
        "cage_stress_noul": cage,
        "same_side_reentry_noul": reentry,
        "change_confidence_score": conf,
        "vetoes": vetoes,
        "follow_trade": final in {"BUY", "SELL", "REDUCE"},
    }
