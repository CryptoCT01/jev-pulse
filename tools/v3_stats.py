#!/usr/bin/env python3
"""Summarise the live v3 paper run (read-only). Usage: .venv/bin/python tools/v3_stats.py"""
import json, os, re, statistics, time
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
acct = json.load(open(os.path.join(root, ".state/paper_account.json")))
t0 = acct.get("run_start_ms", 0) / 1000
hours = max((time.time() - t0) / 3600, 1e-9)
recs = []
with open(os.path.join(root, "logs/paper_ticks.jsonl")) as f:
    for line in f:
        try: recs.append(json.loads(line))
        except Exception: pass
lat = [r.get("latency_ms") for r in recs if r.get("latency_ms")]
errs = [r for r in recs if r.get("error")]
eng = recs[-1].get("engine", {}) if recs else {}
gaps = [(b["ts_ms"] - a["ts_ms"]) / 1000 for a, b in zip(recs, recs[1:])]
blocks = {}
for r in recs:
    b = (r.get("writer_action") or {}).get("gate_block")
    if b: blocks[b] = blocks.get(b, 0) + 1
E = [((r.get("jev") or {}).get("exp_move_bps")) for r in recs if (r.get("jev") or {}).get("exp_move_bps") is not None]
cn = [((r.get("jev") or {}).get("close_now")) for r in recs if (r.get("jev") or {}).get("close_now") is not None]
log = open(os.path.join(root, "logs/companion_loop.log")).read()
print(f"run since {time.strftime('%H:%M:%S', time.localtime(t0))} ({hours*60:.1f} min), ticks={len(recs)}")
print(f"cadence: median gap {statistics.median(gaps) if gaps else 0:.2f}s, max {max(gaps) if gaps else 0:.1f}s; overruns={eng.get('overruns')}")
print(f"errors in ticks: {len(errs)}; 'tick failed' in log: {log.count('tick failed')}; ERR lines: {len(re.findall(r'ERR', log))}")
if lat: print(f"latency ms p50={statistics.median(lat):.0f} p90={sorted(lat)[int(.9*len(lat))-1]:.0f} max={max(lat)}")
print(f"model cost ${eng.get('cost_usd', 0):.4f} -> ${eng.get('cost_usd', 0)/hours:.4f}/h")
print(f"round trips {acct['round_trips']} -> {acct['round_trips']/hours:.1f}/h; fills {acct['fills']}")
fl = acct.get("fees_by_liq", {})
for k in ("maker", "taker"):
    v = fl.get(k, {}); print(f"  {k}: fills={v.get('count',0)} notional=${v.get('notional',0):.0f} full=${v.get('full',0):.4f} rebate=${v.get('rebate',0):.4f} net=${v.get('net',0):.4f}")
print(f"gross ${acct.get('gross_realized',0):.4f} fees_full ${acct.get('fees_full',0):.4f} rebate ${acct.get('fees_rebate',0):.4f} fees_net ${acct.get('fees_paid',0):.4f} realized ${acct.get('realized',0):.4f} equity ${acct.get('equity',0):.2f}")
print("exits:", acct.get("exits_by_kind"), " W/L:", acct.get("wins"), acct.get("losses"))
print("gate blocks:", dict(sorted(blocks.items(), key=lambda x: -x[1])))
if E: print(f"Jev E bps: median {statistics.median(E):.2f} p90 {sorted(E)[int(.9*len(E))-1]:.2f}; share>=4: {sum(e>=4 for e in E)/len(E):.1%}")
if cn: print(f"close_now median {statistics.median(cn):.2f} max {max(cn):.2f} (n={len(cn)})")
for h in acct.get("history", [])[-8:]: print("  trade", {k: h.get(k) for k in ("side","gross_bps","mfe_bps","mae_bps","tp_bps","pnl","exit_kind","hold_s")})
H = acct.get("history", [])
if H:
    print(f"avg gross {statistics.mean(h['gross_bps'] for h in H):+.2f} bps/RT, avg MFE {statistics.mean(h['mfe_bps'] for h in H):.2f}, avg MAE {statistics.mean(h['mae_bps'] for h in H):.2f}, avg hold {statistics.mean(h['hold_s'] for h in H):.0f}s")
