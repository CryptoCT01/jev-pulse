import sys, math, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hf_sim, hf_engine

class Env:
    def __init__(self, bid=84000.0, ask=84000.1, t=1_000_000):
        self.bid, self.ask, self.t = bid, ask, t
    def touch(self): return self.bid, self.ask
    def clock(self): return self.t

def mk(env, **p):
    return hf_sim.PaperBook(touch=env.touch, clock=env.clock, persist=False, fills_log=None,
                            params={"tp_bps": 8, "sl_bps": 4, "time_stop_s": 120, "passive_s": 10, "close_passive_s": 5, **p})

class T(unittest.TestCase):
    def test_taker_entry_at_touch_and_fee(self):
        e = Env(); b = mk(e)
        f = b.open(+1)
        self.assertEqual(f["px"], 84000.1)          # buy at ask
        self.assertAlmostEqual(f["fee_full"], 200 * 0.0006, 9)
        self.assertAlmostEqual(f["fee_net"], 200 * 0.0003, 9)
        self.assertAlmostEqual(f["rebate"], 200 * 0.0003, 9)
        b2 = mk(Env()); f2 = b2.open(-1); self.assertEqual(f2["px"], 84000.0)  # sell at bid

    def test_tp_needs_trade_through(self):
        e = Env(); b = mk(e); b.open(+1)
        tp = b.position()["tp_px"]
        self.assertEqual(tp, hf_sim.round_up(84000.1 * 1.0008))
        e.t += 1000
        self.assertIsNone(b.on_trade(e.t, tp, 1, 1))        # print AT the limit: no fill
        r = b.on_trade(e.t, tp + 0.1, 1, 1)                 # print through: maker fill at limit
        self.assertEqual(r["exit"], tp); self.assertEqual(r["exit_liq"], "maker"); self.assertEqual(r["exit_kind"], "tp_maker")
        qty = 200 / 84000.1
        gross = (tp - 84000.1) * qty
        fees = 84000.1 * qty * 0.0003 + tp * qty * 0.0001
        self.assertAlmostEqual(r["gross"], gross, 9)
        self.assertAlmostEqual(r["pnl"], gross - fees, 9)
        a = b.acct
        self.assertAlmostEqual(a["realized"], gross - fees, 9)
        self.assertAlmostEqual(a["fees_paid"], fees, 9)
        self.assertAlmostEqual(a["fees_full"], 84000.1 * qty * 0.0006 + tp * qty * 0.0002, 9)
        self.assertAlmostEqual(a["fees_full"] - a["fees_rebate"], a["fees_paid"], 9)
        self.assertEqual(a["fees_by_liq"]["maker"]["count"], 1); self.assertEqual(a["fees_by_liq"]["taker"]["count"], 1)
        self.assertEqual(a["round_trips"], 1); self.assertEqual(a["wins"], 1); self.assertEqual(a["fills"], 2)
        self.assertAlmostEqual(a["equity"], 10000 + gross - fees, 9)

    def test_short_tp(self):
        e = Env(); b = mk(e); b.open(-1)
        tp = b.position()["tp_px"]
        self.assertEqual(tp, hf_sim.round_down(84000.0 * (1 - 0.0008)))
        self.assertIsNone(b.on_trade(e.t + 1, tp, 1, -1))
        r = b.on_trade(e.t + 2, tp - 0.1, 1, -1)
        self.assertEqual(r["exit"], tp); self.assertGreater(r["gross"], 0)

    def test_stop_fills_at_worse_of_print_and_touch(self):
        e = Env(); b = mk(e); b.open(+1)
        sl = b.position()["sl_px"]
        e.bid, e.ask = 83960.0, 83960.1
        self.assertIsNone(b.on_trade(e.t + 1, sl + 0.2, 1, -1))
        r = b.on_trade(e.t + 2, 83965.0, 1, -1)   # print below stop, bid even lower -> fill at bid
        self.assertEqual(r["exit"], 83960.0); self.assertEqual(r["exit_liq"], "taker"); self.assertEqual(r["exit_kind"], "stop_taker")
        qty = 200 / 84000.1
        self.assertAlmostEqual(r["pnl"], (83960.0 - 84000.1) * qty - 84000.1 * qty * 3e-4 - 83960.0 * qty * 3e-4, 9)
        self.assertEqual(b.acct["losses"], 1); self.assertEqual(b.acct["streak"], -1)

    def test_time_stop_passive_then_taker(self):
        e = Env(); b = mk(e); b.open(+1)
        e.t += 121_000; e.bid, e.ask = 84001.0, 84001.1
        self.assertIsNone(b.on_clock())
        p = b.position(); self.assertEqual(p["exit"]["kind"], "passive"); self.assertEqual(p["exit"]["px"], 84001.1)
        self.assertIsNone(b.on_trade(e.t + 1, 84001.1, 1, 1))  # at price: no fill
        e.t += 10_001; e.bid, e.ask = 84000.5, 84000.6
        r = b.on_clock()
        self.assertEqual(r["exit_kind"], "time_taker"); self.assertEqual(r["exit"], 84000.5)

    def test_time_stop_passive_maker_fill(self):
        e = Env(); b = mk(e); b.open(+1)
        e.t += 121_000; e.bid, e.ask = 84001.0, 84001.1
        b.on_clock()
        r = b.on_trade(e.t + 5, 84001.2, 1, 1)
        self.assertEqual(r["exit_kind"], "time_maker"); self.assertEqual(r["exit_liq"], "maker"); self.assertEqual(r["exit"], 84001.1)

    def test_jev_close(self):
        e = Env(); b = mk(e); b.open(-1)
        self.assertEqual(b.request_close(), "passive_posted")
        self.assertEqual(b.request_close(), "already_exiting")
        e.t += 5001
        r = b.on_clock(); self.assertEqual(r["exit_kind"], "jev_close_taker"); self.assertEqual(r["exit"], e.ask)

    def test_no_double_open_and_old_trades_ignored(self):
        e = Env(); b = mk(e); b.open(+1)
        self.assertIsNone(b.open(-1))
        self.assertIsNone(b.on_trade(e.t - 5, 1.0, 1, -1))  # print from before the entry

    def test_funding(self):
        e = Env(); b = mk(e); b.open(+1)
        e.t += 1000
        b.on_clock(funding_rate=0.0001, funding_due_ms=e.t - 1)
        self.assertAlmostEqual(b.acct["funding_paid"], 200 * 0.0001 * 1, 6)
        e.t += 121_000; b.on_clock(); e.t += 10_001; r = b.on_clock()
        self.assertAlmostEqual(r["funding"], 0.02, 6)
        self.assertAlmostEqual(b.acct["realized"], r["pnl"], 9)

    def test_gate(self):
        P = hf_engine.load_params()
        f = {"flow_15s": 0.5, "flow_5s": 0.5, "imb_5bps": 0.3, "imb_l1": 0.3, "spread_bps": 0.01, "book_age_ms": 100, "sigma_120s_bps": 6}
        j = {"choice": "LONG", "p_long": 0.7, "p_short": 0.3, "p_none": 0.0, "exp_move_bps": 6.0, "risk_stress": 0.2}
        ctx = {"exp_move_min": 5.0, "last_close": None, "now_ms": 10**6}
        self.assertEqual(hf_engine.gate_entry(f, j, ctx, P)[:2], (1, "enter"))
        self.assertEqual(hf_engine.gate_entry(f, {**j, "risk_stress": 0.9}, ctx, P)[1], "veto_risk_stress")
        self.assertEqual(hf_engine.gate_entry(f, {**j, "exp_move_bps": 4.0}, ctx, P)[1], "move_below_cost")
        self.assertEqual(hf_engine.gate_entry({**f, "flow_15s": -0.5, "flow_5s": -0.5}, j, ctx, P)[1], "flow_disagrees")
        self.assertEqual(hf_engine.gate_entry(f, {**j, "p_long": 0.55, "p_short": 0.45}, ctx, P)[1], "weak_direction")
        jd = {**j, "choice": "SHORT", "p_long": 0.2, "p_short": 0.8}
        self.assertEqual(hf_engine.gate_entry({**f, "flow_15s": -0.5, "flow_5s": -0.5, "imb_5bps": -0.3, "imb_l1": -0.3}, jd, ctx, P)[:2], (-1, "enter"))
        cd = {**ctx, "last_close": {"side": "long", "ts_ms": 10**6 - 5000}}
        self.assertEqual(hf_engine.gate_entry(f, j, cd, P)[1], "move_below_cost")  # same side inside cooldown needs +2 bps
        self.assertEqual(hf_engine.gate_entry(f, {**j, "exp_move_bps": 7.5}, cd, P)[1], "enter")
        cd2 = {**ctx, "last_close": {"side": "short", "ts_ms": 10**6 - 5000}}
        self.assertEqual(hf_engine.gate_entry(f, j, cd2, P)[1], "enter")  # opposite side not penalised

if __name__ == "__main__":
    unittest.main(verbosity=1)
