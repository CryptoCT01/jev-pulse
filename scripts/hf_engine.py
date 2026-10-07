#!/usr/bin/env python3
"""Jev Pulse v4 multi-asset HF paper engine: one persistent process.

  Bitget public WS (books15 + trades + ticker for every symbol in the universe)
  -> per-asset stats every 2.5 s (1 s mids, R = typical 120 s range, spread, depth near touch)
  -> room score per asset = R / (round-trip cost + spread) x thin-depth penalty; closed or
     motionless markets are `idle` with the reason and are never asked about or traded
  -> TypeSafe Jev, per asset, with that asset's own context: the top-N flat, enabled,
     live candidates by room score (direction / expected move / risk) plus close-now for
     each open position, in parallel, under a hard $/h budget
  -> local gate per asset (E >= E_min(asset) = max(cost, adaptive base) + spread, room, flow,
     cooldown, risk veto) -> honest paper fills on a shared $10k account.
Exits (maker TP clamp(k*R, 5, tp_max), taker stop clamp(0.8*TP, 4, stop_max), adaptive time
cap 120-300 s, maker-first soft exits with a 20 s window then taker, Jev close-now >= 0.70)
run per symbol on every public trade, every book update and a 200 ms clock.
Asset on/off toggles: .state/assets.json (written by the dash), re-read every tick. OFF = no
new entries; an open trade keeps its normal exits and the asset shows `closing` until flat.
PAPER ONLY: this file has no exchange order code and never sends orders to Bitget.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hf_control  # noqa: E402
import hf_feed  # noqa: E402
import hf_jev  # noqa: E402
import hf_overlay  # noqa: E402
import hf_sim  # noqa: E402

# HF_OUT_DIR redirects every output (ticks, tape, account, gate state, controls) for shadow runs.
OUT = Path(os.environ["HF_OUT_DIR"]) if os.environ.get("HF_OUT_DIR") else None
LOG = (OUT or ROOT / "logs") / "paper_ticks.jsonl"
DECISIONS_LOG = (OUT or ROOT / "logs") / "paper_decisions.jsonl"
TAPE = (OUT or ROOT / "logs") / "tape_latest.json"
MIDS_LIVE = (OUT or ROOT / "logs") / "mids_live.json"
PARAMS_FILE = Path(os.environ.get("HF_PARAMS") or ROOT / "scripts" / "hf_params.json")
GATE_STATE = (OUT or ROOT / ".state") / "hf_gate.json"
ACCT_PATH = (OUT or ROOT / ".state") / "paper_account.json"
FILLS_PATH = (OUT or ROOT / "logs") / "paper_fills.jsonl"
TRIPS_PATH = (OUT or ROOT / "logs") / "paper_trips.jsonl"   # v4.1: one line per closed round trip (coach input)
OVERLAY_PATH = Path(os.environ.get("HF_OVERLAY") or (OUT / "overlay.json" if OUT else ROOT / "playbook" / "overlay.json"))
CONTROL_PATH = Path(os.environ.get("HF_CONTROL") or (OUT or ROOT / ".state") / "assets.json")
CONTRACTS_CACHE = ROOT / ".state" / "contracts.json"
PUBLISH = OUT is None and os.environ.get("HF_NO_PUBLISH") != "1"  # shadow / dry runs never touch the gist
HIST_IN_TICK = 6
STRATEGY_TAG = "v4-multi"


def load_params() -> dict:
    return json.loads(PARAMS_FILE.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ pure strategy functions
def micro_score(f: dict) -> float:
    """Deterministic flow/book agreement score in [-1, 1]."""
    return round(0.45 * f.get("imb_5bps", 0) + 0.30 * f.get("imb_l1", 0)
                 + 0.15 * f.get("flow_5s", 0) + 0.10 * f.get("flow_15s", 0), 3)


def tp_for(f: dict, P: dict) -> float:
    """Take-profit: clamp(k x R, tp_min, tp_max) with R = this asset's typical 120 s high-low
    range of 1 s mids. tp_min (5) stays above the 4 bps taker-in + maker-out cost. No R -> tp_min."""
    r = f.get("range_h_bps")
    raw = P["tp_range_k"] * r if r else P["tp_bps_min"]
    return round(min(P["tp_bps_max"], max(P["tp_bps_min"], raw)), 1)


def sl_for(tp: float, P: dict) -> float:
    """Stop: clamp(sl_mult x TP, sl_min, sl_max). TP 5 -> 4 bps (the v3.1 stop)."""
    return round(min(P["sl_bps_max"], max(P["sl_bps_min"], P["sl_mult"] * tp)), 1)


def time_cap_for(f: dict, P: dict) -> float:
    """Adaptive time cap: base x (ref_range / R), clamped to [min, max]; quiet -> longer."""
    r = f.get("range_h_bps")
    base = float(P["time_stop_s"])
    raw = base * P["time_cap_ref_range_bps"] / r if r else base
    return float(round(min(P["time_cap_max_s"], max(P["time_cap_min_s"], raw))))


def e_min_for(base: float, spread_bps: float, P: dict) -> float:
    """Adaptive entry floor for one asset: max(round-trip cost, adaptive base) + spread."""
    return round(max(float(P["cost_rt_bps"]), float(base)) + max(0.0, float(spread_bps or 0)), 3)


def room_for(r_bps: float | None, spread_bps: float, depth_min_usd: float, P: dict) -> dict:
    """Room score = R / (round-trip cost + spread) x depth factor, depth factor =
    min(1, sqrt(depth within 5 bps on the thinner side / depth_ref_usd)), floor 0.25."""
    if r_bps is None:
        return {"score": None, "depth_factor": None}
    cost = float(P["cost_rt_bps"]) + max(0.0, float(spread_bps or 0))
    ref = float(P.get("depth_ref_usd") or 0)
    dfac = 1.0 if not ref else max(0.25, min(1.0, (max(0.0, depth_min_usd) / ref) ** 0.5))
    return {"score": round(r_bps / cost * dfac, 3), "depth_factor": round(dfac, 3), "cost_bps": round(cost, 3)}


def asset_status(f: dict, rs: dict, ws_up: bool, P: dict) -> tuple[str, str]:
    """(status, reason). status: live | idle | warming | no_feed. idle = closed / not moving
    (never an error, never traded)."""
    if not ws_up:
        return "no_feed", "public socket reconnecting"
    if not f:
        return "no_feed", "no order book yet"
    ba = f.get("book_age_ms")
    lt = f.get("last_trade_age_s")
    if lt is None or lt > P["idle_no_trade_s"]:
        return "idle", "no trades"
    if ba is None or ba > P["idle_book_stale_s"] * 1000:
        return "idle", "book not updating"
    r = rs.get("range_bps")
    if r is None:
        return "warming", f"warming {rs.get('samples_s', 0)}/{P['vol_min_samples_s']}s"
    if r < P["idle_range_bps"]:
        return "idle", "not moving"
    return "live", ""


def effective_state(enabled: bool, has_pos: bool, status: str) -> str:
    """What the engine actually applies: on | off | closing | idle | warming | no_feed."""
    if not enabled:
        return "closing" if has_pos else "off"
    if status == "live" or has_pos:
        return "on"
    return status


def pick_candidates(assets: dict[str, dict], positions: dict, P: dict) -> list[str]:
    """Top-N flat, enabled, live assets whose room passes, by room score. None when the
    concurrent-position cap is already full (nothing could be opened)."""
    if len(positions) >= int(P["max_open_total"]):
        return []
    c = [s for s, a in assets.items()
         if a["enabled"] and s not in positions and a["status"] == "live"
         and a.get("room") is not None and a["room"] >= P["room_min"]
         and a.get("spread_bps", 99) <= P["spread_max_bps"]]
    c.sort(key=lambda s: -assets[s]["room"])
    # never ask more flat calls than there are free slots (saves Jev budget for close-now checks)
    return c[: min(int(P["jev_top_n"]), int(P["max_open_total"]) - len(positions))]


def gate_entry(f: dict, j: dict, ctx: dict, P: dict) -> tuple[int, str, dict]:
    """Return (direction, reason, detail). direction 0 = no trade. Pure function.
    ctx: exp_move_min = E_min for this asset (already includes its spread), last_close for
    this asset, now_ms, optional room (must be >= room_min when given)."""
    m = micro_score(f)
    e_min = float(ctx.get("exp_move_min", P["exp_move_min"]))
    pl, ps = j["p_long"], j["p_short"]
    d = 1 if pl >= ps else -1
    p_dir, p_opp = (pl, ps) if d > 0 else (ps, pl)
    det = {"micro": m, "e_min": e_min, "p_dir": p_dir, "edge": round(p_dir - p_opp, 3),
           "exp_move": j["exp_move_bps"], "dir": d}
    if j["risk_stress"] >= P["risk_veto"]:
        return 0, "veto_risk_stress", det
    if f.get("spread_bps", 99) > P["spread_max_bps"]:
        return 0, "veto_spread", det
    if (f.get("book_age_ms") or 0) > P["book_stale_ms"]:
        return 0, "veto_stale_book", det
    if ctx.get("room") is not None and ctx["room"] < P.get("room_min", 0):
        return 0, "no_room", det
    # Quiet scalp: a large expected move is a trend. This book banks a small take, so those
    # calls run to the dollar stop. Skip them. 0 / missing = off.
    quiet = float(P.get("quiet_max_bps") or 0)
    if quiet and float(j.get("exp_move_bps") or 0) >= quiet:
        return 0, "quiet_skip", det
    if P.get("fast_enter"):
        if p_dir < 0.51:
            return 0, "weak_direction", det
        if P.get("invert_side"):
            det["jev_dir"] = d
            d = -d
            det["dir"] = d
            det["inverted"] = True
        return d, "enter", det
    if p_dir < P["p_dir_min"] or p_dir - p_opp < P["edge_min"]:
        return 0, "weak_direction", det
    need_move, need_micro = e_min, P["micro_min"]
    lc = ctx.get("last_close") or {}
    if lc and (ctx["now_ms"] - int(lc.get("ts_ms") or 0)) < P["same_side_cooldown_s"] * 1000 \
            and lc.get("side") == ("long" if d > 0 else "short"):
        need_move += P["reentry_extra_move_bps"]
        need_micro *= P["reentry_micro_mult"]
        det["cooldown"] = True
    if j["exp_move_bps"] < need_move:
        return 0, "move_below_cost", det
    if m * d < need_micro:
        return 0, "flow_disagrees", det
    if f.get("sigma_120s_bps", 0) < P["sigma_min_bps"]:
        return 0, "vol_too_low", det
    if P.get("invert_side"):
        det["jev_dir"] = d
        d = -d
        det["dir"] = d
        det["inverted"] = True
    return d, "enter", det


class Budget:
    """Hard Jev spend limit: token bucket refilled at usd_h/3600 $/s, capacity = burst_s of
    spend. A call is made only if the bucket holds its estimated cost; the actual cost is
    settled after the call. Rolling spend is logged per hour."""

    def __init__(self, usd_h: float, burst_s: float, clock=time.time) -> None:
        self.rate = usd_h / 3600.0
        self.cap = self.rate * burst_s
        self.tokens = self.cap
        self.clock = clock
        self.t = clock()
        self.spent: deque = deque()  # (ts, cost)
        self.total = 0.0
        self.calls = 0
        self.skipped = 0
        self.t0 = clock()
        self.est = {"flat": 6e-5, "in_position": 5e-5}

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now

    def take(self, mode: str) -> bool:
        self._refill()
        e = self.est.get(mode, 6e-5)
        if self.tokens >= e:
            self.tokens -= e
            return True
        self.skipped += 1
        return False

    def settle(self, mode: str, cost: float) -> None:
        e = self.est.get(mode, 6e-5)
        self.tokens += e - cost
        if cost > 0:
            self.est[mode] = 0.8 * e + 0.2 * cost
        now = self.clock()
        self.spent.append((now, cost))
        self.total += cost
        self.calls += 1
        while self.spent and self.spent[0][0] < now - 3600:
            self.spent.popleft()

    def per_hour(self) -> dict:
        now = self.clock()
        win = min(3600.0, max(1.0, now - self.t0))
        last = sum(c for t, c in self.spent if t >= now - 3600)
        ten = sum(c for t, c in self.spent if t >= now - 600)
        return {"limit_usd_h": round(self.rate * 3600, 4), "rolling_usd_h": round(last / win * 3600, 4),
                "last10m_usd_h": round(ten / min(600.0, win) * 3600, 4), "window_s": round(win),
                "run_usd": round(self.total, 6), "calls": self.calls, "skipped": self.skipped,
                "est_call_usd": {k: round(v, 7) for k, v in self.est.items()}}


def jev_state(sym: str, f: dict, a: dict, pos: dict | None, acct: dict, feed: hf_feed.Feed, now: int,
              clip: float, n_candles: int = 12) -> dict:
    """The context Jev sees for ONE asset: its own book, flow, returns, 1 s candles, typical
    range, costs, and this asset's last trade / open position."""
    n_c = max(5, int(n_candles))
    bars = feed.candles_1s(max(21, n_c + 1))
    r1 = [round((bars[i]["c"] / bars[i - 1]["c"] - 1) * 1e4, 1) for i in range(1, len(bars)) if bars[i - 1]["c"]]
    mid = f["mid"]
    c20 = [[round((b[k] / mid - 1) * 1e4, 1) for k in ("o", "h", "l", "c")] for b in bars[-n_c:]] if mid else []
    eq = float(acct.get("equity") or hf_sim.START_EQUITY)
    bk = (acct.get("by_asset") or {}).get(sym) or {}
    st: dict[str, Any] = {
        "market": f"{sym} perpetual on Bitget (USDT-M), paper scalper, ${clip:.0f} clip",
        "asset": {"symbol": sym, "tick_bps": f.get("tick_bps"), "spread_bps": f["spread_bps"],
                  "typical_range_120s_bps": a.get("range_bps"), "cost_floor_bps": a.get("e_min"),
                  "depth_5bps_usd": {"bid": f.get("depth_bid_5bps_usd"), "ask": f.get("depth_ask_5bps_usd")},
                  "take_profit_bps_if_entered": a.get("tp_bps"), "stop_bps_if_entered": a.get("sl_bps")},
        "book": {k: f[k] for k in ("spread_bps", "imb_l1", "imb_l5", "imb_l15", "imb_5bps", "micro_bps")},
        "flow": {k: f[k] for k in ("flow_5s", "flow_15s", "flow_60s", "usd_15s", "usd_60s", "n_15s")},
        "returns_bps": {k[4:]: f[k] for k in ("ret_5s", "ret_15s", "ret_30s", "ret_60s", "ret_300s")},
        "vol": {k: f[k] for k in ("rv_1s_bps", "sigma_120s_bps", "range_60s_bps", "pos_in_range")},
        "last20_1s_returns_bps": r1[-20:],
        f"last{n_c}_1s_candles_ohlc_bps_vs_mid": c20,
        "perp": {k: f[k] for k in ("funding_bps", "funding_in_min", "basis_bps", "oi_chg_5m_pct", "ls_ratio")},
        "costs_bps": {"taker_side": 3, "maker_side": 1, "round_trip_take_profit": 4, "round_trip_stop": 6,
                      "spread": f["spread_bps"]},
        "paper": {"equity_change_pct": round((eq / hf_sim.START_EQUITY - 1) * 100, 3),
                  "round_trips_this_asset": bk.get("round_trips", 0),
                  "open_positions_total": len(acct.get("positions") or {})},
    }
    lc = bk.get("last_close")
    if lc:
        st["paper"]["last_trade_this_asset"] = {"side": lc["side"], "net_usd": round(lc["pnl"], 4), "exit": lc["kind"],
                                                "secs_ago": round((now - lc["ts_ms"]) / 1000)}
    if pos:
        st["position"] = {
            "side": "long" if pos["dir"] > 0 else "short", "entry": pos["entry"],
            "unrealized_bps": round((mid / pos["entry"] - 1) * 1e4 * pos["dir"], 2),
            "age_s": round((now - pos["opened_ms"]) / 1000, 1),
            "take_profit_bps": pos["tp_bps"], "stop_bps": pos["sl_bps"],
            "best_bps": round(pos["mfe_bps"], 2), "worst_bps": round(pos["mae_bps"], 2),
            "exit_order": pos["exit"]["kind"],
        }
    else:
        st["position"] = "flat"
    return st


class Engine:
    def __init__(self, jev=None) -> None:
        self.P = load_params()
        self.universe: list[str] = list(self.P["universe"])
        self.specs, self.specs_src = hf_feed.load_contracts(self.universe, CONTRACTS_CACHE)
        self.mf = hf_feed.MultiFeed(self.universe, self.specs, mid_keep_s=int(self.P["grid_span_s"]))
        self.book = hf_sim.PaperBook(path=ACCT_PATH, fills_log=FILLS_PATH, trips_log=TRIPS_PATH, touch=self.mf.touch, specs=self.specs,
                                     clip_usdt=self.P["clip_usdt"], max_open_total=self.P["max_open_total"], params={
                                         "tp_bps": self.P["tp_bps_min"], "sl_bps": self.P["sl_bps"],
                                         "time_stop_s": self.P["time_stop_s"],
                                         "passive_s": self.P["soft_exit_window_s"],
                                         "close_passive_s": self.P["soft_exit_window_s"],
                                         "exit_when_net_green": bool(self.P.get("exit_when_net_green")),
                                         "no_scratch_exits": bool(self.P.get("no_scratch_exits")),
                                         "hard_stop_usd": float(self.P.get("hard_stop_usd") or 0),
                                         "min_take_usd": float(self.P.get("min_take_usd") or 0),
                                         "be_trigger_bps": float(self.P.get("be_trigger_bps") or 0)})
        self.mf.trade_listeners.append(self._on_trade)
        self.mf.book_listeners.append(self._on_book)
        self.mock = os.environ.get("HF_MOCK_JEV") == "1"
        self._jev_override = jev
        self._tls = threading.local()
        self.pool = ThreadPoolExecutor(max_workers=int(self.P["jev_workers"]), thread_name_prefix="jev")
        self.budget = Budget(float(self.P["jev_budget_usd_h"]), float(self.P["jev_burst_s"]))
        self.gate = self._load_gate()
        self.big_prints: list[dict] = []
        self.stats = {"ticks": 0, "errors": 0, "jev_errors": 0, "overruns": 0, "cost_usd": 0.0, "lat_ms": [],
                      "tick_ms": [], "calls": 0}
        self.control, self.control_ok, self.control_err = hf_control.read(CONTROL_PATH, self.universe)
        self.last_good_control = self.control
        self.last_asked: dict[str, int] = {}
        self.assets: dict[str, dict] = {}
        self._pub_busy = False
        self._pub_last = 0.0
        self._pub_status: dict = {}
        self._mids_last = 0.0
        # v4.1 overlay (playbook/overlay.json, written by the overnight coach): re-read every tick,
        # applied per asset only while that asset is flat
        self.ov_file, self.ov_ok, self.ov_err = hf_overlay.load(OVERLAY_PATH, self.universe, self.P)
        self.ov_mtime: float | None = self._ov_mtime()
        self.ov_live: dict[str, dict] = {}
        self.ov_live_hash: dict[str, str] = {}
        self.ov_pending: list[str] = []
        pos0 = self.book.positions()
        for s in self.universe:
            src = None if s in pos0 else self.ov_file
            self.ov_live[s] = hf_overlay.asset_params(self.P, src, s)
            self.ov_live_hash[s] = hf_overlay.overlay_hash(src) if src else "params"

    # -------------------------------------------------------------- overlay (bounded coach knobs)
    @staticmethod
    def _ov_mtime() -> float | None:
        try:
            return OVERLAY_PATH.stat().st_mtime
        except OSError:
            return None

    def _refresh_overlay(self, positions: dict) -> None:
        """Re-read the overlay when its mtime changes; hand new values to each asset only when flat
        (an open trade keeps the TP / stop / time cap / close-now threshold it was opened with)."""
        mt = self._ov_mtime()
        if mt != self.ov_mtime:
            self.ov_mtime = mt
            ov, ok, err = hf_overlay.load(OVERLAY_PATH, self.universe, self.P)
            self.ov_ok, self.ov_err = ok, err
            if ok:
                self.ov_file = ov            # invalid file: keep the last good overlay
        h = hf_overlay.overlay_hash(self.ov_file)
        pending = []
        for s in self.universe:
            if self.ov_live_hash.get(s) == h:
                continue
            if s in positions:
                pending.append(s)
                continue
            self.ov_live[s] = hf_overlay.asset_params(self.P, self.ov_file, s)
            self.ov_live_hash[s] = h
        self.ov_pending = pending

    # -------------------------------------------------------------- jev clients (one per worker)
    def _client(self):
        if self._jev_override is not None:
            return self._jev_override
        c = getattr(self._tls, "c", None)
        if c is None:
            c = hf_jev.MockJev() if self.mock else hf_jev.JevClient(timeout_s=self.P["jev_timeout_s"])
            self._tls.c = c
        return c

    def _ask(self, sym: str, mode: str, state: dict, questions: dict) -> dict:
        t0 = time.time()
        try:
            raw = self._client().ask(state, questions)
            err = None
        except Exception as exc:
            raw, err = {}, str(exc)[:240]
        return {"symbol": sym, "mode": mode, "raw": raw, "err": err, "lat": int((time.time() - t0) * 1000)}

    # -------------------------------------------------------------- gate state (per asset)
    def _load_gate(self) -> dict:
        try:
            g = json.loads(GATE_STATE.read_text())
            if g.get("run_start_ms") == self.book.acct.get("run_start_ms") and g.get("version") == 4:
                for s in self.universe:
                    g["assets"].setdefault(s, {"exp_move_min": self.P["exp_move_min"], "entries": []})
                return g
        except (OSError, json.JSONDecodeError, KeyError, AttributeError):
            pass
        return {"version": 4, "adjusted_ms": int(time.time() * 1000), "run_start_ms": self.book.acct.get("run_start_ms"),
                "assets": {s: {"exp_move_min": self.P["exp_move_min"], "entries": []} for s in self.universe}}

    def _save_gate(self) -> None:
        GATE_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = GATE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.gate))
        tmp.replace(GATE_STATE)

    def _adapt(self, now: int) -> None:
        """v3.1 entry-rate adaptation, per asset: base E floor steps 0.5 bps between 4 and 9
        when that asset trades < 5/h or > 20/h over 60 min (checked every 20 min)."""
        A = self.P["adapt"]
        if not A.get("enabled"):
            return
        for g in self.gate["assets"].values():
            g["entries"] = [t for t in g["entries"] if t >= now - A["window_min"] * 60_000]
        run_age = now - int(self.book.acct.get("run_start_ms") or now)
        if now - self.gate["adjusted_ms"] < A["every_min"] * 60_000 or run_age < A["window_min"] * 60_000:
            return
        for s, g in self.gate["assets"].items():
            rate = len(g["entries"]) / (A["window_min"] / 60)
            old = g["exp_move_min"]
            if rate < A["low_rate_h"]:
                g["exp_move_min"] = max(A["floor_bps"], old - A["step_bps"])
            elif rate > A["high_rate_h"]:
                g["exp_move_min"] = min(A["cap_bps"], old + A["step_bps"])
            if g["exp_move_min"] != old:
                print(f"gate {s}: entries/h={rate:.1f} base {old}->{g['exp_move_min']}", flush=True)
        self.gate["adjusted_ms"] = now
        self._save_gate()

    # -------------------------------------------------------------- feed callbacks
    def _on_trade(self, sym: str, ts: int, px: float, sz: float, sd: int) -> None:
        self.book.on_trade(ts, px, sz, sd, sym=sym)
        usd = px * sz
        if usd >= self.P["big_print_usd"]:
            self.big_prints.append({"symbol": sym, "side": "bid" if sd > 0 else "ask", "usd": round(usd), "px": px, "ts": ts})
            del self.big_prints[:-12]

    def _on_book(self, sym: str) -> None:
        try:
            self.book.reprice(sym)
        except Exception:
            traceback.print_exc()

    def _clock_loop(self) -> None:
        while True:
            try:
                fund = {s: (fd.funding, fd.next_funding_ms) for s, fd in self.mf.feeds.items()}
                self.book.on_clock(funding=fund)
            except Exception:
                traceback.print_exc()
            time.sleep(0.2)

    # -------------------------------------------------------------- controls
    def read_control(self) -> dict[str, bool]:
        c, ok, err = hf_control.read(CONTROL_PATH, self.universe)
        self.control_ok, self.control_err = ok, err
        if ok:
            self.last_good_control = c
        self.control = self.last_good_control
        return dict(self.control["assets"])

    # -------------------------------------------------------------- per-asset view
    def asset_view(self, sym: str, now: int, enabled: bool, positions: dict) -> tuple[dict, dict]:
        fd = self.mf.feeds[sym]
        Ps = self.ov_live.get(sym) or self.P
        f = fd.features(now)
        rs = fd.range_stats(int(self.P["vol_horizon_s"]), int(self.P["vol_lookback_s"]),
                            int(self.P["vol_step_s"]), int(self.P["vol_min_samples_s"]), now)
        if f:
            f["range_h_bps"] = rs["range_bps"]
        status, reason = asset_status(f, rs, self.mf.ws_up, self.P)
        has_pos = sym in positions
        spread = f.get("spread_bps", 0.0) if f else None
        dmin = min(f.get("depth_bid_5bps_usd", 0), f.get("depth_ask_5bps_usd", 0)) if f else 0
        room = room_for(rs["range_bps"], spread or 0, dmin, self.P) if f else {"score": None, "depth_factor": None}
        bias = float(Ps.get("room_bias", 1.0))
        room_eff = round(room["score"] * bias, 3) if room["score"] is not None else None
        tp = tp_for(f or {}, Ps)
        base = self.gate["assets"][sym]["exp_move_min"]
        e_off = float(Ps.get("e_min_offset_bps", 0.0))
        a = {
            "symbol": sym, "enabled": enabled, "status": status, "reason": reason,
            "eff": effective_state(enabled, has_pos, status),
            "mid": f.get("mid") if f else None, "bid": f.get("bid") if f else None, "ask": f.get("ask") if f else None,
            "spread_bps": spread, "tick_bps": f.get("tick_bps") if f else None,
            "range_bps": rs["range_bps"], "samples_s": rs["samples_s"],
            "room": room_eff, "room_raw": room["score"], "room_bias": bias, "depth_factor": room["depth_factor"],
            "depth_bid_usd": f.get("depth_bid_5bps_usd") if f else None,
            "depth_ask_usd": f.get("depth_ask_5bps_usd") if f else None,
            "tp_bps": tp, "sl_bps": sl_for(tp, Ps), "time_cap_s": time_cap_for(f or {}, Ps),
            "e_base": base, "e_min": round(e_min_for(base, spread or 0, self.P) + e_off, 3), "e_min_offset": e_off,
            "close_thr": float(Ps.get("close_now_thr", self.P["close_now_thr"])),
            "flow_15s": f.get("flow_15s") if f else None, "imb_l5": f.get("imb_l5") if f else None,
            "micro": micro_score(f) if f else None,
            "ret_60s": f.get("ret_60s") if f else None, "ret_300s": f.get("ret_300s") if f else None,
            "last_trade_age_s": f.get("last_trade_age_s") if f else None,
            "book_age_ms": f.get("book_age_ms") if f else None,
            "funding_rate": fd.funding, "ls_ratio": fd.ls_ratio if fd.ls_ok else None,
            "chg_24h": fd.chg_24h, "n_60s": f.get("n_60s") if f else None,
            "in_position": has_pos, "asked": None,
        }
        return a, f

    # -------------------------------------------------------------- publish (existing gist sidecar)
    def _publish(self, record: dict) -> None:
        if not PUBLISH or self._pub_busy or time.time() - self._pub_last < self.P["publish_every_s"]:
            return
        self._pub_busy, self._pub_last = True, time.time()

        def run() -> None:
            try:
                import stance_publish
                res = stance_publish.publish(record)
                self._pub_status = {"ok": bool(res.get("ok")), "skipped": res.get("skipped"),
                                    "error": res.get("error"), "ts_ms": int(time.time() * 1000)}
            except Exception as exc:
                self._pub_status = {"ok": False, "error": type(exc).__name__}
            finally:
                self._pub_busy = False

        threading.Thread(target=run, daemon=True).start()

    # -------------------------------------------------------------- one decision tick
    def tick(self) -> None:
        t_start = time.time()
        now = int(t_start * 1000)
        P = self.P
        enabled = self.read_control()
        positions = self.book.positions()
        self._refresh_overlay(positions)
        assets: dict[str, dict] = {}
        feats: dict[str, dict] = {}
        for s in self.universe:
            assets[s], feats[s] = self.asset_view(s, now, enabled.get(s, True), positions)
        acct = self.book.snapshot()
        # ---- who gets a Jev call this tick: close-now for every open position (least recently
        #      asked first), then the top-N flat candidates by room; each only if the budget allows.
        cands = pick_candidates(assets, positions, P)
        pos_every_ms = float(P.get("jev_pos_every_s", P["cadence_s"])) * 1000 - 400
        if P.get("no_scratch_exits"):
            queue = []
        else:
            queue = [(s, "in_position") for s in
                     sorted(positions, key=lambda s: self.last_asked.get(s, 0))
                     if feats.get(s) and now - self.last_asked.get(s, 0) >= pos_every_ms]
        queue += [(s, "flat") for s in cands]
        jobs, skipped = [], []
        for s, mode in queue:
            if self.budget.take(mode):
                jobs.append((s, mode))
            else:
                skipped.append((s, mode))
        for s in cands:
            assets[s]["asked"] = "flat" if (s, "flat") in jobs else "budget_skip"
        for s in positions:
            if s in assets:
                assets[s]["asked"] = ("close_now" if (s, "in_position") in jobs else
                                      "budget_skip" if (s, "in_position") in skipped else "close_now_wait")
        futs = []
        for s, mode in jobs:
            pos = positions.get(s) if mode == "in_position" else None
            st = jev_state(s, feats[s], assets[s], pos, acct, self.mf.feeds[s], now, P["clip_usdt"],
                           int(P.get("jev_candles_n", 12)))
            q = hf_jev.pos_questions(s) if mode == "in_position" else hf_jev.flat_questions(s)
            futs.append(self.pool.submit(self._ask, s, mode, st, q))
            self.last_asked[s] = now
        results = [fu.result() for fu in futs]
        decisions = []
        tick_cost = 0.0
        for r in results:
            s, mode = r["symbol"], r["mode"]
            usage = r["raw"].get("usage") or {}
            cost = float(usage.get("cost") or 0)
            self.budget.settle(mode, cost)
            tick_cost += cost
            self.stats["cost_usd"] += cost
            self.stats["calls"] += 1
            decisions.append(self._decide(s, mode, r, feats[s], assets[s], now, cost, usage))
        for s, mode in skipped:
            decisions.append({"ts_ms": now, "symbol": s, "mode_q": mode, "jev": {}, "answers": {},
                              "writer_action": {"action": "HOLD", "mode": mode, "gate_block": "jev_budget_skip"},
                              "latency_ms": None, "cost": 0.0, "error": None})
        self._adapt(now)
        self.stats["ticks"] += 1
        lats = [d["latency_ms"] for d in decisions if d.get("latency_ms") is not None]
        if lats:
            self.stats["lat_ms"] = (self.stats["lat_ms"] + lats)[-400:]
        # refresh effective states after entries / exits this tick
        pos_after = self.book.positions()
        enabled = dict(self.control["assets"])
        for s, a in assets.items():
            a["in_position"] = s in pos_after
            a["eff"] = effective_state(enabled.get(s, True), s in pos_after, a["status"])
            a["enabled"] = enabled.get(s, True)
        self.assets = assets
        self.stats["tick_ms"] = (self.stats["tick_ms"] + [int((time.time() - t_start) * 1000)])[-200:]
        self.record(now, assets, feats, decisions, max(lats) if lats else None, tick_cost)

    def _decide(self, s: str, mode: str, r: dict, f: dict, a: dict, now: int, cost: float, usage: dict) -> dict:
        P = self.P
        answers = r["raw"].get("answers") or {}
        err = r["err"]
        wa: dict[str, Any] = {"action": "HOLD", "mode": mode}
        jev: dict[str, Any] = {}
        if err:
            self.stats["jev_errors"] += 1
            wa.update(gate_block="jev_error", error=err)
        elif mode == "flat":
            jev = hf_jev.parse_flat(answers)
            lc = ((self.book.acct.get("by_asset") or {}).get(s) or {}).get("last_close")
            ctx = {"exp_move_min": a["e_min"], "last_close": lc, "now_ms": now, "room": a["room"]}
            d, reason, det = gate_entry(f, jev, ctx, P)
            wa.update(det)
            wa["gate_block"] = reason
            wa["jev_action"] = jev["choice"]
            if d:
                # the toggle is re-read right before the fill so a click during the Jev call counts
                if not self.read_control().get(s, True):
                    wa["gate_block"] = "asset_off"
                else:
                    tp, sl, cap = a["tp_bps"], a["sl_bps"], a["time_cap_s"]
                    fill = self.book.open(d, sym=s, signal={
                        "p_dir": det["p_dir"], "exp_move": jev["exp_move_bps"], "e_min": a["e_min"],
                        "micro": det["micro"], "tp_bps": tp, "sl_bps": sl, "time_cap_s": cap,
                        "range_bps": a["range_bps"], "room": a["room"], "room_raw": a.get("room_raw"),
                        "room_bias": a.get("room_bias"), "e_min_offset": a.get("e_min_offset"),
                        "close_thr": a.get("close_thr"), "overlay": self.ov_live_hash.get(s),
                        "spread_bps": a["spread_bps"], "sigma_120s": f.get("sigma_120s_bps")},
                        tp_bps=tp, sl_bps=sl, time_stop_s=cap)
                    if fill:
                        wa.update(action="BUY" if d > 0 else "SELL", fill_px=fill["px"], qty=fill["qty"],
                                  signal_mid=f["mid"], tp_bps=tp, sl_bps=sl, time_cap_s=cap)
                        self.gate["assets"][s]["entries"].append(now)
                        self._save_gate()
                    else:
                        wa["gate_block"] = "refused_" + (self.book.last_refusal or "unknown")
        else:
            jev = hf_jev.parse_pos(answers)
            cur = self.book.position(s)
            # the threshold this trade was opened with (overlay changes never touch an open trade)
            thr = float(((cur or {}).get("signal") or {}).get("close_thr") or P["close_now_thr"])
            wa["close_thr"] = thr
            wa["jev_action"] = "CLOSE" if jev["close_now"] >= thr else "HOLD"
            age = (now - cur["opened_ms"]) / 1000 if cur else 0
            if cur is None:
                wa["gate_block"] = "closed_during_call"
            elif wa["jev_action"] == "CLOSE" and age >= P["close_min_age_s"]:
                res = self.book.request_close("jev_close", sym=s)
                wa["action"] = "CLOSE" if res == "passive_posted" else "HOLD"
                wa["gate_block"] = f"jev_close_{res}"
            elif wa["jev_action"] == "CLOSE":
                wa["gate_block"] = "close_too_young"
            else:
                wa["gate_block"] = "holding_tp"
        return {"ts_ms": now, "symbol": s, "mode_q": mode, "jev": jev, "answers": answers, "writer_action": wa,
                "latency_ms": r["lat"], "cost": cost, "usage": usage or None, "error": err,
                "model": r["raw"].get("model") or hf_jev.MODEL,
                "room": a["room"], "range_bps": a["range_bps"], "e_min": a["e_min"], "spread_bps": a["spread_bps"],
                "tp_bps": a["tp_bps"], "sl_bps": a["sl_bps"]}

    @staticmethod
    def _stance(d: dict, pos: dict | None) -> dict:
        jev = d.get("jev") or {}
        if d["mode_q"] == "flat" and jev:
            lab = {"LONG": "Long", "SHORT": "Short"}.get(jev.get("choice"), "Flat")
            pct = {"Long": jev["p_long"], "Short": jev["p_short"], "Flat": jev["p_none"]}[lab]
            return {"symbol": d["symbol"], "label": lab, "pct": round(pct * 100), "hold": jev["p_none"],
                    "buy": jev["p_long"], "sell": jev["p_short"], "exp_move_bps": jev["exp_move_bps"]}
        if jev:
            side = "Long" if (pos or {}).get("dir", 1) > 0 else "Short"
            keep = 1 - jev["close_now"]
            return {"symbol": d["symbol"], "label": side, "pct": round(keep * 100), "hold": keep,
                    "close_now": jev["close_now"], "buy": keep if side == "Long" else 0, "sell": keep if side == "Short" else 0}
        return {"symbol": d.get("symbol"), "label": "Flat", "pct": 0, "hold": 0, "buy": 0, "sell": 0,
                "error": bool(d.get("error")), "skipped": d.get("writer_action", {}).get("gate_block") == "jev_budget_skip"}

    def record(self, now: int, assets: dict, feats: dict, decisions: list, lat: int | None, tick_cost: float) -> None:
        acct = self.book.snapshot()
        self.book.save()
        fills = self.book.drain_events()
        pos = acct.get("positions") or {}
        for d in decisions:
            d["stance"] = self._stance(d, pos.get(d["symbol"]))
        # primary decision (tool compatibility: top-level jev / writer_action / mode_q like v3.1):
        # an action if there was one, else the top flat candidate, else the first call
        real = [d for d in decisions if d.get("latency_ms") is not None]
        prim = next((d for d in real if d["writer_action"].get("action") != "HOLD"), None) \
            or next((d for d in real if d["mode_q"] == "flat"), None) or (real[0] if real else None)
        lats = self.stats["lat_ms"]
        bh = self.budget.per_hour()
        ps = prim["symbol"] if prim else None
        pf = feats.get(ps) or {}
        rec = {
            "ts_ms": now,
            "symbol": ps or "MULTI",
            "symbols_asked": [d["symbol"] for d in real],
            "strategy": STRATEGY_TAG,
            "strategy_version": hf_sim.STRATEGY_VERSION,
            "cadence_s": self.P["cadence_s"],
            "universe": self.universe,
            "state": {"symbol": ps, "ts_ms": now, "mark": pf.get("mid"), "bid": pf.get("bid"), "ask": pf.get("ask"),
                      "features": pf or None, "source": "bitget_public_ws"} if prim else {"symbol": None, "ts_ms": now},
            "mode_q": prim["mode_q"] if prim else None,
            "answers": prim["answers"] if prim else {},
            "jev": prim["jev"] if prim else {},
            "writer_action": prim["writer_action"] if prim else {"action": "HOLD", "gate_block": "no_candidates"},
            "stance": prim["stance"] if prim else {"label": "Flat", "pct": 0, "hold": 0, "buy": 0, "sell": 0},
            "decisions": [{k: d.get(k) for k in ("symbol", "mode_q", "jev", "writer_action", "stance", "latency_ms",
                                                 "cost", "error", "room", "range_bps", "e_min", "tp_bps", "sl_bps")}
                          for d in decisions],
            "assets": assets,
            "control": {"file_ok": self.control_ok, "error": self.control_err or None,
                        "seq": self.control.get("seq"), "updated_ms": self.control.get("updated_ms"),
                        "enabled": dict(self.control["assets"])},
            "account": {
                **{k: acct.get(k) for k in (
                    "equity", "realized", "gross_realized", "upnl", "side", "open_count", "streak", "wins",
                    "losses", "last_settlement", "fills", "round_trips", "fees_paid", "fees_full", "fees_rebate",
                    "fees_by_liq", "exits_by_kind", "soft_exits", "funding_paid", "volume_usdt", "gate_block",
                    "last_action", "run_start_ms", "strategy", "last_close")},
                "positions": {s: {k: p.get(k) for k in ("symbol", "dir", "qty", "entry", "tp_px", "sl_px", "tp_bps", "sl_bps",
                                                        "opened_ms", "time_cap_s", "deadline_ms", "exit", "upnl", "move_bps",
                                                        "mark", "notional")} for s, p in pos.items()},
                "by_asset": {s: {k: b.get(k) for k in ("realized", "gross_realized", "upnl", "fees_paid", "volume_usdt",
                                                       "fills", "round_trips", "wins", "losses", "exits_by_kind")}
                             for s, b in (acct.get("by_asset") or {}).items()},
                "history": (acct.get("history") or [])[-HIST_IN_TICK:],
            },
            "fills": fills,
            "gate": {"per_asset": {s: {"exp_move_min": g["exp_move_min"], "entries_1h": len(g["entries"])}
                                   for s, g in self.gate["assets"].items()}},
            "tape": {"events": self.big_prints[-6:], "ws": self.mf.ws_up, "ws_connects": self.mf.connects,
                     "feed": self.mf.status()},
            "budget": bh,
            "overlay": {"file_ok": self.ov_ok, "error": self.ov_err or None, "hash": hf_overlay.overlay_hash(self.ov_file),
                        "source": self.ov_file.get("source"), "run_id": self.ov_file.get("run_id"),
                        "updated_ms": self.ov_file.get("updated_ms"), "pending_until_flat": self.ov_pending,
                        "live": {s: {"room_bias": v.get("room_bias"), "tp_k": v.get("tp_range_k"), "sl_mult": v.get("sl_mult"),
                                     "tcap": [v.get("time_cap_min_s"), v.get("time_cap_max_s")],
                                     "e_off": v.get("e_min_offset_bps"), "close_thr": v.get("close_now_thr")}
                                 for s, v in self.ov_live.items()}},
            "latency_ms": lat,
            "tick_cost_usd": round(tick_cost, 7),
            "model": (prim or {}).get("model") or hf_jev.MODEL,
            "usage": (prim or {}).get("usage"),
            "error": next((d["error"] for d in decisions if d.get("error")), None),
            "engine": {"ticks": self.stats["ticks"], "errors": self.stats["errors"], "jev_errors": self.stats["jev_errors"],
                       "overruns": self.stats["overruns"], "cost_usd": round(self.stats["cost_usd"], 6),
                       "calls": self.stats["calls"],
                       "lat_p50": sorted(lats)[len(lats) // 2] if lats else None,
                       "lat_p90": sorted(lats)[int(len(lats) * 0.9)] if lats else None,
                       "tick_ms_p50": sorted(self.stats["tick_ms"])[len(self.stats["tick_ms"]) // 2] if self.stats["tick_ms"] else None,
                       "specs_src": self.specs_src, "mock_jev": self.mock},
            "gist": self._pub_status or None,
            "mode": "paper_offline",
            "live_orders": False,
        }
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
        real_or_skip = [d for d in decisions]
        if real_or_skip:
            with DECISIONS_LOG.open("a", encoding="utf-8") as fh:
                for d in real_or_skip:
                    fh.write(json.dumps({k: v for k, v in d.items()}, default=str, separators=(",", ":")) + "\n")
        # tape sidecar (dash compatibility) for the primary / first live symbol
        ts_sym = ps or next((s for s, a in assets.items() if a["mid"]), self.universe[0])
        fd = self.mf.feeds[ts_sym]
        tf = feats.get(ts_sym) or {}
        tape = {"symbol": ts_sym, "ts_ms": now, "fetch_ms": 0, "mark": tf.get("mid"),
                "funding_rate": fd.funding, "bid": tf.get("bid"), "ask": tf.get("ask"),
                "candles": fd.candles_1s(120), "granularity": "1s", "book": fd.top(10),
                "extra": {"events": self.big_prints[-6:], "source": "bitget_public_ws"}}
        tmp = TAPE.with_suffix(".tmp")
        tmp.write_text(json.dumps(tape, default=str), encoding="utf-8")
        tmp.replace(TAPE)
        if time.time() - self._mids_last >= self.P["grid_step_s"]:
            self._mids_last = time.time()
            m = {"ts_ms": now, "step_s": self.P["grid_step_s"], "span_s": self.P["grid_span_s"], "source": "engine_book_mids",
                 "mids": {s: fdd.mids_every(int(self.P["grid_step_s"]), int(self.P["grid_span_s"])) for s, fdd in self.mf.feeds.items()}}
            tmp = MIDS_LIVE.with_suffix(".tmp")
            tmp.write_text(json.dumps(m, separators=(",", ":")), encoding="utf-8")
            tmp.replace(MIDS_LIVE)
        self._publish(rec)
        parts = []
        for d in decisions:
            j, w = d.get("jev") or {}, d["writer_action"]
            if d["mode_q"] == "flat" and j:
                parts.append(f"{d['symbol'][:-4]}:{w.get('action')}/{w.get('gate_block')} E={j['exp_move_bps']:.1f}/{d['e_min']:.1f} p={max(j['p_long'], j['p_short']):.2f}")
            elif j:
                parts.append(f"{d['symbol'][:-4]}:pos close={j['close_now']:.2f} {w.get('gate_block')}")
            else:
                parts.append(f"{d['symbol'][:-4]}:{w.get('gate_block')}")
        fill_txt = " ".join(f"[{x['symbol'][:-4]} {x['side']} {x['liq']} {x['reason']} @{x['px']}]" for x in fills)
        errs = [d["error"] for d in decisions if d.get("error")]
        live = sum(1 for a in assets.values() if a["status"] == "live")
        print(f"{' | '.join(parts) or 'no calls'} lat={lat}ms live={live}/{len(assets)} open={len(pos)} "
              f"eq={acct['equity']:.2f} rt={acct['round_trips']} $/h={bh['rolling_usd_h']:.3f} {fill_txt}"
              + (f" ERR {errs[0][:120]}" if errs else ""), flush=True)

    def _on_term(self, signum, frame) -> None:
        """Clean stop (supervisor/rollback send SIGTERM): close every open paper position as a
        taker at the touch so nothing is left dangling, save, exit."""
        try:
            recs = self.book.force_close_all("shutdown_taker")
            self.book.save()
            print(f"v4 engine stop (signal {signum}): "
                  + ("; ".join(f"closed {r['symbol']} {r['side']} at {r['exit']} pnl={r['pnl']:+.4f}" for r in recs)
                     if recs else "flat, nothing to close"), flush=True)
        finally:
            os._exit(0)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_term)
        hf_jev.load_dotenv(ROOT)
        self.mf.start()
        threading.Thread(target=self._clock_loop, daemon=True, name="hf-clock").start()
        t_wait = time.time()
        while not any(fd.ready() for fd in self.mf.feeds.values()) and time.time() - t_wait < 30:
            time.sleep(0.25)
        print(f"v4 multi-asset HF engine up: {len(self.universe)} assets {','.join(s[:-4] for s in self.universe)} "
              f"cadence={self.P['cadence_s']}s ws={self.mf.ws_up} specs={self.specs_src} "
              f"equity={self.book.acct['equity']:.2f} fills={self.book.acct['fills']} "
              f"topN={self.P['jev_top_n']} budget=${self.P['jev_budget_usd_h']}/h max_open={self.P['max_open_total']} "
              f"tp={self.P['tp_bps_min']}-{self.P['tp_bps_max']}bps stop={self.P['sl_mult']}xTP[{self.P['sl_bps_min']},{self.P['sl_bps_max']}] "
              f"quiet<{self.P.get('quiet_max_bps') or 0:g}bps take${self.P.get('min_take_usd') or 0:g} hard${self.P.get('hard_stop_usd') or 0:g} "
              f"cap={self.P['time_cap_min_s']}-{self.P['time_cap_max_s']}s soft={self.P['soft_exit_window_s']}s "
              f"{'MOCK JEV ' if self.mock else ''}(paper only)", flush=True)
        cad = float(self.P["cadence_s"])
        nxt = time.time()
        while True:
            try:
                self.tick()
            except Exception:
                self.stats["errors"] += 1
                print("tick error:", traceback.format_exc(limit=4).replace("\n", " | "), flush=True)
            nxt += cad
            now = time.time()
            if nxt < now:  # the calls overran the slot: skip ahead, never stack calls
                self.stats["overruns"] += 1
                nxt = now + 0.05
            time.sleep(max(0.0, nxt - now))


if __name__ == "__main__":
    Engine().run()
