# Jev Pulse v4: multi-asset HF paper desk

> **Live book (5 Oct 2026):** universe and dollar exits are documented in [docs/TIMELINE.md](docs/TIMELINE.md) and [scripts/hf_params.json](scripts/hf_params.json). Current universe is BTC/ETH/SOL/LINK/DOGE/LTC/XAU/XAG (BNB/XRP out; SUI out earlier). `min_take_usd=0.20`, `hard_stop_usd=1.25`, `no_scratch_exits=true` (time soft exit OFF).


PAPER ONLY. The engine has no order code. It reads Bitget public market data only and never sends orders.

## Why v4
v3.1 traded only BTC. On 26 Sep, BTC's typical 5-minute range was about 4 bps, against a round-trip cost of 4–6 bps. The result was 81 round trips at +$0.33 gross and −$8.43 net, so fees ate everything. v4 keeps the same high-frequency engine and the same 2.5 s Jev cadence. The difference is that it spreads the scan over 8 Bitget USDT-M perps and trades only the ones where the move can clear the cost.

## Universe (`hf_params.json` → `universe`, easy to extend)
BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, SUIUSDT, XAUUSDT, XAGUSDT (product type USDT-FUTURES).
- **Feed:** one public WebSocket carries books15, trade and ticker for every symbol. Reconnects use backoff from 1 to 30 s. The socket reconnects if it goes silent for 15 s. A symbol is re-subscribed if it has been stale for 45 s.
- **Contract specs:** tick = `priceEndStep × 10^-pricePlace` and size step = `sizeMultiplier`, from `/api/v2/mix/market/contracts`. They are cached in `.state/contracts.json`.

## Per-asset stats
- **Mids:** 1 s mids, keeping the last 30 min.
- **R** = median high-low range of 1 s mids over rolling 120 s windows (5 s step) in the last 10 min. R is available after 180 s of data; before that the asset shows as warming.
- **Book:** spread in bps, plus USD depth within 5 bps on each side.
- **Idle** (no trading, not counted as an error). An asset is idle when any of these holds:
  - R < 1 bps ("not moving")
  - the last trade was more than 120 s ago ("no trades")
  - the book has not updated for more than 30 s ("book not updating")

## Room score
`room = R / (4 bps round-trip cost + spread bps) × depth_factor`, where `depth_factor = min(1, √(thinner-side depth within 5 bps / $10k))`.
- An asset is a flat candidate only when all of these hold: it is ON, it is live, it has room ≥ 1.0, and its spread is ≤ 3 bps.

## Jev call budget
Each tick (2.5 s), Jev is asked:
1. a close-now question for each open position, at most every 5 s per position (`jev_pos_every_s`), least recently asked first
2. then the top N = 2 flat candidates by room, with that asset's own context: symbol, the last 12 one-second candles and 20 one-second returns (`jev_candles_n`), book and flow features, R, spread and cost floor

Calls run in parallel, so a tick takes about one Jev latency.

**Budget:** a hard token bucket of $0.35/h with a 60 s burst (v4.1; it was $0.25/h in v4, raised so close-now checks on 5 open trades do not starve entry calls). A call is made only if the bucket covers its estimated cost; otherwise it is logged as `budget skip`. Rolling cost per hour is logged in every tick. A flat call costs about $8e-5 (about 1.8k input tokens), so $0.35/h buys about 3 calls per 2.5 s tick. Flat calls are never asked for more assets than there are free slots. Close-now calls go first, then flat candidates, and any excess shows as `budget skip`.

## Entry gate (per asset)
The v3.1 gate, scaled per asset:
- Jev direction probability ≥ 0.60 with an edge ≥ 0.20
- Jev expected move ≥ E_min, where E_min = max(4 bps, that asset's adaptive base) + its spread. E_min is never below cost.
- room ≥ 1.0
- flow and microprice agree (|micro| ≥ 0.12)
- risk veto < 0.85
- 30 s same-side cooldown

The adaptive base is per asset. It moves between 4 and 9 bps to hold 5–20 entries per hour.

## Sizing and caps
- $200 notional per trade, rounded to the nearest size step, so real notional is roughly ±7% on coarse steps.
- Max 1 open trade per asset and max 5 concurrent overall (v4.1; v4 was 3). Size stays $200 per trade.
- Entry is taker at the touch.

## Exits (v3.1 logic, scaled per asset)
- **Take-profit (TP):** clamp(1.0 × R, 5, 25) bps, a resting maker order rounded to the asset's tick. It fills only when the market trades through it.
- **Stop:** clamp(0.8 × TP, 4, 20) bps, taker.
- **Time cap:** 120–300 s, adaptive; a slower asset gets a longer cap.
- **Soft exit:** when the time cap hits, or Jev close-now is ≥ 0.70 after 8 s, the exit is maker-first at the touch for 20 s, then falls back to taker.
- **Shutdown:** SIGTERM closes every position at the touch (`shutdown_taker`).
- **Fees (net per side):** maker 1 bps, taker 3 bps. The same schedule is assumed for every perp.

## Asset toggles (real)
- Each dash chip's switch POSTs `{symbol, enabled}` to `/api/assets`. The endpoint accepts only requests that meet all of these:
  - loopback client
  - Host 127.0.0.1/localhost:8790
  - header `X-Jev-Control: 1`
  - JSON body of at most 512 B
- The request is strictly validated, then `.state/assets.json` is written atomically and its `seq` is incremented.
- The engine re-reads the file every tick, and again just before a fill. OFF means no new entries on that asset; an open trade there keeps its normal exits, and the chip shows `closing` until the asset is flat.
- The engine publishes the effective state (on / off / closing / idle / warming / no_feed) and the `seq` it applied. The chip follows the engine, not the click.
- Default is all ON, and the state persists across restarts. An invalid file keeps the last good state and is shown on the dash.

## Accounting and logs
- **Account:** `.state/paper_account.json` (version 4) holds totals plus a `by_asset` bucket for each symbol. Both are updated in the same call, so the sum of the per-asset figures always equals the account.
- **Logs:** every tick, decision and fill line carries `symbol`, in `logs/paper_ticks.jsonl`, `logs/paper_decisions.jsonl` and `logs/paper_fills.jsonl`.
- **Tools:** `tools/v4_stats.py` gives the per-asset and total breakdown with exit kinds. `tools/rollback_v4.sh [--dry-run]` restores v3.1.
