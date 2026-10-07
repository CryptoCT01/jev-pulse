#!/usr/bin/env python3
"""v4 multi-asset HF paper book: taker entries, post-only maker take-profit, taker stop,
maker-first soft exits (time cap / Jev close-now), fee waterfall per fill. No exchange orders.

v4: one account, many symbols. At most one open position per symbol (positions are keyed
by symbol) and at most `max_open_total` at once. Prices use each symbol's real tick
(TP rounded away from entry onto the tick grid); size uses the symbol's real size step
(nearest step to clip/price, at least one step). Every fill, round trip and fee is booked
both to the account totals and to that symbol's bucket in `by_asset`, in the same call, so
account totals always equal the sum of the per-asset buckets.

Soft exit (v3.1, per symbol): when the time cap or a Jev close-now fires, the resting
take-profit is replaced by ONE post-only exit at the touch (long: sell at best ask, short:
buy at best bid), never less aggressive than the TP it replaces, re-priced to the touch
when the touch moves away, filled only on a print THROUGH its price. Unfilled after the
window it crosses as a taker (`*_taker_fallback`). The hard stop always overrides.

Fill rules (honest, conservative):
  * taker fills at the touch from the live book: buy at best ask, sell at best bid;
  * a resting maker order fills only when a public trade prints THROUGH its price
    (sell limit P fills on a print > P; buy limit P on a print < P), filled at P;
  * a stop triggers on a print at/through the stop level and fills as a taker at the
    worse of that print and the current touch.
Fees (Bitget USDT-M list 6/2 bps, user's 50% rebate): taker 3 bps net, maker 1 bps net,
assumed identical on every perp in the universe (the contracts API lists 6/2 bps for all).
"""
from __future__ import annotations

import inspect
import json
import math
import os
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".state" / "paper_account.json"
FILLS_LOG = ROOT / "logs" / "paper_fills.jsonl"

START_EQUITY = 10_000.0
CLIP_USDT = 200.0
TICK = 0.1
DEFAULT_SYMBOL = "BTCUSDT"
FEES = {  # per side, fraction of notional
    "taker": {"full": 0.0006, "net": 0.0003},
    "maker": {"full": 0.0002, "net": 0.0001},
}
DEFAULTS = {
    "tp_bps": 8.0,          # maker take-profit distance from the entry fill
    "sl_bps": 4.0,          # taker stop distance from the entry fill
    "time_stop_s": 120.0,   # default time cap (the engine passes an adaptive one per trade)
    "passive_s": 10.0,      # soft (maker-first) exit window after the time cap, then taker
    "close_passive_s": 5.0, # soft exit window after a Jev close-now, then taker
    "exit_when_net_green": False,  # first-tick taker flatten when net after rebate > 0
    "no_scratch_exits": False,     # skip time-cap / Jev scratch
    "hard_stop_usd": 0.0,          # live: $0.85 taker stop; 0 = use bps sl_px
    "min_take_usd": 0.0,           # live: flatten only if net after rebate ≥ this
    "be_trigger_bps": 0.0,         # break-even stop: once best move ≥ this, flatten if net after fees ≤ 0. 0 = off
}
STRATEGY = "jev-pulse-v4-multi"
STRATEGY_VERSION = "4.1"
ACCOUNT_VERSION = 4
EXIT_KINDS = ("tp_maker", "time_maker", "jev_close_maker", "time_taker_fallback",
              "jev_close_taker_fallback", "stop_taker", "shutdown_taker", "fee_green_taker", "be_stop_taker")
HISTORY_KEEP = 600
FILL_KEEP = 400


def _liq_bucket() -> dict[str, float]:
    return {"count": 0, "notional": 0.0, "full": 0.0, "rebate": 0.0, "net": 0.0}


def empty_asset() -> dict[str, Any]:
    return {"realized": 0.0, "gross_realized": 0.0, "fees_paid": 0.0, "fees_full": 0.0, "fees_rebate": 0.0,
            "fees_by_liq": {"maker": _liq_bucket(), "taker": _liq_bucket()}, "funding_paid": 0.0,
            "volume_usdt": 0.0, "fills": 0, "round_trips": 0, "wins": 0, "losses": 0,
            "exits_by_kind": {}, "upnl": 0.0, "last_close": None}


def empty_account(now_ms: int | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    return {
        "version": ACCOUNT_VERSION,
        "strategy": STRATEGY,
        "strategy_version": STRATEGY_VERSION,
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
        # soft (maker-first) exits: posted, filled maker, fell back to taker, cancelled by the stop
        "soft_exits": {"posted": 0, "maker": 0, "fallback": 0, "stop_override": 0, "tp_merged": 0},
        "volume_usdt": 0.0,
        "fills": 0,
        "round_trips": 0,
        "wins": 0,
        "losses": 0,
        "streak": 0,
        "last_settlement": 0.0,
        "positions": {},          # symbol -> open position (max 1 per symbol)
        "open_count": 0,
        "side": "flat",           # summary: flat / long / short / mixed
        "upnl": 0.0,
        "last_action": "HOLD",
        "gate_block": "reset",
        "last_close": None,
        "by_asset": {s: empty_asset() for s in (symbols or [])},
        "history": [],
        "fill_log": [],
        "updated_ms": now,
    }


def _dp(tick: float) -> int:
    return max(0, -Decimal(str(tick)).normalize().as_tuple().exponent)


def round_up(px: float, tick: float = TICK) -> float:
    return round(math.ceil(px / tick - 1e-9) * tick, _dp(tick))


def round_down(px: float, tick: float = TICK) -> float:
    return round(math.floor(px / tick + 1e-9) * tick, _dp(tick))


def size_for(clip: float, px: float, step: float = 0.0, min_qty: float = 0.0) -> float:
    """Contracts for a `clip` USDT order: nearest multiple of the size step (at least one step,
    at least min_qty). step 0 = exact clip/px (v3.1 behaviour)."""
    if px <= 0:
        return 0.0
    raw = clip / px
    if not step:
        return raw
    n = max(1, int(math.floor(raw / step + 0.5)))
    q = round(n * step, _dp(step))
    return max(q, min_qty or 0.0)


class PaperBook:
    """Thread-safe: feed thread calls on_trade/reprice, engine thread calls open/close/on_clock.
    `touch(symbol) -> (bid, ask)`; a zero-argument touch() is accepted for single-symbol use."""

    def __init__(self, path: Path = PATH, params: dict | None = None,
                 touch: Callable[..., tuple[float, float]] | None = None,
                 clock: Callable[[], int] | None = None, persist: bool = True,
                 fills_log: Path | None = FILLS_LOG, specs: dict[str, dict] | None = None,
                 trips_log: Path | None = None,
                 clip_usdt: float = CLIP_USDT, max_open_total: int = 3) -> None:
        self.path = path
        self.p = {**DEFAULTS, **(params or {})}
        self.specs = specs or {DEFAULT_SYMBOL: {"tick": TICK, "size_step": 0.0, "min_qty": 0.0}}
        self.symbols = list(self.specs.keys())
        self.default = self.symbols[0]
        self.clip = float(clip_usdt)
        self.max_open_total = int(max_open_total)
        t = touch or (lambda *a: (0.0, 0.0))
        try:
            nargs = len([p for p in inspect.signature(t).parameters.values()
                         if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
        except (TypeError, ValueError):
            nargs = 1
        self._touch = (lambda s: t()) if nargs == 0 else t
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.persist = persist
        self.fills_log = fills_log
        self.trips_log = trips_log      # v4.1: one JSON line per closed round trip (input of the overnight coach)
        self.lock = threading.RLock()
        self.acct = self._load()
        self.events: list[dict] = []  # fills since the engine last drained them
        self.last_refusal = ""

    def touch(self, sym: str | None = None) -> tuple[float, float]:
        return self._touch(sym or self.default)

    def tick(self, sym: str) -> float:
        return float((self.specs.get(sym) or {}).get("tick") or TICK)

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict[str, Any]:
        if self.persist and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") == ACCOUNT_VERSION:
                    base = empty_account(symbols=self.symbols)
                    base.update(data)
                    for s in self.symbols:
                        base["by_asset"].setdefault(s, empty_asset())
                    base["strategy"], base["strategy_version"] = STRATEGY, STRATEGY_VERSION
                    return base
            except (OSError, json.JSONDecodeError):
                pass
        return empty_account(self.clock(), self.symbols)

    def save(self) -> None:
        if not self.persist:
            return
        # the tick thread and the WS fill thread can both save: serialise the whole snapshot +
        # write + rename under the (re-entrant) book lock, with a per-thread tmp name, so two
        # saves can never race on one tmp file or land an older snapshot over a newer one.
        with self.lock:
            text = json.dumps(self.acct, separators=(",", ":"), default=str) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)

    # ------------------------------------------------------------ accounting
    def _fee(self, liq: str, notional: float) -> dict[str, float]:
        full = abs(notional) * FEES[liq]["full"]
        net = abs(notional) * FEES[liq]["net"]
        return {"full": full, "rebate": full - net, "net": net}

    def _leg_net(self, p: dict, px: float, liq: str) -> float:
        """Projected round-trip net if we close at px as liq (open fee already paid)."""
        if not px or not p:
            return -1.0
        gross = (px - p["entry"]) * p["qty"] * p["dir"]
        close_fee = abs(px * p["qty"]) * FEES[liq]["net"]
        funding = float(p.get("funding") or 0.0)
        return gross - float(p.get("entry_fee_net") or 0.0) - close_fee - funding

    def _taker_exit_px(self, p: dict) -> float:
        bid, ask = self.touch(p["symbol"])
        return (bid if p["dir"] > 0 else ask) or 0.0

    def _try_fee_green(self, sym: str, ts: int | None = None) -> dict | None:
        """Flatten now if a taker at the touch would be net-green after fees. Stop still wins first."""
        if not self.p.get("exit_when_net_green"):
            return None
        p = self.acct["positions"].get(sym)
        if p is None:
            return None
        px = self._taker_exit_px(p)
        need = float(self.p.get("min_take_usd") or 0)
        if px and self._leg_net(p, px, "taker") >= (need if need > 0 else 1e-12):
            return self._close(sym, px, "taker", "fee_green_taker", ts or self.clock())
        return None

    def _try_hard_stop(self, sym: str, ts: int | None = None, print_px: float | None = None) -> dict | None:
        """Flatten as taker if closing now would be ≤ −hard_stop_usd after rebate. 0 = off."""
        cap = float(self.p.get("hard_stop_usd") or 0)
        if cap <= 0:
            return None
        p = self.acct["positions"].get(sym)
        if p is None:
            return None
        px = self._taker_exit_px(p)
        if print_px:
            d = p["dir"]
            if d > 0:
                px = min(px, print_px) if px else print_px
            else:
                px = max(px, print_px) if px else print_px
        if px and self._leg_net(p, px, "taker") <= -cap:
            return self._close(sym, px, "taker", "stop_taker", ts or self.clock())
        trig = float(self.p.get("be_trigger_bps") or 0)
        if trig > 0 and px and p.get("mfe_bps", 0.0) >= trig and self._leg_net(p, px, "taker") <= 0:
            return self._close(sym, px, "taker", "be_stop_taker", ts or self.clock())
        return None

    def _asset(self, sym: str) -> dict:
        return self.acct["by_asset"].setdefault(sym, empty_asset())

    def _record_fill(self, *, sym: str, side: str, px: float, qty: float, liq: str, reason: str,
                     ts: int, gross: float = 0.0) -> dict[str, Any]:
        a = self.acct
        notional = px * qty
        fee = self._fee(liq, notional)
        for bkt in (a, self._asset(sym)):  # the same numbers go to the total and to the symbol
            bkt["fills"] += 1
            bkt["volume_usdt"] += notional
            bkt["fees_paid"] += fee["net"]
            bkt["fees_full"] += fee["full"]
            bkt["fees_rebate"] += fee["rebate"]
            b = bkt["fees_by_liq"][liq]
            b["count"] += 1
            b["notional"] += notional
            b["full"] += fee["full"]
            b["rebate"] += fee["rebate"]
            b["net"] += fee["net"]
            bkt["realized"] += gross - fee["net"]
            bkt["gross_realized"] += gross
        a["cash"] = START_EQUITY + a["realized"]
        fill = {"ts_ms": ts, "symbol": sym, "side": side, "px": px, "qty": qty, "notional": round(notional, 4),
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
    def open(self, direction: int, *, sym: str | None = None, reason: str = "jev_entry",
             signal: dict | None = None, tp_bps: float | None = None, sl_bps: float | None = None,
             time_stop_s: float | None = None) -> dict | None:
        """Taker entry at the touch. direction +1 long / -1 short. None if refused
        (already open on this symbol, max concurrent reached, or no valid touch)."""
        sym = sym or self.default
        with self.lock:
            a = self.acct
            if sym in a["positions"]:
                self.last_refusal = "already_open"
                return None
            if len(a["positions"]) >= self.max_open_total:
                self.last_refusal = "max_open_total"
                return None
            bid, ask = self.touch(sym)
            if not bid or not ask or ask < bid:
                self.last_refusal = "no_touch"
                return None
            now = self.clock()
            spec = self.specs.get(sym) or {}
            tick = self.tick(sym)
            px = ask if direction > 0 else bid
            qty = size_for(self.clip, px, float(spec.get("size_step") or 0), float(spec.get("min_qty") or 0))
            tp = float(tp_bps or self.p["tp_bps"])
            sl = float(sl_bps or self.p["sl_bps"])
            cap = float(time_stop_s or self.p["time_stop_s"])
            fill = self._record_fill(sym=sym, side="buy" if direction > 0 else "sell", px=px, qty=qty,
                                     liq="taker", reason=reason, ts=now)
            if direction > 0:
                tp_px = round_up(px * (1 + tp / 1e4), tick)
                sl_px = px * (1 - sl / 1e4)
            else:
                tp_px = round_down(px * (1 - tp / 1e4), tick)
                sl_px = px * (1 + sl / 1e4)
            a["positions"][sym] = {
                "symbol": sym, "dir": direction, "qty": qty, "entry": px, "opened_ms": now,
                "notional": px * qty, "tick": tick,
                "entry_fee_net": fill["fee_net"], "entry_fee_full": fill["fee_full"],
                "tp_bps": tp, "sl_bps": sl, "tp_px": tp_px, "sl_px": sl_px,
                "time_cap_s": cap, "deadline_ms": now + int(cap * 1000),
                "exit": {"kind": "tp", "px": tp_px, "until_ms": None},
                "signal": signal or {}, "mae_bps": 0.0, "mfe_bps": 0.0,
            }
            a["last_action"] = "BUY" if direction > 0 else "SELL"
            a["gate_block"] = "opened"
            self.last_refusal = ""
            self._mtm_locked()
            self.save()
            return fill

    def _close(self, sym: str, px: float, liq: str, kind: str, ts: int) -> dict:
        a = self.acct
        p = a["positions"][sym]
        d, qty, entry = p["dir"], p["qty"], p["entry"]
        gross = (px - entry) * qty * d
        fill = self._record_fill(sym=sym, side="sell" if d > 0 else "buy", px=px, qty=qty, liq=liq,
                                 reason=kind, ts=ts, gross=gross)
        funding = float(p.get("funding") or 0.0)
        net = gross - p["entry_fee_net"] - fill["fee_net"] - funding
        rec = {
            "symbol": sym,
            "side": "long" if d > 0 else "short", "qty": qty, "entry": entry, "exit": px,
            "opened_ms": p["opened_ms"], "ts_ms": ts, "hold_s": round((ts - p["opened_ms"]) / 1000, 1),
            "gross": gross, "fee_open_net": p["entry_fee_net"], "fee_close_net": fill["fee_net"],
            "fee_full": p["entry_fee_full"] + fill["fee_full"],
            "rebate": (p["entry_fee_full"] - p["entry_fee_net"]) + fill["rebate"],
            "funding": funding, "pnl": net, "pnl_bps": net / (entry * qty) * 1e4,
            "gross_bps": (px / entry - 1) * 1e4 * d, "notional": entry * qty,
            "entry_liq": "taker", "exit_liq": liq, "exit_kind": kind,
            "tp_bps": p["tp_bps"], "sl_bps": p["sl_bps"], "tp_px": p["tp_px"], "sl_px": p["sl_px"],
            "time_cap_s": p.get("time_cap_s"),
            "mfe_bps": round(p["mfe_bps"], 2), "mae_bps": round(p["mae_bps"], 2),
            "signal": p.get("signal") or {}, "fee": True,
        }
        a["history"].append(rec)
        del a["history"][:-HISTORY_KEEP]
        if self.persist and self.trips_log:
            try:
                self.trips_log.parent.mkdir(parents=True, exist_ok=True)
                with self.trips_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, default=str) + "\n")
            except OSError:
                pass
        lc = {"symbol": sym, "side": rec["side"], "ts_ms": ts, "pnl": net, "kind": kind}
        for bkt in (a, self._asset(sym)):
            bkt["round_trips"] += 1
            bkt["exits_by_kind"][kind] = bkt["exits_by_kind"].get(kind, 0) + 1
            if net > 0:
                bkt["wins"] += 1
            else:
                bkt["losses"] += 1
            bkt["last_close"] = lc
        a["streak"] = (a["streak"] + 1 if a["streak"] > 0 else 1) if net > 0 else (a["streak"] - 1 if a["streak"] < 0 else -1)
        a["last_settlement"] = net
        del a["positions"][sym]
        a["last_action"] = "FLAT"
        a["gate_block"] = kind
        self._mtm_locked()
        self.save()
        return rec

    def request_close(self, kind: str = "jev_close", sym: str | None = None) -> str:
        """Jev close-now: maker-first soft exit at the touch for close_passive_s, then taker.
        When exit_when_net_green, only flatten if the taker touch is already net after fees."""
        sym = sym or self.default
        with self.lock:
            p = self.acct["positions"].get(sym)
            if p is None:
                return "flat"
            if self.p.get("no_scratch_exits"):
                return "holding_tp"
            if self.p.get("exit_when_net_green"):
                rec = self._try_fee_green(sym)
                return "green_closed" if rec else "not_green"
            if p["exit"]["kind"] != "tp":
                return "already_exiting"
            self._go_soft(sym, kind, self.p["close_passive_s"])
            return "passive_posted"

    def _soft_px(self, sym: str, p: dict) -> float:
        """Post-only price at our side of the touch, never less aggressive than the resting TP."""
        bid, ask = self.touch(sym)
        px = ask if p["dir"] > 0 else bid
        if not px:
            return 0.0
        return min(px, p["tp_px"]) if p["dir"] > 0 else max(px, p["tp_px"])

    def _go_soft(self, sym: str, why: str, secs: float) -> None:
        """Replace the resting TP with ONE soft exit order (merge: no second resting order)."""
        p = self.acct["positions"][sym]
        now = self.clock()
        px = self._soft_px(sym, p) or p["entry"]
        p["exit"] = {"kind": "passive", "px": px, "until_ms": now + int(secs * 1000), "why": why,
                     "posted_ms": now, "reprices": 0}
        self.acct["soft_exits"]["posted"] += 1
        self.save()

    def reprice(self, sym: str | None = None) -> bool:
        """Book update: keep a resting soft exit at the touch (follows the touch away, never
        chases it back). Returns True if the price changed."""
        sym = sym or self.default
        with self.lock:
            p = self.acct["positions"].get(sym)
            if p is None or p["exit"]["kind"] != "passive":
                return False
            new = self._soft_px(sym, p)
            if not new:
                return False
            cur = p["exit"]["px"]
            if (p["dir"] > 0 and new < cur - 1e-12) or (p["dir"] < 0 and new > cur + 1e-12):
                p["exit"]["px"] = new
                p["exit"]["reprices"] = int(p["exit"].get("reprices") or 0) + 1
                return True
            return False

    def force_close(self, kind: str = "shutdown_taker", sym: str | None = None) -> dict | None:
        """Clean shutdown of one symbol (default symbol if None): taker at the touch."""
        sym = sym or self.default
        with self.lock:
            p = self.acct["positions"].get(sym)
            if p is None:
                return None
            bid, ask = self.touch(sym)
            px = bid if p["dir"] > 0 else ask
            if not px:
                return None
            return self._close(sym, px, "taker", kind, self.clock())

    def force_close_all(self, kind: str = "shutdown_taker") -> list[dict]:
        with self.lock:
            out = []
            for s in list(self.acct["positions"]):
                r = self.force_close(kind, s)
                if r:
                    out.append(r)
            return out

    # ------------------------------------------------------------ events
    def on_trade(self, ts: int, px: float, sz: float, side: int, sym: str | None = None) -> dict | None:
        sym = sym or self.default
        with self.lock:
            p = self.acct["positions"].get(sym)
            if p is None or ts < p["opened_ms"]:
                return None
            d = p["dir"]
            move = (px / p["entry"] - 1) * 1e4 * d
            p["mfe_bps"] = max(p["mfe_bps"], move)
            p["mae_bps"] = min(p["mae_bps"], move)
            ex = p["exit"]
            rec = self._try_hard_stop(sym, ts, print_px=px)
            if rec:
                return rec
            if not float(self.p.get("hard_stop_usd") or 0):
                if (d > 0 and px <= p["sl_px"]) or (d < 0 and px >= p["sl_px"]):
                    bid, ask = self.touch(sym)
                    fill = min(px, bid) if d > 0 else max(px, ask)
                    if not fill:
                        fill = px
                    if ex["kind"] == "passive":
                        self.acct["soft_exits"]["stop_override"] += 1
                    return self._close(sym, fill, "taker", "stop_taker", ts)
            rec = self._try_fee_green(sym, ts)
            if rec:
                return rec
            # 2) the one resting maker order (TP or soft exit): print must go THROUGH it
            if ex["kind"] in ("tp", "passive"):
                lim = ex["px"]
                eps = p.get("tick", TICK) * 1e-6
                if (d > 0 and px > lim + eps) or (d < 0 and px < lim - eps):
                    if ex["kind"] == "tp":
                        kind = "tp_maker"
                    elif (d > 0 and lim >= p["tp_px"] - eps) or (d < 0 and lim <= p["tp_px"] + eps):
                        kind = "tp_maker"  # soft exit sat at the merged TP price: it is a TP fill
                        self.acct["soft_exits"]["tp_merged"] += 1
                    else:
                        kind = f"{ex.get('why', 'time')}_maker"
                        self.acct["soft_exits"]["maker"] += 1
                    need = float(self.p.get("min_take_usd") or 0)
                    if need > 0 and self._leg_net(p, lim, "maker") < need:
                        return None
                    return self._close(sym, lim, "maker", kind, ts)
            return None

    def on_clock(self, funding_rate: float = 0.0, funding_due_ms: int = 0,
                 funding: dict[str, tuple[float, int]] | None = None) -> dict | None:
        """Time caps, soft-exit re-price/expiry, funding for every open position. Call every
        ~200 ms. `funding` = {symbol: (rate, next_settlement_ms)}; the positional pair applies
        to every symbol when `funding` is not given. Returns the last close record, if any."""
        out = None
        with self.lock:
            for sym in list(self.acct["positions"]):
                fr, due = (funding or {}).get(sym, (funding_rate, funding_due_ms)) if funding is not None \
                    else (funding_rate, funding_due_ms)
                r = self._clock_one(sym, fr, due)
                if r:
                    out = r
        return out

    def _clock_one(self, sym: str, funding_rate: float, funding_due_ms: int) -> dict | None:
        p = self.acct["positions"].get(sym)
        if p is None:
            return None
        now = self.clock()
        if funding_due_ms and p["opened_ms"] < funding_due_ms <= now and not p.get("funding_ms"):
            p["funding"] = p["dir"] * funding_rate * p["qty"] * p["entry"]
            p["funding_ms"] = funding_due_ms
            for bkt in (self.acct, self._asset(sym)):
                bkt["funding_paid"] += p["funding"]
                bkt["realized"] -= p["funding"]
        rec = self._try_hard_stop(sym, now)
        if rec:
            return rec
        rec = self._try_fee_green(sym, now)
        if rec:
            return rec
        ex = p["exit"]
        if self.p.get("no_scratch_exits") or self.p.get("exit_when_net_green"):
            return None
        if ex["kind"] == "tp" and now >= p["deadline_ms"]:
            self._go_soft(sym, "time", self.p["passive_s"])
            return None
        if ex["kind"] == "passive":
            if now >= (ex["until_ms"] or 0):
                bid, ask = self.touch(sym)
                px = bid if p["dir"] > 0 else ask
                if not px:
                    return None
                self.acct["soft_exits"]["fallback"] += 1
                return self._close(sym, px, "taker", f"{ex.get('why', 'time')}_taker_fallback", now)
            self.reprice(sym)
        return None

    # ------------------------------------------------------------ views
    def _mtm_locked(self) -> None:
        a = self.acct
        tot = 0.0
        sides = set()
        for s, bk in a["by_asset"].items():
            bk["upnl"] = 0.0
        for s, p in a["positions"].items():
            bid, ask = self.touch(s)
            u = 0.0
            if bid and ask:
                mid = (bid + ask) / 2
                u = (mid - p["entry"]) * p["qty"] * p["dir"]
                p["move_bps"] = (mid / p["entry"] - 1) * 1e4 * p["dir"]
                p["mark"] = mid
            p["upnl"] = u
            self._asset(s)["upnl"] = u
            tot += u
            sides.add("long" if p["dir"] > 0 else "short")
        a["upnl"] = tot
        a["open_count"] = len(a["positions"])
        a["side"] = "flat" if not sides else (sides.pop() if len(sides) == 1 else "mixed")
        a["equity"] = START_EQUITY + a["realized"] + tot
        a["updated_ms"] = self.clock()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._mtm_locked()
            return json.loads(json.dumps(self.acct, default=str))

    def drain_events(self) -> list[dict]:
        with self.lock:
            ev, self.events = self.events, []
            return ev

    def position(self, sym: str | None = None) -> dict | None:
        with self.lock:
            p = self.acct["positions"].get(sym or self.default)
            return dict(p) if p else None

    def positions(self) -> dict[str, dict]:
        with self.lock:
            return {s: dict(p) for s, p in self.acct["positions"].items()}
