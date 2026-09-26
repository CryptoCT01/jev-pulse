#!/usr/bin/env python3
"""Jev Pulse v3 HF paper engine: one persistent process.

  Bitget public WS (books15 + trades + ticker) -> features every 2.5 s
  -> TypeSafe Jev (flat: direction / expected move / risk; in trade: close-now / risk)
  -> local gate (cost, flow agreement, cooldown, risk veto) -> honest paper fills.
Exits (maker take-profit, taker stop, time stop) run on every public trade and a
200 ms clock, independent of the model cadence. PAPER ONLY: this file has no
exchange order code and never sends orders to Bitget.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hf_feed  # noqa: E402
import hf_jev  # noqa: E402
import hf_sim  # noqa: E402

import os  # noqa: E402

# HF_OUT_DIR redirects every output (ticks, tape, account, gate state) for shadow runs.
OUT = Path(os.environ["HF_OUT_DIR"]) if os.environ.get("HF_OUT_DIR") else None
LOG = (OUT or ROOT / "logs") / "paper_ticks.jsonl"
TAPE = (OUT or ROOT / "logs") / "tape_latest.json"
PARAMS_FILE = Path(os.environ.get("HF_PARAMS") or ROOT / "scripts" / "hf_params.json")
GATE_STATE = (OUT or ROOT / ".state") / "hf_gate.json"
ACCT_PATH = (OUT or ROOT / ".state") / "paper_account.json"
FILLS_PATH = (OUT or ROOT / "logs") / "paper_fills.jsonl"
PUBLISH = OUT is None  # shadow runs never touch the gist
HIST_IN_TICK = 6


def load_params() -> dict:
    return json.loads(PARAMS_FILE.read_text(encoding="utf-8"))


def micro_score(f: dict) -> float:
    """Deterministic flow/book agreement score in [-1, 1]."""
    # book imbalance leads (best-documented short-horizon predictor); trade flow is a lighter tie-break
    return round(0.45 * f.get("imb_5bps", 0) + 0.30 * f.get("imb_l1", 0)
                 + 0.15 * f.get("flow_5s", 0) + 0.10 * f.get("flow_15s", 0), 3)


def tp_for(f: dict, P: dict) -> float:
    return round(min(P["tp_bps_max"], max(P["tp_bps_min"], P["tp_sigma_k"] * f.get("sigma_120s_bps", 0))), 1)


def gate_entry(f: dict, j: dict, ctx: dict, P: dict) -> tuple[int, str, dict]:
    """Return (direction, reason, detail). direction 0 = no trade. Pure function."""
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
    # Jev answers a forced UP/DOWN choice; its probability of the chosen side is the lean.
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
    return d, "enter", det


def jev_state(f: dict, pos: dict | None, acct: dict, feed: hf_feed.Feed, now: int) -> dict:
    bars = feed.candles_1s(31)
    r1 = [round((bars[i]["c"] / bars[i - 1]["c"] - 1) * 1e4, 1) for i in range(1, len(bars)) if bars[i - 1]["c"]]
    eq = float(acct.get("equity") or hf_sim.START_EQUITY)
    st: dict[str, Any] = {
        "market": "BTCUSDT perpetual on Bitget, paper scalper, $200 clip",
        "book": {k: f[k] for k in ("spread_bps", "imb_l1", "imb_l5", "imb_l15", "imb_5bps", "micro_bps")},
        "flow": {k: f[k] for k in ("flow_5s", "flow_15s", "flow_60s", "usd_15s", "usd_60s", "n_15s")},
        "returns_bps": {k[4:]: f[k] for k in ("ret_5s", "ret_15s", "ret_30s", "ret_60s", "ret_300s")},
        "vol": {k: f[k] for k in ("rv_1s_bps", "sigma_120s_bps", "range_60s_bps", "pos_in_range")},
        "last30_1s_returns_bps": r1[-30:],
        "perp": {k: f[k] for k in ("funding_bps", "funding_in_min", "basis_bps", "oi_chg_5m_pct", "ls_ratio")},
        "costs_bps": {"taker_side": 3, "maker_side": 1, "round_trip_take_profit": 4, "round_trip_stop": 6},
        "paper": {"equity_change_pct": round((eq / hf_sim.START_EQUITY - 1) * 100, 3),
                  "round_trips": acct.get("round_trips", 0)},
    }
    lc = acct.get("last_close")
    if lc:
        st["paper"]["last_trade"] = {"side": lc["side"], "net_usd": round(lc["pnl"], 4), "exit": lc["kind"],
                                     "secs_ago": round((now - lc["ts_ms"]) / 1000)}
    if pos:
        mid = f["mid"]
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
    def __init__(self) -> None:
        self.P = load_params()
        self.feed = hf_feed.Feed()
        self.book = hf_sim.PaperBook(path=ACCT_PATH, fills_log=FILLS_PATH, touch=self.feed.touch, params={
            "tp_bps": self.P["tp_bps_min"], "sl_bps": self.P["sl_bps"],
            "time_stop_s": self.P["time_stop_s"], "passive_s": self.P["passive_s"],
            "close_passive_s": self.P["close_passive_s"]})
        self.feed.trade_listeners.append(self._on_trade)
        self.jev = hf_jev.MockJev() if os.environ.get("HF_MOCK_JEV") == "1" else hf_jev.JevClient(timeout_s=self.P["jev_timeout_s"])
        self.gate = self._load_gate()
        self.big_prints: list[dict] = []
        self.stats = {"ticks": 0, "errors": 0, "overruns": 0, "cost_usd": 0.0, "lat_ms": []}
        self._pub_busy = False
        self._pub_last = 0.0
        self._pub_status: dict = {}

    def _load_gate(self) -> dict:
        try:
            g = json.loads(GATE_STATE.read_text())
            if g.get("run_start_ms") == self.book.acct.get("run_start_ms"):
                return g
        except (OSError, json.JSONDecodeError):
            pass
        return {"exp_move_min": self.P["exp_move_min"], "adjusted_ms": int(time.time() * 1000),
                "run_start_ms": self.book.acct.get("run_start_ms"), "entries": []}

    def _save_gate(self) -> None:
        GATE_STATE.parent.mkdir(parents=True, exist_ok=True)
        GATE_STATE.write_text(json.dumps(self.gate))

    def _on_trade(self, ts: int, px: float, sz: float, sd: int) -> None:
        self.book.on_trade(ts, px, sz, sd)
        usd = px * sz
        if usd >= 80_000:
            self.big_prints.append({"side": "bid" if sd > 0 else "ask", "usd": round(usd), "px": px, "ts": ts})
            del self.big_prints[:-12]

    def _clock_loop(self) -> None:
        while True:
            try:
                self.book.on_clock(self.feed.funding, self.feed.next_funding_ms)
            except Exception:
                traceback.print_exc()
            time.sleep(0.2)

    def _adapt(self, now: int) -> None:
        A = self.P["adapt"]
        if not A.get("enabled"):
            return
        ents = [t for t in self.gate["entries"] if t >= now - A["window_min"] * 60_000]
        self.gate["entries"] = ents
        run_age = now - int(self.book.acct.get("run_start_ms") or now)
        if now - self.gate["adjusted_ms"] < A["every_min"] * 60_000 or run_age < A["window_min"] * 60_000:
            return
        rate = len(ents) / (A["window_min"] / 60)
        old = self.gate["exp_move_min"]
        if rate < A["low_rate_h"]:
            self.gate["exp_move_min"] = max(A["floor_bps"], old - A["step_bps"])
        elif rate > A["high_rate_h"]:
            self.gate["exp_move_min"] = min(A["cap_bps"], old + A["step_bps"])
        self.gate["adjusted_ms"] = now
        if self.gate["exp_move_min"] != old:
            print(f"gate: entries/h={rate:.1f} exp_move_min {old}->{self.gate['exp_move_min']}", flush=True)
        self._save_gate()

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

    def tick(self) -> None:
        now = int(time.time() * 1000)
        f = self.feed.features(now)
        if not f or not self.feed.ready(now):
            print("feed not ready: waiting for books15", flush=True)
            return
        pos = self.book.position()
        acct = self.book.snapshot()
        state = jev_state(f, pos, acct, self.feed, now)
        questions = hf_jev.POS_Q if pos else hf_jev.FLAT_Q
        mode = "in_position" if pos else "flat"
        t0 = time.time()
        err = None
        raw: dict = {}
        try:
            raw = self.jev.ask(state, questions)
        except Exception as exc:
            err = str(exc)[:240]
        lat = int((time.time() - t0) * 1000)
        answers = raw.get("answers") or {}
        usage = raw.get("usage") or {}
        self.stats["cost_usd"] += float(usage.get("cost") or 0)
        wa: dict[str, Any] = {"action": "HOLD", "mode": mode}
        jev: dict[str, Any] = {}
        if err:
            self.stats["errors"] += 1
            wa.update(action="HOLD", gate_block="jev_error", error=err)
        elif mode == "flat":
            jev = hf_jev.parse_flat(answers)
            ctx = {"exp_move_min": self.gate["exp_move_min"], "last_close": acct.get("last_close"), "now_ms": now}
            d, reason, det = gate_entry(f, jev, ctx, self.P)
            wa.update(det)
            wa["gate_block"] = reason
            wa["jev_action"] = jev["choice"]
            if d and self.book.position() is None:
                tp = tp_for(f, self.P)
                fill = self.book.open(d, signal={"p_dir": det["p_dir"], "exp_move": jev["exp_move_bps"],
                                                 "micro": det["micro"], "tp_bps": tp,
                                                 "sigma_120s": f["sigma_120s_bps"]},
                                      tp_bps=tp, sl_bps=self.P["sl_bps"])
                if fill:
                    wa["action"] = "BUY" if d > 0 else "SELL"
                    wa["fill_px"] = fill["px"]
                    wa["signal_mid"] = f["mid"]
                    wa["tp_bps"] = tp
                    self.gate["entries"].append(now)
                    self._save_gate()
        else:
            jev = hf_jev.parse_pos(answers)
            wa["jev_action"] = "CLOSE" if jev["close_now"] >= self.P["close_now_thr"] else "HOLD"
            cur = self.book.position()
            age = (now - cur["opened_ms"]) / 1000 if cur else 0
            if cur is None:
                wa["gate_block"] = "closed_during_call"
            elif wa["jev_action"] == "CLOSE" and age >= self.P["close_min_age_s"]:
                res = self.book.request_close("jev_close")
                wa["action"] = "CLOSE" if res == "passive_posted" else "HOLD"
                wa["gate_block"] = f"jev_close_{res}"
            elif wa["jev_action"] == "CLOSE":
                wa["gate_block"] = "close_too_young"
            else:
                wa["gate_block"] = "holding_tp"
        self._adapt(now)
        self.stats["ticks"] += 1
        self.stats["lat_ms"] = (self.stats["lat_ms"] + [lat])[-200:]
        self.record(now, f, state, answers, jev, wa, lat, raw, usage, mode, err)

    def record(self, now, f, state, answers, jev, wa, lat, raw, usage, mode, err) -> None:
        acct = self.book.snapshot()
        self.book.save()  # keep equity/uPnL in paper_account.json fresh for the desk
        fills = self.book.drain_events()
        pos = acct.get("pos")
        if mode == "flat" and jev:
            lab = {"LONG": "Long", "SHORT": "Short"}.get(jev.get("choice"), "Flat")
            pct = {"Long": jev["p_long"], "Short": jev["p_short"], "Flat": jev["p_none"]}[lab]
            stance = {"label": lab, "pct": round(pct * 100), "hold": jev["p_none"], "buy": jev["p_long"],
                      "sell": jev["p_short"], "exp_move_bps": jev["exp_move_bps"]}
        elif jev:
            side = "Long" if (pos or {}).get("dir", 1) > 0 else "Short"
            keep = 1 - jev["close_now"]
            stance = {"label": side, "pct": round(keep * 100), "hold": keep, "close_now": jev["close_now"],
                      "buy": keep if side == "Long" else 0, "sell": keep if side == "Short" else 0}
        else:
            stance = {"label": "Flat", "pct": 0, "hold": 0, "buy": 0, "sell": 0, "error": True}
        hist = acct.get("history") or []
        lats = self.stats["lat_ms"]
        rec = {
            "ts_ms": now,
            "symbol": "BTCUSDT",
            "strategy": "v3-hf",
            "cadence_s": self.P["cadence_s"],
            "state": {
                "symbol": "BTCUSDT", "ts_ms": now, "mark": f["mid"], "bid": f["bid"], "ask": f["ask"],
                "funding_rate": f["funding_bps"] / 1e4,
                "ret_1m": f["ret_60s"] / 1e4, "ret_5m": f["ret_300s"] / 1e4,
                "vol_short": f["rv_1s_bps"] * (60 ** 0.5) / 1e4, "range_pos": f["pos_in_range"],
                "position": state.get("position"),
                "extra": {"book_imb": f["imb_l5"], "spread_bps": f["spread_bps"],
                          "taker_buy_ratio": round((f["flow_60s"] + 1) / 2, 4), "ls_ratio": f["ls_ratio"],
                          "oi": self.feed.oi},
                "features": f,
                "source": "bitget_public_ws",
            },
            "mode_q": mode,
            "answers": answers,
            "jev": jev,
            "writer_action": wa,
            "stance": stance,
            "account": {
                **{k: acct.get(k) for k in (
                    "equity", "realized", "gross_realized", "upnl", "side", "qty", "entry", "streak", "wins",
                    "losses", "last_settlement", "fills", "round_trips", "fees_paid", "fees_full", "fees_rebate",
                    "fees_by_liq", "exits_by_kind", "funding_paid", "volume_usdt", "gate_block", "move_bps",
                    "last_action", "run_start_ms")},
                "pos": {k: pos[k] for k in ("dir", "entry", "tp_px", "sl_px", "tp_bps", "sl_bps", "opened_ms",
                                            "deadline_ms", "exit")} if pos else None,
                "history": hist[-HIST_IN_TICK:],
            },
            "fills": fills,
            "gate": {"exp_move_min": self.gate["exp_move_min"], "entries_1h": len(self.gate["entries"])},
            "tape": {"events": self.big_prints[-6:], "book_imb": f["imb_l5"], "ls_ratio": f["ls_ratio"],
                     "fetch_ms": 0, "ws": self.feed.ws_up, "ws_connects": self.feed.connects},
            "latency_ms": lat,
            "model": raw.get("model") or hf_jev.MODEL,
            "usage": usage or None,
            "error": err,
            "engine": {"ticks": self.stats["ticks"], "errors": self.stats["errors"],
                       "overruns": self.stats["overruns"], "cost_usd": round(self.stats["cost_usd"], 6),
                       "lat_p50": sorted(lats)[len(lats) // 2] if lats else None},
            "gist": self._pub_status or None,
            "mode": "paper_offline",
            "live_orders": False,
        }
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
        tape = {"symbol": "BTCUSDT", "ts_ms": now, "fetch_ms": 0, "mark": f["mid"],
                "funding_rate": f["funding_bps"] / 1e4, "bid": f["bid"], "ask": f["ask"],
                "candles": self.feed.candles_1s(120), "granularity": "1s", "book": self.feed.top(10),
                "extra": {**rec["state"]["extra"], "events": self.big_prints[-6:], "large": None,
                          "source": "bitget_public_ws"}}
        tmp = TAPE.with_suffix(".tmp")
        tmp.write_text(json.dumps(tape, default=str), encoding="utf-8")
        tmp.replace(TAPE)
        self._publish(rec)
        a = acct
        extra = ""
        if mode == "flat" and jev:
            extra = f"dir={jev['choice']} pL={jev['p_long']:.2f} pS={jev['p_short']:.2f} E={jev['exp_move_bps']:.1f} m={wa.get('micro', 0):+.2f}"
        elif jev:
            extra = f"close_now={jev['close_now']:.2f} move={a.get('move_bps', 0):+.1f}bps"
        fill_txt = " ".join(f"[{x['side']} {x['liq']} {x['reason']} @{x['px']}]" for x in fills)
        print(f"{wa.get('action')} {wa.get('gate_block', '')} {extra} {lat}ms mid={f['mid']:.1f} "
              f"eq={a['equity']:.2f} rt={a['round_trips']} {fill_txt}"
              + (f" ERR {err}" if err else ""), flush=True)

    def run(self) -> None:
        hf_jev.load_dotenv(ROOT)
        self.feed.start()
        threading.Thread(target=self._clock_loop, daemon=True, name="hf-clock").start()
        t_wait = time.time()
        while not self.feed.ready() and time.time() - t_wait < 30:
            time.sleep(0.25)
        print(f"v3 HF engine up: cadence={self.P['cadence_s']}s ws={self.feed.ws_up} "
              f"equity={self.book.acct['equity']:.2f} fills={self.book.acct['fills']} (paper only)", flush=True)
        cad = float(self.P["cadence_s"])
        nxt = time.time()
        while True:
            try:
                self.tick()
            except Exception:
                self.stats["errors"] += 1
                print("tick error:", traceback.format_exc(limit=3).replace("\n", " | "), flush=True)
            nxt += cad
            now = time.time()
            if nxt < now:  # the call overran the slot: skip ahead, never stack calls
                self.stats["overruns"] += 1
                nxt = now + 0.05
            time.sleep(max(0.0, nxt - now))


if __name__ == "__main__":
    Engine().run()
