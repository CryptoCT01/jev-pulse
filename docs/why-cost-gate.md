# Why the executor changed

The first paper run is kept. It is not the live book.

The first run is in this repo, split so GitHub will take it:

- `logs/pre-fee/paper_ticks.part1.jsonl`
- `logs/pre-fee/paper_ticks.part2.jsonl`
- `logs/pre-fee/paper_ticks.part3.jsonl`
- `logs/pre-fee/account.json`

## What that run was

About 45 hours, ~25,300 Mac paper fills, realized about **+$196 gross**. Median hold **6.4 seconds**. Mean closed-leg move **0.27 bps**. Gross win rate about **51%**.

A rebated Bitget USDT-M taker round trip is about **6 bps** (0.03% per side, open and close). After that cost the same book is about **−$1,800**. About **2.2%** of legs cleared 6 bps. Confidence did not separate winners. Following the 1-minute, 5-minute, or 1-hour return lost to doing nothing on a later slice of the same tape.

The prompt told the model to flip so the desk looked busy. The executor obeyed, including a same-tick reverse. Fills were at the mark. No fee. No slippage. The green number was an upper bound, not a result.

## What the desk does now

One $200 clip. No add. No same-tick flip. No exit until the mark has moved 6 bps for or against. New fills pay 3 bps a side. The desk was reset to a flat **$10,000** so the new rule can be watched on its own. Older realized is not rewritten into that number.

Flat is the benchmark. A 6 bps winner nets about $0 after the fee. A 6 bps loser is about −$0.24. This stops the scratch. It does not claim an edge.
