"""Deterministic Studio writer gate from 5m state. No network. No LLM."""
from typing import Any

ENTRY_5M = 0.0008  # 8 bps on last 5m close-to-close
ENTRY_1H = 0.0025  # 25 bps over ~12×5m
RANGE_HIGH = 0.62
RANGE_LOW = 0.38
REDUCE_UPNL = -0.012


def apply_local(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return the same writer payload shape as gate_map.apply_gate."""
    state = state or {}
    ret5 = float(state.get("ret_5m") or 0.0)
    ret1h = float(state.get("ret_1h") or 0.0)
    rng = float(state.get("range_pos") or 0.5)
    pos = state.get("position") or {}
    side = str(pos.get("side") or "flat").lower()
    upnl = float(pos.get("upnl_pct") or 0.0)

    vetoes: list[str] = []
    if side in {"long", "buy", "short", "sell"} and upnl <= REDUCE_UPNL:
        return _payload("REDUCE", "REDUCE", abs(upnl) * 100, vetoes + ["local_upnl_reduce"])

    extra = state.get("extra") or {}
    imb = float(extra.get("book_imb") or 0.0)
    taker = float(extra.get("taker_buy_ratio") or 0.5)
    ls = float(extra.get("ls_ratio") or 1.0)
    liq_long = float(extra.get("liq_long") or 0.0)
    liq_short = float(extra.get("liq_short") or 0.0)
    large = extra.get("large") if isinstance(extra.get("large"), dict) else None

    lean = 0
    if ret5 >= ENTRY_5M:
        lean += 1
    elif ret5 <= -ENTRY_5M:
        lean -= 1
    if ret1h >= ENTRY_1H:
        lean += 1
    elif ret1h <= -ENTRY_1H:
        lean -= 1
    if rng >= RANGE_HIGH:
        lean += 1
    elif rng <= RANGE_LOW:
        lean -= 1
    if imb >= 0.12:
        lean += 1
    elif imb <= -0.12:
        lean -= 1
    if taker >= 0.58:
        lean += 1
    elif taker <= 0.42:
        lean -= 1
    if ls >= 1.2:
        lean += 1
    elif ls <= 0.85:
        lean -= 1
    if liq_short > liq_long * 1.4 and liq_short > 0:
        lean += 1
    elif liq_long > liq_short * 1.4 and liq_long > 0:
        lean -= 1
    if large:
        if str(large.get("side") or "") in {"buy", "bid"}:
            lean += 1
        elif str(large.get("side") or "") in {"sell", "ask"}:
            lean -= 1

    if lean >= 1:
        raw = "BUY"
    elif lean <= -1:
        raw = "SELL"
    else:
        raw = "HOLD"
        vetoes.append("local_no_lean")

    final = raw
    if raw == "BUY" and side in {"long", "buy"}:
        final = "HOLD"
        vetoes.append("local_already_long")
    elif raw == "SELL" and side in {"short", "sell"}:
        final = "HOLD"
        vetoes.append("local_already_short")

    score = min(3.0, max(0.0, abs(lean) * 1.0))
    return _payload(final, raw, score, vetoes)


def _payload(final: str, raw: str, score: float, vetoes: list[str]) -> dict[str, Any]:
    return {
        "action": final,
        "raw_action": raw,
        "cage_stress_noul": 0.0,
        "same_side_reentry_noul": 1.0,
        "change_confidence_score": score,
        "vetoes": vetoes,
        "follow_trade": final in {"BUY", "SELL", "REDUCE"},
        "decision_path": "agenthub_5m",
    }
