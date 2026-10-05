# Paper run export — 2026-10-05 (v4.1 green run)

Public scrubbed raw logs from the live Jev Pulse paper desk for **Agentic S2** judges.

| | |
| --- | --- |
| Strategy | `jev-pulse-v4-multi` / v4.1 |
| Run start (BST) | 2026-10-05 08:59:23 BST |
| `run_start_ms` | `1791187163150` |
| Export time (BST) | 2026-10-05 14:49:19 BST |
| Starting equity | $10,000.00 |
| Equity at export | $10,000.6087 |
| Realized PnL | $3.5331 |
| Round trips | **38** (35W / 3L) |
| Fills | 81 |
| Decision rows | 143 (JEV calls since reset) |

Paper only — no live orders. Live engine was **not** restarted for this export.

## Files

| File | Description |
| --- | --- |
| `trips.jsonl` | Closed round trips since `run_start_ms` (one JSON object per line) |
| `fills.jsonl` | Individual fill events (entries + exits) |
| `decisions.jsonl` | JEV / writer decision samples for this run |
| `account_snapshot.json` | Scrubbed `paper_account` snapshot at export (positions, history, fees) |
| `README.md` | This file |

Filter rule: keep rows where `ts_ms >= run_start_ms` (exit timestamp for trips).

## Field guide (trips.jsonl)

Core fields judges need:

| Field | Meaning |
| --- | --- |
| `ts_ms` | Exit / settle time (Unix ms) |
| `opened_ms` | Entry time (Unix ms) |
| `symbol` | e.g. `BTCUSDT`, `XAGUSDT` |
| `side` | `long` or `short` |
| `entry` / `exit` | Entry and exit prices |
| `qty` | Position size |
| `notional` | Approx USDT notional at entry |
| `pnl` | Net PnL after fees / rebate / funding (USDT) |
| `gross` | Gross PnL before fees |
| `pnl_bps` | Net PnL in basis points of notional |
| `hold_s` | Hold duration (seconds) |
| `exit_kind` | e.g. `fee_green_taker`, `tp_maker`, `stop_taker` |
| `entry_liq` / `exit_liq` | `maker` or `taker` |
| `tp_bps` / `sl_bps` | Take-profit / stop distances in bps |
| `mfe_bps` / `mae_bps` | Max favorable / adverse excursion |
| `signal` | Snapshot of gate inputs at entry (`p_dir`, `exp_move`, `e_min`, …) |

## Field guide (fills.jsonl)

| Field | Meaning |
| --- | --- |
| `ts_ms` | Fill time (Unix ms) |
| `symbol` | Instrument |
| `side` | `buy` or `sell` |
| `px` | Fill price |
| `qty` | Fill quantity |
| `notional` | `px * qty` |
| `liq` | `maker` or `taker` |
| `reason` | e.g. `jev_entry`, `fee_green_taker`, `tp_maker`, `stop_taker` |
| `fee_net` / `fee_full` / `rebate` | Fee accounting |
| `gross` / `net` | Gross and net USDT for this fill |

## Field guide (decisions.jsonl)

| Field | Meaning |
| --- | --- |
| `ts_ms` | Decision time |
| `symbol` | Instrument queried |
| `jev` | Model choice / probabilities / expected move |
| `writer_action` | Gate result (`action`, `gate_block`, `p_dir`, `edge`, …) |
| `model` | Model id used for the call |
| `latency_ms` | Inference latency |
| `usage` / `cost` | Token usage and inferred call cost |

## Field guide (account_snapshot.json)

| Field | Meaning |
| --- | --- |
| `equity_start` | Starting paper equity (USDT) |
| `equity` | Mark-to-market equity at snapshot |
| `cash` | Cash balance |
| `realized` | Realized net PnL this run |
| `round_trips` / `wins` / `losses` | Closed-trade stats |
| `fills` | Fill count |
| `positions` | Open positions (with `entry`, `qty`, `upnl`, TP/SL) |
| `history` | Closed trips embedded in account state |
| `fill_log` | Recent fills embedded in account state |
| `by_asset` | Per-symbol PnL / fee breakdown |
| `fees_by_liq` | Maker vs taker fee totals |
| `_export` | Export metadata (times, note) |

**Equity** in the snapshot is mark-to-market (cash + unrealized). **PnL** on trips is realized net of fees.

## Scrubbing

- No API keys, OpenRouter keys, or private emails
- No `.env` contents
- Trading metrics and public model ids only

## Quick verify

```bash
# trip count
wc -l trips.jsonl
# last trip
tail -n 1 trips.jsonl | python3 -m json.tool | head
# realized from trips
python3 -c "import json; print(sum(json.loads(l)['pnl'] for l in open('trips.jsonl')))"
```
