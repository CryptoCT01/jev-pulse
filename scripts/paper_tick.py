#!/usr/bin/env python3
"""One offline paper tick: Agent Hub tape → Jev gate → JSONL. No live orders.

Does not start a second OpenRouter loop — this IS the companion tick.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "playbook" / "src"))

import jev_gate  # noqa: E402
import paper_sim  # noqa: E402
import tape  # noqa: E402
from gate_map import apply_gate  # noqa: E402


def _rets(closes: list[float], n: int) -> float:
    if len(closes) <= n or closes[-1 - n] == 0:
        return 0.0
    return (closes[-1] / closes[-1 - n]) - 1.0


def build_state(symbol: str = "BTCUSDT") -> tuple[dict, dict]:
    tape_snap = tape.snapshot(symbol)
    extra = tape_snap.get("extra") or {}
    mark = float(tape_snap.get("mark") or 0)
    closes = [float(c["c"]) for c in (tape_snap.get("candles") or []) if c.get("c")]
    high = max(closes[-30:]) if len(closes) >= 2 else mark
    low = min(closes[-30:]) if len(closes) >= 2 else mark
    range_pos = 0.5 if high == low else (mark - low) / (high - low)

    acct = paper_sim.load()
    pos = paper_sim.position_snapshot(acct, mark)
    cage = paper_sim.cage_snapshot(acct, mark)

    # Compact extra only — never send full book/candles to Jev (OpenRouter tokens).
    jev_extra = {
        "book_imb": extra.get("book_imb"),
        "spread_bps": extra.get("spread_bps"),
        "taker_buy_ratio": extra.get("taker_buy_ratio"),
        "ls_ratio": extra.get("ls_ratio"),
        "large": extra.get("large"),
        "oi": extra.get("oi"),
    }

    state = {
        "symbol": symbol,
        "ts_ms": int(time.time() * 1000),
        "mark": mark,
        "funding_rate": float(tape_snap.get("funding_rate") or 0),
        "ret_1m": _rets(closes, 1),
        "ret_5m": _rets(closes, 5),
        "ret_1h": _rets(closes, min(60, max(1, len(closes) - 1))),
        "vol_short": (
            (sum((c - sum(closes[-20:]) / 20) ** 2 for c in closes[-20:]) / 20) ** 0.5 / mark
            if len(closes) >= 20 and mark
            else 0.0
        ),
        "range_pos": range_pos,
        "position": pos,
        "cage": cage,
        "extra": jev_extra,
        "source": "bitget_public_agenthub",
    }
    return state, tape_snap


def stance_from_answers(answers: dict) -> dict:
    action = answers.get("action") or {}
    probs = action.get("probabilities") or {}
    hold = float(probs.get("HOLD") or 0)
    buy = float(probs.get("BUY") or 0)
    sell = float(probs.get("SELL") or 0)
    if buy >= sell and buy >= hold:
        label, pct = "Long", buy
    elif sell >= buy and sell >= hold:
        label, pct = "Short", sell
    else:
        label, pct = "Flat", hold
    return {"label": label, "pct": round(pct * 100), "hold": hold, "buy": buy, "sell": sell}


def main(argv: list[str] | None = None) -> int:
    jev_gate._load_dotenv()
    parser = argparse.ArgumentParser(description="Offline Jev paper tick")
    parser.add_argument("--once", action="store_true", default=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--dry-schema", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--log",
        default=str(ROOT / "logs" / "paper_ticks.jsonl"),
        help="Append JSONL path",
    )
    args = parser.parse_args(argv)

    state, tape_snap = build_state(args.symbol)
    if args.dry_schema:
        print(json.dumps({"state": state, "questions": jev_gate.build_questions()}, indent=2))
        return 0

    t0 = time.time()
    raw = jev_gate.decide(state)
    latency_ms = int((time.time() - t0) * 1000)
    answers = raw.get("answers") or raw.get("data", {}).get("answers") or {}
    decision = apply_gate(answers)
    stance = stance_from_answers(answers)
    mark = float(state["mark"])
    jev_action = decision.get("action") or "HOLD"
    acct = paper_sim.apply(mark, jev_action)
    decision = {
        **decision,
        "jev_action": jev_action,
        "action": acct.get("last_action") or "HOLD",
        "gate_block": acct.get("gate_block") or "",
        "move_bps": acct.get("move_bps"),
    }
    # Refresh position after fill so JSONL matches the book Jev will see next tick.
    state["position"] = paper_sim.position_snapshot(acct, mark)
    state["cage"] = paper_sim.cage_snapshot(acct, mark)

    record = {
        "ts_ms": state["ts_ms"],
        "symbol": args.symbol,
        "state": state,
        "answers": answers,
        "writer_action": decision,
        "stance": stance_from_answers(answers),
        "account": {
            "equity": acct.get("equity"),
            "realized": acct.get("realized"),
            "upnl": acct.get("upnl"),
            "side": acct.get("side"),
            "qty": acct.get("qty"),
            "entry": acct.get("entry"),
            "streak": acct.get("streak"),
            "wins": acct.get("wins"),
            "losses": acct.get("losses"),
            "last_settlement": acct.get("last_settlement"),
            "fills": acct.get("fills"),
            "fees_paid": acct.get("fees_paid"),
            "volume_usdt": acct.get("volume_usdt"),
            "gate_block": acct.get("gate_block"),
            "move_bps": acct.get("move_bps"),
            "last_action": acct.get("last_action"),
            "history": acct.get("history") or [],
        },
        "tape": {
            "events": (tape_snap.get("extra") or {}).get("events") or [],
            "book_imb": (tape_snap.get("extra") or {}).get("book_imb"),
            "ls_ratio": (tape_snap.get("extra") or {}).get("ls_ratio"),
            "fetch_ms": tape_snap.get("fetch_ms"),
        },
        "latency_ms": latency_ms,
        "model": raw.get("model") or jev_gate.DEFAULT_MODEL,
        "usage": raw.get("usage"),
        "mode": "paper_offline",
        "live_orders": False,
    }
    try:
        import stance_publish  # noqa: E402

        gist_res = stance_publish.publish(record)
        record["gist"] = {"ok": bool(gist_res.get("ok")), "skipped": gist_res.get("skipped")}
    except Exception as exc:
        record["gist"] = {"ok": False, "error": type(exc).__name__}
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    # Desk reads this file — no extra Bitget/OpenRouter from the UI.
    (ROOT / "logs" / "tape_latest.json").write_text(
        json.dumps(tape_snap, default=str), encoding="utf-8"
    )

    quiet = args.quiet or (not sys.stdout.isatty())
    if quiet:
        wa = decision.get("action")
        print(
            f"{wa} stance={(record['stance'] or {}).get('label')} "
            f"{(record['stance'] or {}).get('pct')}% {latency_ms}ms mark={mark:.1f}",
            file=sys.stderr,
        )
    else:
        print(json.dumps(record, indent=2, default=str))
        print(f"appended {log_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
