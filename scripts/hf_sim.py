#!/usr/bin/env python3
"""v3 HF paper book: taker entries, post-only maker take-profit, taker stop,
time stop with a short passive exit, fee waterfall per fill. No exchange orders.

Fill rules (honest, conservative):
  * taker fills at the touch from the live book: buy at best ask, sell at best bid;
  * a resting maker order fills only when a public trade prints THROUGH its price
    (sell limit P fills on a print > P; buy limit P on a print < P), filled at P;
  * a stop triggers on a print at/through the stop level and fills as a taker at the
    worse of that print and the current touch.
Fees (Bitget USDT-M, user's 50% rebate): taker 6 bps full / 3 bps net, maker
2 bps full / 1 bps net. Every fill records full fee, rebate and net separately.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".state" / "paper_account.json"
FILLS_LOG = ROOT / "logs" / "paper_fills.jsonl"

START_EQUITY = 10_000.0
CLIP_USDT = 200.0
TICK = 0.1
FEES = {  # per side, fraction of notional
    "taker": {"full": 0.0006, "net": 0.0003},
    "maker": {"full": 0.0002, "net": 0.0001},
}
DEFAULTS = {
    "tp_bps": 8.0,          # maker take-profit distance from the entry fill
    "sl_bps": 4.0,          # taker stop distance from the entry fill
    "time_stop_s": 120.0,   # then passive exit at the touch
    "passive_s": 10.0,      # passive exit window before crossing as taker
    "close_passive_s": 5.0, # passive window for a Jev close-now call
}
HISTORY_KEEP = 400
FILL_KEEP = 300


def _liq_bucket() -> dict[str, float]:
    return {"count": 0, "notional": 0.0, "full": 0.0, "rebate": 0.0, "net": 0.0}


def empty_account(now_ms: int | None = None) -> dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    return {
        "version": 3,
        "strategy": "jev-pulse-v3-hf",
        "equity_start": START_EQUITY,
        "run_start_ms": now,
        "cash": START_EQUITY,
        "equity": START_EQUITY,
        "realized": 0.0,          # net of all fees and funding
        "gross_realized": 0.0,    # price PnL only
        "funding_paid": 0.0,
        "fees_paid": 0.0,         # net fees actually charged (after rebate)
        "fees_full": 0.0,         # list-price fees before the rebate
        "fees_rebate": 0.0,
        "fees_by_liq": {"maker": _liq_bucket(), "taker": _liq_bucket()},
        "exits_by_kind": {},
        "volume_usdt": 0.0,
        "fills": 0,
        "round_trips": 0,
        "wins": 0,
        "losses": 0,
        "streak": 0,
        "last_settlement": 0.0,
        "side": "flat",
        "qty": 0.0,
        "entry": 0.0,
        "pos": None,
        "upnl": 0.0,
        "last_action": "HOLD",
        "gate_block": "reset",
        "move_bps": 0.0,
        "last_close": None,
        "history": [],
        "fill_log": [],
        "updated_ms": now,
    }


def round_up(px: float) -> float:
    return round(math.ceil(px / TICK - 1e-9) * TICK, 1)


def round_down(px: float) -> float:
    return round(math.floor(px / TICK + 1e-9) * TICK, 1)


class PaperBook:
    """Thread-safe: feed thread calls on_trade, engine thread calls open/close/on_clock."""

    def __init__(self, path: Path = PATH, params: dict | None = None,
                 touch: Callable[[], tuple[float, float]] | None = None,
                 clock: Callable[[], int] | None = None, persist: bool = True,
                 fills_log: Path | None = FILLS_LOG) -> None:
        self.path = path
        self.p = {**DEFAULTS, **(params or {})}
        self.touch = touch or (lambda: (0.0, 0.0))
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.persist = persist
        self.fills_log = fills_log
        self.lock = threading.RLock()
        self.acct = self._load()
        self.events: list[dict] = []  # fills since the engine last drained them

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict[str, Any]:
        if self.persist and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") == 3:
                    base = empty_account()
                    base.update(data)
                    return base
            except (OSError, json.JSONDecodeError):
                pass
        return empty_account(self.clock())

    def save(self) -> None:
        if not self.persist:
            return
        with self.lock:
            text = json.dumps(self.acct, indent=1, default=str) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ accounting
    def _fee(self, liq: str, notional: float) -> dict[str, float]:
        full = abs(notional) * FEES[liq]["full"]
        net = abs(notional) * FEES[liq]["net"]
        return {"full": full, "rebate": full - net, "net": net}

    def _record_fill(self, *, side: str, px: float, qty: float, liq: str, reason: str,
                     ts: int, gross: float = 0.0) -> dict[str, Any]:
        a = self.acct
        notional = px * qty
        fee = self._fee(liq, notional)
        a["fills"] += 1
        a["volume_usdt"] += notional
        a["fees_paid"] += fee["net"]
        a["fees_full"] += fee["full"]
        a["fees_rebate"] += fee["rebate"]
        b = a["fees_by_liq"][liq]
        b["count"] += 1
        b["notional"] += notional
        b["full"] += fee["full"]
        b["rebate"] += fee["rebate"]
        b["net"] += fee["net"]
        a["realized"] += gross - fee["net"]
        a["gross_realized"] += gross
        a["cash"] = START_EQUITY + a["realized"]
        fill = {"ts_ms": ts, "side": side, "px": px, "qty": qty, "notional": round(notional, 4),
                "liq": liq, "reason": reason, "fee_full": fee["full"], "rebate": fee["rebate"],
                "fee_net": fee["net"], "gross": gross, "net": gross - fee["net"]}
        a["fill_log"].append(fill)
        del a["fill_log"][:-FILL_KEEP]
        self.events.append(fill)
        if self.persist and self.fills_log:
            try:
                self.fills_log.parent.mkdir(parents=True, exist_ok=True)
                with self.fills_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(fill) + "\n")
            except OSError:
                pass
        return fill

    # ------------------------------------------------------------ actions
    def open(self, direction: int, *, reason: str = "jev_entry", signal: dict | None = None,
             tp_bps: float | None = None, sl_bps: float | None = None) -> dict | None:
        """Taker entry at the touch. direction +1 long / -1 short."""
        with self.lock:
            a = self.acct
            if a["pos"] is not None:
                return None
            bid, ask = self.touch()
            if not bid or not ask or ask < bid:
                return None
            now = self.clock()
            px = ask if direction > 0 else bid
            qty = CLIP_USDT / px
            tp = float(tp_bps or self.p["tp_bps"])
            sl = float(sl_bps or self.p["sl_bps"])
            fill = self._record_fill(side="buy" if direction > 0 else "sell", px=px, qty=qty,
                                     liq="taker", reason=reason, ts=now)
            if direction > 0:
                tp_px = round_up(px * (1 + tp / 1e4))
                sl_px = px * (1 - sl / 1e4)
            else:
                tp_px = round_down(px * (1 - tp / 1e4))
                sl_px = px * (1 + sl / 1e4)
            a["pos"] = {
                "dir": direction, "qty": qty, "entry": px, "opened_ms": now,
                "entry_fee_net": fill["fee_net"], "entry_fee_full": fill["fee_full"],
                "tp_bps": tp, "sl_bps": sl, "tp_px": tp_px, "sl_px": sl_px,
                "deadline_ms": now + int(self.p["time_stop_s"] * 1000),
                "exit": {"kind": "tp", "px": tp_px, "until_ms": None},
                "signal": signal or {}, "mae_bps": 0.0, "mfe_bps": 0.0,
            }
            a["side"], a["qty"], a["entry"] = ("long" if direction > 0 else "short"), qty, px
            a["last_action"] = "BUY" if direction > 0 else "SELL"
            a["gate_block"] = "opened"
            a["opened_ms"] = now
            self._mtm_locked()
            self.save()
            return fill

    def _close(self, px: float, liq: str, kind: str, ts: int) -> dict:
        a = self.acct
        p = a["pos"]
        d, qty, entry = p["dir"], p["qty"], p["entry"]
        gross = (px - entry) * qty * d
        fill = self._record_fill(side="sell" if d > 0 else "buy", px=px, qty=qty, liq=liq,
                                 reason=kind, ts=ts, gross=gross)
        funding = float(p.get("funding") or 0.0)
        net = gross - p["entry_fee_net"] - fill["fee_net"] - funding
        rec = {
            "side": "long" if d > 0 else "short", "qty": qty, "entry": entry, "exit": px,
            "opened_ms": p["opened_ms"], "ts_ms": ts, "hold_s": round((ts - p["opened_ms"]) / 1000, 1),
            "gross": gross, "fee_open_net": p["entry_fee_net"], "fee_close_net": fill["fee_net"],
            "fee_full": p["entry_fee_full"] + fill["fee_full"],
            "rebate": (p["entry_fee_full"] - p["entry_fee_net"]) + fill["rebate"],
            "funding": funding, "pnl": net, "pnl_bps": net / (entry * qty) * 1e4,
            "gross_bps": (px / entry - 1) * 1e4 * d,
            "entry_liq": "taker", "exit_liq": liq, "exit_kind": kind,
            "tp_bps": p["tp_bps"], "sl_bps": p["sl_bps"],
            "mfe_bps": round(p["mfe_bps"], 2), "mae_bps": round(p["mae_bps"], 2),
            "signal": p.get("signal") or {}, "fee": True,
        }
        a["history"].append(rec)
        del a["history"][:-HISTORY_KEEP]
        a["round_trips"] += 1
        a["exits_by_kind"][kind] = a["exits_by_kind"].get(kind, 0) + 1
        if net > 0:
            a["wins"] += 1
            a["streak"] = a["streak"] + 1 if a["streak"] > 0 else 1
        else:
            a["losses"] += 1
            a["streak"] = a["streak"] - 1 if a["streak"] < 0 else -1
        a["last_settlement"] = net
        a["last_close"] = {"side": rec["side"], "ts_ms": ts, "pnl": net, "kind": kind}
        a["pos"] = None
        a["side"], a["qty"], a["entry"] = "flat", 0.0, 0.0
        a["last_action"] = "FLAT"
        a["gate_block"] = kind
        self._mtm_locked()
        self.save()
        return rec

    def request_close(self, kind: str = "jev_close") -> str:
        """Jev close-now: passive at the touch for close_passive_s, then taker."""
        with self.lock:
            p = self.acct["pos"]
            if p is None:
                return "flat"
            if p["exit"]["kind"] in ("passive", "taker"):
                return "already_exiting"
            self._go_passive(kind, self.p["close_passive_s"])
            return "passive_posted"

    def _go_passive(self, kind: str, secs: float) -> None:
        p = self.acct["pos"]
        bid, ask = self.touch()
        now = self.clock()
        # post-only at our side of the touch: long sells at the ask, short buys at the bid
        px = ask if p["dir"] > 0 else bid
        if not px:
            px = p["entry"]
        # a passive exit never undercuts the take-profit that was already resting
        if p["dir"] > 0:
            px = min(px, p["tp_px"])
        else:
            px = max(px, p["tp_px"])
        p["exit"] = {"kind": "passive", "px": px, "until_ms": now + int(secs * 1000), "why": kind}
        self.save()

    # ------------------------------------------------------------ events
    def on_trade(self, ts: int, px: float, sz: float, side: int) -> dict | None:
        with self.lock:
            p = self.acct["pos"]
            if p is None or ts < p["opened_ms"]:
                return None
            d = p["dir"]
            move = (px / p["entry"] - 1) * 1e4 * d
            p["mfe_bps"] = max(p["mfe_bps"], move)
            p["mae_bps"] = min(p["mae_bps"], move)
            ex = p["exit"]
            # 1) resting maker order (take-profit or passive exit): print must go THROUGH
            if ex["kind"] in ("tp", "passive"):
                lim = ex["px"]
                if (d > 0 and px > lim + 1e-9) or (d < 0 and px < lim - 1e-9):
                    kind = "tp_maker" if ex["kind"] == "tp" else f"{ex.get('why', 'time')}_maker"
                    return self._close(lim, "maker", kind, ts)
            # 2) stop: taker at the worse of the triggering print and the touch
            if (d > 0 and px <= p["sl_px"]) or (d < 0 and px >= p["sl_px"]):
                bid, ask = self.touch()
                fill = min(px, bid) if d > 0 else max(px, ask)
                if not fill:
                    fill = px
                return self._close(fill, "taker", "stop_taker", ts)
            return None

    def on_clock(self, funding_rate: float = 0.0, funding_due_ms: int = 0) -> dict | None:
        """Time stop, passive-exit expiry, funding. Call every ~250 ms."""
        with self.lock:
            p = self.acct["pos"]
            if p is None:
                return None
            now = self.clock()
            if funding_due_ms and p["opened_ms"] < funding_due_ms <= now and not p.get("funding_ms"):
                p["funding"] = p["dir"] * funding_rate * p["qty"] * p["entry"]
                p["funding_ms"] = funding_due_ms
                self.acct["funding_paid"] += p["funding"]
                self.acct["realized"] -= p["funding"]
            ex = p["exit"]
            if ex["kind"] == "tp" and now >= p["deadline_ms"]:
                self._go_passive("time", self.p["passive_s"])
                return None
            if ex["kind"] == "passive" and now >= (ex["until_ms"] or 0):
                bid, ask = self.touch()
                px = bid if p["dir"] > 0 else ask
                if not px:
                    return None
                return self._close(px, "taker", f"{ex.get('why', 'time')}_taker", now)
            return None

    # ------------------------------------------------------------ views
    def _mtm_locked(self) -> None:
        a = self.acct
        p = a["pos"]
        bid, ask = self.touch()
        upnl = 0.0
        if p is not None and bid and ask:
            mid = (bid + ask) / 2
            upnl = (mid - p["entry"]) * p["qty"] * p["dir"]
            a["move_bps"] = (mid / p["entry"] - 1) * 1e4 * p["dir"]
        else:
            a["move_bps"] = 0.0
        a["upnl"] = upnl
        a["equity"] = START_EQUITY + a["realized"] + upnl
        a["updated_ms"] = self.clock()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._mtm_locked()
            return json.loads(json.dumps(self.acct, default=str))

    def drain_events(self) -> list[dict]:
        with self.lock:
            ev, self.events = self.events, []
            return ev

    def position(self) -> dict | None:
        with self.lock:
            p = self.acct["pos"]
            return dict(p) if p else None
