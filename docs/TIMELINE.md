# Jev Pulse · experiment timeline

Honest record of the paper-desk experiments that led to the current final settings.
All times are **BST (Europe/London, UTC+1)** unless noted. Paper only — no live orders.

Repo: [github.com/CryptoCT01/jev-pulse](https://github.com/CryptoCT01/jev-pulse)

---

## Current final book (as of 5 Oct 2026 ~14:25 BST)

![Jev Pulse desk · 5 Oct 2026 · equity ~$10,004 after $10k reset](screenshots/2026-10-05-desk-10004.png)

| Field | Value |
| --- | --- |
| Strategy | v4 / v4.1 multi-asset HF paper desk |
| Reset | ~08:59 BST, 5 Oct 2026 → $10,000 |
| Snapshot | ~14:25–14:26 BST, 5 Oct 2026 |
| Equity (net) | **~$10,004.43** (+0.044%) |
| Net P&L | **+$4.43** (gross +$8.29, fees −$3.89) |
| Win rate | **100%** · 31W / 0L · 31 round trips |
| Max drawdown | **−$3.34** (~0.033% of peak) |
| Pace | ~5.7 RT/h · ~136/day projected |
| Open slots | 5 / 5 (cap) |

### Final config (live `scripts/hf_params.json`)

| Param | Value |
| --- | --- |
| Universe | BTCUSDT, ETHUSDT, SOLUSDT, LINKUSDT, DOGEUSDT, LTCUSDT, XAUUSDT, XAGUSDT |
| Out of universe | BNBUSDT, XRPUSDT (removed 5 Oct ~05:00); SUIUSDT (removed earlier) |
| Cadence | 2.5 s · Jev top-2 by room · max 5 open · $200 clip |
| `min_take_usd` | **0.20** |
| `hard_stop_usd` | **1.25** |
| `no_scratch_exits` | **true** (time soft exit **OFF**) |
| `exit_when_net_green` | **true** |
| TP band (bps) | clamp(1.0 × R, 5, 25) maker |
| Stop (bps) | clamp(0.8 × TP, 4, 20) taker |

**Why this is the final choice (so far):** dollar take-profit at $0.20 with a $1.25 hard stop, and **no time soft exit**, produced the first clean green run after fees. Earlier attempts that kept a time soft exit (even with a $0.20 TP) lost money. Leaving the engine alone while it is green.

---

## Experiments (newest last)

### 1. Early multi-asset run — fees killed high-WR small targets

- High win rate, small dollar targets.
- **~1,381** round trips · WR **~74%** · fees **~$159** → equity **~$9,836**.
- Lesson: win rate alone is not enough when average winners are tiny vs rebated taker cost.
- **SUI** was the worst asset in that book and was later removed.

### 2. 2 Oct 2026 — TP $0.28 / stop $1.0, reset; SUI → BNB

- Raised the dollar take target to **$0.28**, hard stop **$1.0**.
- Swapped **SUI → BNB**.
- Paper book reset for a clean read.

### 3. 2–5 Oct 2026 — BNB/XRP worst; 5 Oct ~05:00 BST swap + tighter TP

- **BNB** and **XRP** were the worst names in the book.
- **5 Oct ~05:00 BST:** swap **BNB + XRP → LTC + LINK**.
- Params: **TP $0.19** · **stop $1.25** · reset again.

### 4. Morning 5 Oct — time soft exit + TP $0.05 (made worse)

- Research idea: add a **time soft exit** and drop take-profit to **$0.05**.
- Result: **93** RT · **28W / 65L** · about **−$14**.
- Lesson: tiny targets + time exits churned fees and flipped the book red.

### 5. Same morning — revert TP to $0.20, keep time exit (still worse)

- Restored **TP $0.20**, left the **time soft exit on**.
- Result: **104** RT · **25W / 79L** — still worse than the no-time-exit book.

### 6. 5 Oct ~08:59 BST — restore original no-scratch (time exit OFF) → green run

- User call: **time exits make it worse**.
- Restored **`no_scratch_exits: true`** (time soft exit **OFF**), **`min_take_usd: 0.20`**, **`hard_stop_usd: 1.25`**, **`exit_when_net_green: true`**.
- Reset to **$10,000** at **~08:59 BST**.
- By **~14:25 BST**: equity **~$10,004.45**, **31W / 0L**, fees **~$3.89**, max DD **~−$3.34**.
- Screenshot: [screenshots/2026-10-05-desk-10004.png](screenshots/2026-10-05-desk-10004.png).

---

## What we tried / final choice (summary)

| Idea | Outcome |
| --- | --- |
| High WR + very small dollar targets | Fees dominate → equity drifts down |
| Swap out weak names (SUI, then BNB/XRP) | Helps the book; LTC + LINK in |
| Time soft exit + ultra-small TP ($0.05) | Clearly worse (heavy losers) |
| Time soft exit + TP $0.20 | Still worse |
| **No time soft exit + TP $0.20 + SL $1.25** | **Best so far — green after fees** |

Flat is still the benchmark. This timeline is paper evidence for Bitget AI Base Camp Hackathon S2, not a live-trading claim.

See also: [STRATEGY-v4.md](../STRATEGY-v4.md), [STRATEGY-v4.1.md](../STRATEGY-v4.1.md), live params in [scripts/hf_params.json](../scripts/hf_params.json).
