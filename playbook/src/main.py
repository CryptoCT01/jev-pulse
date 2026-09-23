"""Jev Pulse Playbook entry — Studio writer loop for BTCUSDT paper.

Studio owns 5m data → local_gate → emit_signal_or_follow. Typed Jev answers
are optional (injected tests only). Sandbox never calls OpenRouter or LLM.
"""

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from getagent import data, runtime

from agenthub_tape import snapshot as agenthub_snapshot
from gate_map import apply_gate
from jev_bridge import fetch_answers
from local_gate import apply_local
from radar_state import build_state, empty_cage, empty_position


def _sanitize(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _closes_from_bars(bars: Any) -> list[Decimal]:
    records = data.to_records(bars)
    out: list[Decimal] = []
    for row in records:
        close = row.get("close")
        if close in (None, ""):
            continue
        out.append(Decimal(str(close)))
    return out


def _ret(closes: list[Decimal], n: int) -> float:
    if len(closes) <= n or closes[-1 - n] == 0:
        return 0.0
    return float(closes[-1] / closes[-1 - n] - 1)


def _vol_short(closes: list[Decimal], window: int = 20) -> float:
    if len(closes) < window:
        return 0.0
    chunk = closes[-window:]
    mean = sum(chunk) / Decimal(window)
    var = sum((c - mean) ** 2 for c in chunk) / Decimal(window)
    mark = float(closes[-1] or 0)
    if mark <= 0:
        return 0.0
    return float(var.sqrt()) / mark


def _range_pos(closes: list[Decimal], window: int = 30) -> float:
    if len(closes) < 2:
        return 0.5
    chunk = closes[-window:]
    hi = max(chunk)
    lo = min(chunk)
    if hi == lo:
        return 0.5
    return float((closes[-1] - lo) / (hi - lo))


def _position_snapshot(symbol: str) -> dict[str, Any]:
    try:
        from getagent import trade

        current = trade.contract.current_position(symbol=symbol)
        position = trade.helpers.find_contract_position(current, symbol=symbol)
        if position is None:
            return empty_position()
        side = str(getattr(position, "hold_side", None) or "flat")
        qty = float(getattr(position, "available", None) or getattr(position, "total", 0) or 0)
        return {
            "side": side,
            "qty": qty,
            "upnl_pct": float(getattr(position, "unrealizedPL", 0) or 0),
            "age_bars": 0,
        }
    except Exception:
        return empty_position()


def _execute_paper(
    *,
    symbol: str,
    action: str,
    margin_budget: str,
    leverage: int,
) -> dict[str, Any]:
    """Follow-trade callback. Studio Paper routes fills; still no live intent here."""
    from getagent import trade

    if action == "REDUCE":
        current = trade.contract.current_position(symbol=symbol)
        position = trade.helpers.find_contract_position(current, symbol=symbol)
        if position is None:
            return {"status": "flat"}
        closed = trade.contract.close_position(symbol=symbol, hold_side=position.hold_side)
        if not trade.is_success(closed):
            raise RuntimeError(f"contract close failed: {closed}")
        return {"status": "reduced", "result": closed}

    hold_side = "long" if action == "BUY" else "short"
    current = trade.contract.current_position(symbol=symbol)
    position = trade.helpers.find_contract_position(current, symbol=symbol)
    if position is not None and position.hold_side == hold_side:
        return {"status": "already_positioned", "hold_side": hold_side}
    if position is not None:
        closed = trade.contract.close_position(symbol=symbol, hold_side=position.hold_side)
        if not trade.is_success(closed):
            raise RuntimeError(f"contract close failed: {closed}")

    qty_plan = trade.helpers.compute_qty(
        symbol=symbol,
        market="contract",
        budget_amount=margin_budget,
        leverage=leverage,
    )
    open_position = (
        trade.contract.open_long_market if action == "BUY" else trade.contract.open_short_market
    )
    result = open_position(symbol=symbol, qty=qty_plan.qty, leverage=leverage)
    if not trade.is_success(result):
        raise RuntimeError(f"contract open failed: {result}")
    return {"qty": str(qty_plan.qty), "result": result}


def _run_live() -> None:
    cfg = runtime.manifest.get("strategy_config", {}) or {}
    symbol = str((cfg.get("trading_symbols") or ["BTCUSDT"])[0])
    leverage = int(cfg.get("leverage", 3) or 3)
    margin_budget = str(cfg.get("margin_budget", "100") or "100")

    # Studio data bridge accepts 5m/15m/1h/4h/1d only — not 1m (HTTP 422).
    bars = data.crypto.futures.kline(
        symbol=symbol,
        interval="5m",
        exchange="bitget",
        limit=120,
        closed_only=True,
    )
    closes = _closes_from_bars(bars)
    if len(closes) < 30:
        runtime.emit_signal(
            action="hold",
            symbol=symbol,
            confidence=0.0,
            metrics={"rows": len(closes)},
            meta={"reason": "insufficient closed bars", "jev_status": "skipped"},
        )
        return

    mark = float(closes[-1])
    funding = 0.0
    try:
        fr = data.crypto.futures.funding_rate(symbol=symbol)
        if isinstance(fr, dict):
            funding = float(fr.get("fundingRate") or fr.get("rate") or 0)
    except Exception:
        funding = 0.0

    extra = {"evaluation_mode": str(runtime.evaluation_mode)}
    extra.update(agenthub_snapshot(symbol, mark))
    state = build_state(
        symbol=symbol,
        ts_ms=_now_ms(),
        mark=mark,
        funding_rate=funding,
        ret_1m=_ret(closes, 1),  # 1×5m bar (Studio has no 1m)
        ret_5m=_ret(closes, 1),  # 5m = 1 bar on 5m klines
        ret_1h=_ret(closes, min(12, len(closes) - 1)),  # 12×5m ≈ 1h
        vol_short=_vol_short(closes),
        range_pos=_range_pos(closes),
        position=_position_snapshot(symbol),
        cage=empty_cage(leverage=leverage),
        extra=extra,
    )

    bridge = fetch_answers(state)
    decision_path = "jev"
    if bridge.get("status") == "ok":
        decision = apply_gate(bridge.get("answers") or {})
        writer_action = decision["action"]
        if writer_action == "HOLD":
            action = (bridge.get("answers") or {}).get("action") or {}
            probs = action.get("probabilities") or {} if isinstance(action, dict) else {}
            buy = float(probs.get("BUY") or 0)
            sell = float(probs.get("SELL") or 0)
            hold = float(probs.get("HOLD") or 0)
            if buy >= 0.28 and buy > sell and buy >= hold:
                decision = {
                    **decision,
                    "action": "BUY",
                    "vetoes": list(decision.get("vetoes") or []) + ["stance_override_long"],
                    "follow_trade": True,
                }
                writer_action = "BUY"
            elif sell >= 0.28 and sell > buy and sell >= hold:
                decision = {
                    **decision,
                    "action": "SELL",
                    "vetoes": list(decision.get("vetoes") or []) + ["stance_override_short"],
                    "follow_trade": True,
                }
                writer_action = "SELL"
    else:
        decision = apply_local(state)
        writer_action = decision["action"]
        decision_path = str(decision.get("decision_path") or "local_5m")

    emit_action = {
        "HOLD": "hold",
        "BUY": "long",
        "SELL": "short",
        "REDUCE": "close",
    }.get(writer_action, "hold")

    extra = state.get("extra") or {}
    metrics = {
        "mark": _sanitize(mark),
        "funding_rate": _sanitize(funding),
        "rows": len(closes),
        "cage_stress_noul": decision.get("cage_stress_noul"),
        "change_confidence_score": decision.get("change_confidence_score"),
        "book_imb": _sanitize(extra.get("book_imb")),
        "taker_buy_ratio": _sanitize(extra.get("taker_buy_ratio")),
        "ls_ratio": _sanitize(extra.get("ls_ratio")),
        "liq_long": _sanitize(extra.get("liq_long")),
        "liq_short": _sanitize(extra.get("liq_short")),
    }
    meta = {
        "jev_status": "bypassed" if decision_path != "jev" else bridge.get("status"),
        "jev_detail": bridge.get("detail"),
        "writer_action": writer_action,
        "raw_action": decision.get("raw_action"),
        "vetoes": decision.get("vetoes"),
        "decision_path": decision_path,
        "gate": "agenthub_5m" if decision_path != "jev" else "jev",
        "tape": extra.get("source"),
        "paper_only": True,
    }

    if emit_action in {"long", "short", "close"} and decision.get("follow_trade"):
        runtime.emit_signal_or_follow(
            action=emit_action,
            symbol=symbol,
            confidence=min(1.0, float(decision.get("change_confidence_score") or 0) / 3.0),
            metrics=metrics,
            meta=meta,
            execute_trade=lambda: _execute_paper(
                symbol=symbol,
                action=writer_action,
                margin_budget=margin_budget,
                leverage=leverage,
            ),
        )
        return

    runtime.emit_signal(
        action="hold",
        symbol=symbol,
        confidence=0.0,
        metrics=metrics,
        meta=meta,
    )


def run() -> None:
    if runtime.is_historical():
        cfg = runtime.manifest.get("strategy_config", {}) or {}
        symbol = str((cfg.get("trading_symbols") or ["BTCUSDT"])[0])
        runtime.emit_signal(
            action="watch",
            symbol=symbol,
            confidence=0.0,
            metrics={},
            meta={
                "reason": "backtest_support=none — paper writer is live/paper only",
                "paper_only": True,
            },
        )
        return
    if runtime.is_live():
        _run_live()
        return
    raise ValueError(f"unsupported evaluation_mode={runtime.evaluation_mode!r}")


if __name__ == "__main__":
    run()
