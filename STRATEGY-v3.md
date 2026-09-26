# Jev Pulse v3 — HF paper strategy (BTCUSDT perp, Bitget public data, PAPER ONLY)

No order is ever sent to Bitget. `scripts/hf_engine.py` has no exchange order code.

## Loop
* One persistent process (`scripts/hf_engine.py`, supervised by `scripts/run_companion_loop.sh`).
* Bitget public WebSocket: `books15` (top-15 book, ~150 ms), `trade` (every print, taker side), `ticker`
  (funding, mark, index, OI). REST every 60 s: 5-min long/short account ratio.
* Jev (TypeSafe `typesafe/jev-1.13`, OpenRouter Decisions API) every **2.5 s**, start-to-start; a slow call
  is never stacked (the next slot is skipped). One persistent HTTPS connection. ~1.2k input tokens/call.
* Exits (take-profit, stop, time stop) are evaluated on **every public trade print** and a 200 ms clock,
  independent of the model cadence.

## Jev questions
* Flat: `direction` (choice UP/DOWN, "which way first by ≥4 bps in 60-120 s"), `move_60s` (score over
  5 buckets <2, 2-4, 4-7, 7-12, >12 bps → probability-weighted expected move E), `risk_stress` (noul).
* In a trade: `close_now` (noul), `risk_stress` (noul).
* State sent: spread, imbalance L1/L5/L15 and within 5 bps, microprice offset, taker-flow imbalance
  5/15/60 s and notional, returns 5/15/30/60/300 s, last 30 one-second returns, realised vol,
  60 s range and position in range, funding + minutes to settlement, perp basis, OI change, L/S ratio,
  position (entry, uPnL bps, age, TP/stop, best/worst), costs, last trade result.

## Entry gate (taker, one $200 clip, flat only, never a same-tick flip)
1. risk_stress < 0.85, spread ≤ 1 bps, book fresher than 3 s;
2. Jev lean: P(chosen side) ≥ 0.60 and ≥ 0.20 over the other side;
3. Jev E ≥ E_min. E_min starts at **4.0 bps = round-trip cost with a maker TP** (taker 3 + maker 1) and
   adapts every 20 min on the trailing hour: +0.5 if > 20 entries/h, −0.5 if < 5 entries/h, never below
   4.0, never above 9.0 (keeps it high-frequency without ever accepting E below cost);
4. flow/book agreement: m = 0.45·imb_5bps + 0.30·imb_L1 + 0.15·flow_5s + 0.10·flow_15s must have the
   trade's sign with |m| ≥ 0.12;
5. same side within 30 s of a close needs E_min + 2 bps and 1.5× the flow threshold (opposite side free).

## Exits
* Post-only **maker take-profit** at entry ± TP, TP = clamp(2.5·σ₁₂₀ₛ, 6, 10) bps (σ from 1 s returns).
* **Taker stop** 4 bps from the entry fill, triggered by a print at/through the level.
* **Time stop** 120 s → passive exit at the touch for 15 s → taker.
* **Jev close-now** ≥ 0.70 (position ≥ 8 s old) → passive exit at the touch for 5 s → taker.
* Funding is charged if a position is open across a settlement.

## Honest paper fills (`scripts/hf_sim.py`)
* Taker at the live touch: buy at best ask, sell at best bid (book read *after* the model answered).
* Maker fills only when a public print trades **through** the limit (strictly beyond), filled at the limit.
* Stop fills at the worse of the triggering print and the touch.
* Fees per fill, Bitget USDT-M with the 50% rebate: taker 6 bps full / 3 bps net, maker 2 bps full /
  1 bps net. Every fill records full fee, rebate and net; the account keeps totals by maker/taker, gross
  PnL, funding and exits by type. `logs/paper_fills.jsonl` has every fill.

## Break-even arithmetic (why this is hard)
TP 6 maker nets +2 bps; a 4 bps stop nets −10 bps; a flat time exit costs 4–6 bps. At TP 6/SL 4 the
book needs ≈ 83% of TP-or-stop exits to be take-profits to break even (2p = 10(1−p)). Measured edge so far is ≈ 0.
