# Jev Pulse v4.1: tidier desk, 5 slots, overnight coach

v4.1 keeps the v4 strategy (see `STRATEGY-v4.md`) and changes three things. It is still **paper only**: every fill is simulated on this Mac from Bitget public books and trade prints, and no order is ever sent.

## 1. Dashboard tidy-up (top half)

- **The asset on/off strip is unchanged.**
- **The header is slimmed** to the title and version, the LIVE · PAPER status, and Jev latency (p50) and cost/h against the limit. The scrolling marquee is gone (it was static text).
- **One row of six tiles** replaces the old KPI header tiles and the two stacked six-tile rows:

  | Tile | Big number | Lines |
  |---|---|---|
  | Equity · net | equity | % vs the $10k start; uPnL and realized |
  | Net P&L · gross · fees | net | gross and net fees (and funding); net at list fees with no rebate |
  | Win rate | win % | W / L and assets traded; Sharpe/leg net and gross |
  | Trades · pace | round trips, plus an **open x / 5** badge | pace/h, ≈/day and fills; top exit kinds (full mix and maker-first counts on hover) |
  | Fees · maker / taker | net fees, plus volume for Bitget | taker and maker fill counts with their net fees; list − rebate, maker share |
  | Max drawdown | $ drawdown | % of peak since the reset; the log it was measured on |

- **The "Jev now" card** sits at the top of the right column, above the decision feed. It holds:
  - the tick beacon (countdown), the feeds-live count, and a small card-level info button
  - regime, stance, writer, last settlement, streak, and book / LS, each with its own info button
- **The price and 24 h change** of the selected asset moved into the price panel header.
- The blotter header reads "x / 5 open".
- Every metric that was on screen is still there (regrouped). All 22 info buttons open their popup; this was checked in headless Chrome at 1440×900 and 1280×800 with no clipping and 0 console errors.

## 2. Five positions

- `max_open_total` 3 → **5**. Still max 1 per asset and $200 per trade.
- Jev budget `jev_budget_usd_h` 0.25 → **0.35 $/h**, so close-now checks on five open trades do not starve entry calls.
- Flat calls are never asked for more assets than there are free slots, which saves budget for close-now.
- Reset on 26 Sep 2026 at 20:23 BST to a fresh $10,000 paper account. The v4 run is archived (moved, with SHA256SUMS) in `logs/archive/run4-v4-20260926-2022/`.

## 3. Overnight coach ("slow coach")

Jev stays the fast live trader. Once a night a slower model reviews the day's real paper trades and may nudge a **bounded playbook overlay**, `playbook/overlay.json`.

- **Models (OpenRouter):** primary `xiaomi/mimo-v2.6-pro`, fallback `deepseek/deepseek-v4.1-flash`. The key is read in-process from `.env` (the same loader as `hf_jev.py`) and is never printed or logged.
- **Input:** a compact JSON summary (a few thousand tokens), never raw tick lines. It covers:
  - Per asset:
    - round trips, wins, and gross / fees / net
    - average MFE / MAE / hold
    - exit-kind mix, maker-exit share and maker-first fill rate
    - TP / stop / time cap used (p10/50/90)
    - room at entry and room of the flat candidates asked (p10/50/90), and results by room bucket
    - Jev E vs the realized move and MFE, and results by E bucket
    - Jev confidence (p_dir) calibration buckets
    - flat-call gate blocks, and close-now stats
  - The current overlay, the bounds and the minimum sample.
  - Sources: `logs/paper_trips.jsonl` (new in v4.1: one line per closed round trip) merged with the account history, plus `logs/paper_decisions.jsonl`.
- **Output:** strict JSON, `{"global": {...}, "assets": {"SYM": {"key": {"value", "reason"}}}, "summary"}`. It must pass a schema check (only the keys below, numeric values, a text reason) or it counts as a miss.

### What it may change, and the bounds

| Knob | Scope | Absolute limits | Max step per night |
|---|---|---|---|
| `room_bias`: room-score weight (ranking **and** the room ≥ 1.0 test) | per asset | 0.5 to 1.5 | ±20% of current |
| `tp_k`: TP = clamp(tp_k × R, **5, 25**) bps | per asset | 0.5 to 2.0 | ±20% |
| `sl_mult`: stop = clamp(sl_mult × TP, **4, 20**) bps | per asset | 0.5 to 1.2 | ±20% |
| `time_cap_min_s` / `time_cap_max_s` | per asset | **120 to 300 s**, min ≤ max | ±20% |
| `e_min_offset_bps`, added to E_min (never below the 4 bps cost) | per asset | **0 to +4** | ±0.8 bps |
| `close_now_thr` (Jev close-now) | global | **0.60 to 0.85** | ±0.05 |

Each proposed value is first limited to one step from the current value, then to the absolute limits. The fixed TP (5–25 bps) and stop (4–20 bps) clamps stay in `hf_params.json` and cannot be overlaid.

**It may never change:** fees, trade size ($200), max positions (5 total / 1 per asset), the user's asset toggles (user toggles always win: the engine checks the switch before anything else and re-checks it right before a fill), feed or safety settings (spread cap, risk veto, cadence, budget …), or anything that places orders. Such keys are rejected by the schema, dropped by the overlay validator (the engine and the dashboard report them), and covered by tests.

### Sample rule and the HOLD rule

- An asset with **fewer than 30 round trips** in the window is **held** (no change). A global change needs 30 round trips in total.
- **A miss is a HOLD.** If both models fail, time out (150 s each) or return invalid JSON, the result is HOLD and yesterday's overlay stays exactly as it was (the file is not rewritten). An unreadable overlay file is also a HOLD.
- If nothing is left after the sample and step rules, the result is also HOLD (overlay unchanged).

### Applying

- `--apply` writes `playbook/overlay.json` atomically (temp file, fsync, rename).
- The engine re-reads it every tick (mtime check). It applies a new value to an asset **only while that asset is flat**, so an open trade keeps the TP, stop, time cap and close-now threshold it was opened with. The threshold is stored with the trade. Assets still waiting are listed as `overlay.pending_until_flat` in each tick.
- An invalid overlay file is ignored and the last good overlay stays in force.
- Every run, HOLDs included, is appended to `logs/coach_runs.jsonl`. Each line records:
  - the time, mode and every model tried (latency, cost, error)
  - the model used and the input-summary SHA-256
  - the proposed changes, the applied changes after clamping (from, proposed, to, clamped) and their reasons
  - held assets, rejected items, and the overlay hash before and after
- The full summary is saved alongside in `logs/coach/<run_id>-<mode>.json`.

### Schedule

- The schedule is `scripts/coach_loop.py`, a small loop process started **in its own session** (like the engine), so it survives shell closure. **No launchd or cron entry was added.**
- It runs once a night at **03:07 Mac local time**. If the Mac was asleep, the missed run happens once on wake if it is less than 12 h late.
- It runs `tools/coach_run.py --apply` as a separate, niced child process with a 15-minute hard timeout, so it never blocks or slows the engine.
- It writes a heartbeat and the next run time to `.state/coach_state.json` every 30 s.
- It does not restart itself after a reboot, and neither does the engine.

### CLI

```
.venv/bin/python tools/coach_run.py --dry-run     # propose only, never writes the overlay
.venv/bin/python tools/coach_run.py --apply       # validate, clamp, write (or HOLD)
  --runs DIR[,DIR]  --labels A[,B]  --hours 24 (0 = all)
```

### Dashboard

The "Overnight coach" card (middle column, under the market matrix) shows:

- the last review time, the model used, and HOLD or the changes applied with their reasons
- the next scheduled run, or "scheduler not running" when its heartbeat is stale
- before the first review: "No review yet · first review scheduled 03:07 …"
- a dry run on its own line, labelled **dry run · not applied**

All of it comes from `/api/coach`, which reads the real files.

## Rollback

- `tools/rollback_coach.sh [--dry-run] [--now]`: back to v4.1 Part A (no coach). It keeps the account and run, parks the coach files, and does a clean restart.
- `tools/rollback_v41.sh [--dry-run]`: back to v4 (3 slots, old dash) plus the archived v4 run. It parks the v4.1 run and every v4.1-only file.
- Backups: `backups/strategy-v4-20260926-2014/` (v4) and `backups/strategy-v41-<ts>/` (Part A, taken before the coach).
