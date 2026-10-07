# Run 6–7 Oct 2026 (before the break-even stop)

Paper book started 6 Oct 22:16 BST on $10,000. Settings: TP floor $0.22, hard stop $1.15, quiet-market skip (`quiet_max_bps` 8), time exit off, 8 perps.

Snapshot taken 7 Oct ~18:12 BST: **137 round trips, 107W / 30L, realized −$9.33** (gross +7.04, fees $16.61 after rebate).

What the trips showed:
- **LINKUSDT lost $9.08** on its own (45 trips, 14 losses), most of the run's loss.
- **10 of the 29 losers had been up 10–16 bps** (best move, `mfe_bps`) before reversing all the way to the $1.15 stop.

That led to the 7 Oct change: LINK off and a break-even stop. See [docs/TIMELINE.md](../../docs/TIMELINE.md).

Files: `trips.jsonl` (one closed trade per line), `account_snapshot.json` (account totals at the snapshot). Paper only.
