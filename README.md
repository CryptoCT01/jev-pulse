# Jev Pulse

BTCUSDT perpetual paper desk. Live Bitget marks. TypeSafe Jev names a side. The executor decides whether that side is allowed to pay a fee.

This repo is the evidence pack: two desk frames, the fee rule, and the paper logs. No API keys. No `.env`. No live order path.

## Two frames

### 1. Pure paper. No fee.

![Pure paper desk. Gross PnL, no fee charged.](docs/paper-no-fee.png)

This is the first run, before the book was reset. Equity **$10,196.34**. PnL **+$196.34**. **25,299** fills. Those fills are at the mark. No fee. No spread. The green number is an upper bound, not a result.

### 2. The same desk after the fee was put in.

![Fee-aware paper desk. Taker, rebate, and net fee on the book.](docs/paper-with-fee.png)

This frame is the run that started after the reset. PnL is **−$12.22**. Equity is **$9,987.78**. A volume counter was added so the fee can be checked against notional crossed. It is not the point of the frame.

What the top row is doing:

| Box | Meaning |
| --- | --- |
| Gross taker 0.06% | Full Bitget USDT-M taker, before the rebate |
| 50% rebate kept | Half of that taker. It is not added on top of Mac equity |
| Net fee in PnL | The half already taken out of the book |
| Equity + rebate | Mac equity plus the rebate. A preview of the credit. Not cash |

On this frame the rebate preview is **$9,999.36**. That is still under the **$10,000** seed. The book is not in profit.

## How the fee was identified

Bitget USDT-M taker is **0.06% per side**. This account has a **50%** maker and taker rebate, so the cash cost is **0.03% per side**. A round trip, open and close, is **6 bps**.

The first paper sim charged nothing. A winning scratch of a few cents looked like edge. Measured on the archived log:

- about 45 hours
- about 25,300 fills
- realized about **+$196** gross
- median hold **6.4 seconds**
- mean closed-leg move **0.27 bps**
- about **2.2%** of legs cleared a 6 bps round trip
- the same book after the rebated taker is about **−$1,800**

The migration was an executor change, not a new model.

1. Charge **3 bps per side** on every new fill. That is the 0.06% taker after the 50% rebate.
2. Show the gross taker beside it, so the 0.06% line is visible and the rebate is the half that was not paid.
3. Do not add the rebate back onto Mac equity. Equity + rebate is a separate preview.
4. One **$200** clip. No add. No same-tick flip. No exit until the mark has moved **6 bps** for or against.
5. Reset the displayed book to a flat **$10,000** so the new rule can be watched on its own. The old realized was not rewritten into that number.

A winner that exits at exactly 6 bps nets about **$0** after the fee. A loser at 6 bps is about **−$0.24**. Flat is still the benchmark.

## Logs

The first run is split only because GitHub rejects a single file over 100 MB. Concatenate the parts in order. Nothing was dropped.

```bash
cat logs/pre-fee/paper_ticks.part1.jsonl \
    logs/pre-fee/paper_ticks.part2.jsonl \
    logs/pre-fee/paper_ticks.part3.jsonl \
    > paper_ticks_pre_fee.jsonl
```

| Path | What it is |
| --- | --- |
| `logs/pre-fee/paper_ticks.part1.jsonl` | First paper run, part 1 |
| `logs/pre-fee/paper_ticks.part2.jsonl` | First paper run, part 2 |
| `logs/pre-fee/paper_ticks.part3.jsonl` | First paper run, part 3 |
| `logs/pre-fee/account.json` | Account snapshot at the reset |
| `logs/with-fee/paper_ticks.jsonl` | Run after the fee rule |
| `logs/with-fee/account.json` | Account snapshot from that run |

Each tick is one JSON object per line: state, Jev's answers, the action the executor actually took, and the paper account after the fill or the hold.
