#!/usr/bin/env python3
"""Local paper book for the Mac companion. No exchange orders."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".state" / "paper_account.json"
START_EQUITY = 10_000.0
ADD_NOTIONAL = 200.0  # USDT notional per BUY/SELL add
MAX_NOTIONAL = 1_200.0
# Rebated taker: 0.06% halved. Round trip is 6 bps. Do not flip inside that.
FEE_RATE = 0.0003
HOLD_BPS = 6.0


def _empty() -> dict[str, Any]:
    return {
        "equity_start": START_EQUITY,
        "cash": START_EQUITY,
        "side": "flat",
        "qty": 0.0,
        "entry": 0.0,
        "realized": 0.0,
        "wins": 0,
        "losses": 0,
        "streak": 0,
        "last_settlement": 0.0,
        "last_action": "HOLD",
        "fills": 0,
        "history": [],
        "updated_ms": 0,
    }


def load() -> dict[str, Any]:
    if not PATH.is_file():
        return _empty()
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    out = _empty()
    out.update(data if isinstance(data, dict) else {})
    if not out.get("volume_usdt") and float(out.get("fees_paid") or 0) > 0:
        out["volume_usdt"] = float(out["fees_paid"]) / FEE_RATE
    return out


def save(acct: dict[str, Any]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(acct, indent=2) + "\n", encoding="utf-8")


def mark_to_market(acct: dict[str, Any], mark: float) -> float:
    qty = float(acct.get("qty") or 0)
    entry = float(acct.get("entry") or 0)
    side = acct.get("side") or "flat"
    if side == "flat" or not qty or not entry:
        return 0.0
    if side == "long":
        return (mark - entry) * qty
    return (entry - mark) * qty


def _move_bps(side: str, entry: float, mark: float) -> float:
    if not entry or not mark:
        return 0.0
    raw = (mark - entry) / entry * 10_000.0
    return raw if side == "long" else -raw


def _charge_open(acct: dict[str, Any], notional: float) -> float:
    fee = abs(notional) * FEE_RATE
    acct["realized"] = float(acct.get("realized") or 0) - fee
    acct["cash"] = float(acct.get("cash") or START_EQUITY) - fee
    acct["fees_paid"] = float(acct.get("fees_paid") or 0) + fee
    acct["last_fee"] = fee
    acct["open_fee"] = fee
    acct["volume_usdt"] = float(acct.get("volume_usdt") or 0) + abs(notional)
    return fee


def _close(acct: dict[str, Any], mark: float, qty: float) -> float:
    """Close qty. History pnl is net of the open fee and this close fee."""
    side = acct.get("side") or "flat"
    entry = float(acct.get("entry") or 0)
    price_pnl = (mark - entry) * qty if side == "long" else (entry - mark) * qty
    close_fee = abs(qty * mark) * FEE_RATE
    acct["volume_usdt"] = float(acct.get("volume_usdt") or 0) + abs(qty * mark)
    open_fee = float(acct.get("open_fee") or 0)
    leg_net = price_pnl - close_fee - open_fee
    acct["realized"] = float(acct.get("realized") or 0) + price_pnl - close_fee
    acct["cash"] = float(acct.get("cash") or START_EQUITY) + price_pnl - close_fee
    acct["fees_paid"] = float(acct.get("fees_paid") or 0) + close_fee
    acct["last_fee"] = close_fee
    acct["open_fee"] = 0.0
    acct["last_settlement"] = leg_net
    if leg_net >= 0:
        acct["wins"] = int(acct.get("wins") or 0) + 1
        acct["streak"] = max(int(acct.get("streak") or 0), 0) + 1
    else:
        acct["losses"] = int(acct.get("losses") or 0) + 1
        acct["streak"] = min(int(acct.get("streak") or 0), 0) - 1
    hist = list(acct.get("history") or [])
    hist.append(
        {
            "side": side,
            "qty": qty,
            "entry": entry,
            "exit": mark,
            "pnl": leg_net,
            "fee": True,
            "ts_ms": int(time.time() * 1000),
        }
    )
    acct["history"] = hist[-40:]
    return leg_net


def _flatten(acct: dict[str, Any]) -> None:
    acct["side"] = "flat"
    acct["qty"] = 0.0
    acct["entry"] = 0.0
    acct["open_fee"] = 0.0


def apply(mark: float, action: str) -> dict[str, Any]:
    """One clip. No same-tick flip. No add. Exit only after a 6 bps move."""
    acct = load()
    action = (action or "HOLD").upper()
    side = acct.get("side") or "flat"
    qty = float(acct.get("qty") or 0)
    entry = float(acct.get("entry") or 0)
    executed = "HOLD"
    acct["gate_block"] = ""
    acct["move_bps"] = 0.0

    if side in ("long", "short") and qty and entry and mark:
        move = _move_bps(side, entry, mark)
        acct["move_bps"] = move
        if abs(move) < HOLD_BPS - 1e-6:
            executed = "HOLD"
            acct["gate_block"] = "hold_for_6bps"
        else:
            _close(acct, mark, qty)
            _flatten(acct)
            acct["fills"] = int(acct.get("fills") or 0) + 1
            executed = "FLAT"
            acct["gate_block"] = "closed_at_6bps"
    elif action in ("BUY", "SELL") and mark:
        add_qty = ADD_NOTIONAL / mark
        notional = add_qty * mark
        if notional <= MAX_NOTIONAL:
            _charge_open(acct, notional)
            acct["entry"] = mark
            acct["qty"] = add_qty
            acct["side"] = "long" if action == "BUY" else "short"
            acct["fills"] = int(acct.get("fills") or 0) + 1
            acct["opened_ms"] = int(time.time() * 1000)
            executed = action
            acct["gate_block"] = "opened"

    upnl = mark_to_market(acct, mark)
    equity = START_EQUITY + float(acct.get("realized") or 0) + upnl
    acct["last_action"] = executed
    acct["jev_action"] = action
    acct["updated_ms"] = int(time.time() * 1000)
    acct["upnl"] = upnl
    acct["equity"] = equity
    acct["mark"] = mark
    save(acct)
    return acct


def position_snapshot(acct: dict[str, Any], mark: float) -> dict[str, Any]:
    qty = float(acct.get("qty") or 0)
    upnl = mark_to_market(acct, mark)
    notional = qty * mark
    upnl_pct = (upnl / notional) if notional else 0.0
    return {
        "side": acct.get("side") or "flat",
        "qty": qty,
        "upnl_pct": upnl_pct,
        "age_bars": 0,
    }


def cage_snapshot(acct: dict[str, Any], mark: float) -> dict[str, Any]:
    equity = float(acct.get("equity") or START_EQUITY)
    dd = max(0.0, (START_EQUITY - equity) / START_EQUITY * 100.0)
    notional = float(acct.get("qty") or 0) * mark
    return {
        "dd_pct": dd,
        "leverage": 10,
        "notional_usdt": notional,
        "daily_pnl_pct": (equity - START_EQUITY) / START_EQUITY * 100.0,
    }
