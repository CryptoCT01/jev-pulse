#!/usr/bin/env python3
"""JEV PULSE desk — file-backed. No OpenRouter. No Bitget on the request path."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dash"
LOG = ROOT / "logs" / "paper_ticks.jsonl"
TAPE_FILE = ROOT / "logs" / "tape_latest.json"
ACCT = ROOT / ".state" / "paper_account.json"
PARAMS = ROOT / "scripts" / "hf_params.json"  # v3 HF strategy parameters (read-only here)
sys.path.insert(0, str(ROOT / "scripts"))

import tape  # noqa: E402
import ws_public  # noqa: E402
import studio_sync  # noqa: E402

HOST = "0.0.0.0"
PORT = 8790
START_EQUITY = 10_000.0
CANDLE_KEEP = 1800  # 1 s candles kept in memory for the 1S view (30 min). Nothing written.
STATE_CANDLES = 240  # /api/state still carries the last 4 min, as before
# --- /api/pricehist: longer BTC price history for the 1H / 6H chart views ---
# Bitget PUBLIC REST 1m klines (last-trade closes, same price type as the 1 s WS candles) for
# BTCUSDT USDT-M perp, fetched by a background thread every KLINE_EVERY_S and cached in memory,
# then merged with the live 1 s buffer. Never fetched on the request path. Nothing written.
KLINE_PATH = "/api/v2/mix/market/candles?symbol=BTCUSDT&productType=USDT-FUTURES&granularity=1m&limit=400"
KLINE_EVERY_S = 30
PRICE_RANGES = {"1h": (3600, 5), "6h": (21600, 60)}  # span seconds, point resolution seconds
_kl: dict = {"rows": [], "ok": False, "err": "", "fetched_ms": 0, "tries": 0}
_kl_lock = threading.Lock()
_live: dict = {"mark": 0.0, "funding": 0.0, "bid": 0.0, "ask": 0.0, "ts": 0.0}


def _tail_jsonl(path: Path, n: int = 48) -> list[dict]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            buf = b""
            while size > 0 and buf.count(b"\n") <= n:
                step = min(65536, size)
                size -= step
                fh.seek(size)
                buf = fh.read(step) + buf
    except OSError:
        return []
    out = []
    for line in buf.splitlines()[-n:]:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _ticker_loop() -> None:
    return


def _kline_loop() -> None:
    """Background: refresh the cached 1m kline closes. Keeps the last good rows if a fetch fails."""
    while True:
        try:
            data = tape._get(KLINE_PATH, timeout=8.0) or []
            rows = {}
            for k in data:
                t, c = int(k[0]) // 1000, float(k[4])
                if t > 0 and c > 0:
                    rows[t] = c
            if not rows:
                raise ValueError("empty kline response")
            with _kl_lock:
                _kl.update(rows=sorted(rows.items()), ok=True, err="", fetched_ms=int(time.time() * 1000))
        except Exception as exc:  # network / API error: keep serving the cached rows, flag it
            with _kl_lock:
                _kl.update(ok=False, err=f"{type(exc).__name__}: {str(exc)[:120]}")
        with _kl_lock:
            _kl["tries"] += 1
        time.sleep(KLINE_EVERY_S)


def _pricehist(rng: str) -> dict:
    """Real closes only: Bitget REST 1m klines up to where the live 1 s buffer starts, then the live
    1 s closes bucketed to the range resolution (close of each bucket, stamped at the bucket open,
    the kline convention). No interpolation; a missing stretch is simply shorter coverage."""
    span, step = PRICE_RANGES.get(rng, PRICE_RANGES["1h"])
    now = time.time()
    live = ws_public.snapshot()
    bars = sorted((int(b["t"]) // 1000, float(b["c"])) for b in live.get("candles") or [] if float(b.get("c") or 0) > 0)
    with _kl_lock:
        kl = list(_kl["rows"])
        kmeta = {"ok": _kl["ok"], "err": _kl["err"], "fetched_ms": _kl["fetched_ms"], "tries": _kl["tries"],
                 "age_s": round(now - _kl["fetched_ms"] / 1000, 1) if _kl["fetched_ms"] else None}
    live_from = -(-bars[0][0] // step) * step if bars else None  # first full bucket of the live buffer
    start = int(now) - span
    pts: list = []
    for t, c in kl:
        if t >= start and (live_from is None or t < live_from):
            pts.append([t, c])
    n_kl = len(pts)
    buckets: dict = {}
    for t, c in bars:
        if t >= start and live_from is not None and t >= live_from:
            buckets[t - t % step] = c  # sorted input, so the bucket keeps its last close
    last_kl = pts[-1][0] if pts else -1
    pts.extend([t, c] for t, c in sorted(buckets.items()) if t > last_kl)
    segs = []
    if n_kl:
        segs.append({"source": "bitget_rest_klines_1m", "from_ms": pts[0][0] * 1000, "to_ms": pts[n_kl - 1][0] * 1000, "n": n_kl, "res_s": 60})
    if len(pts) > n_kl:
        segs.append({"source": "ws_1s_closes", "from_ms": pts[n_kl][0] * 1000, "to_ms": pts[-1][0] * 1000, "n": len(pts) - n_kl, "res_s": step})
    cov = (pts[-1][0] - pts[0][0]) if len(pts) > 1 else 0
    return {"ok": bool(pts), "range": rng if rng in PRICE_RANGES else "1h", "span_s": span, "res_s": step,
            "coverage_s": cov, "points": pts, "segments": segs, "kline": kmeta, "ws": bool(live.get("ws")),
            "live_buffer_from_ms": bars[0][0] * 1000 if bars else None, "now_ms": int(now * 1000)}


def _regime(tick: dict) -> dict:
    st = tick.get("state") or {}
    vol = float(st.get("vol_short") or 0)
    r5 = abs(float(st.get("ret_5m") or 0))
    rp = float(st.get("range_pos") or 0.5)
    label = "Range" if (vol < 0.0018 or r5 < 0.0025) else "Trend"
    score = int(max(8, min(92, 50 + (0.5 - abs(rp - 0.5)) * 80)))
    return {"label": label, "score": score}


# --- /api/history: read-only equity, trades and marks from paper_ticks.jsonl ---
# One incremental reader. Boot scans the file once in a thread; after that only
# appended bytes are parsed. Nothing here writes a file.
HIST_STRIDE = 10  # older lines: parse 1 in 10. history is 40 deep, <=1 close per tick, so no close is lost
HIST_FULL_BYTES = 40 << 20  # newest ~40 MB parsed line by line (~6 h): exact entries and marks
HIST_MARK_MS = 6 * 3600 * 1000  # marks / entries kept for the 1H and 6H price views
FEE_SIDE = 0.0003  # v1/v2 legs only: net taker per side. v3 legs carry their own maker/taker fee split
_hist_lock = threading.Lock()
_hist: dict = {"off": 0, "ino": None, "ready": False, "checked": 0.0, "lines": 0, "parsed": 0}


def _hist_reset(h: dict) -> None:
    h.update(eq=[], marks=[], opens=[], trades={}, prev=None, run_start_ms=0, first_ms=0)


def _hist_ingest(h: dict, t: dict, full: bool) -> None:
    a = t.get("account") or {}
    ts = int(t.get("ts_ms") or 0)
    if not a or not ts:
        return
    fills = int(a.get("fills") or 0)
    prev = h["prev"]
    if prev is not None and fills < prev["fills"]:  # paper account was reset: new run
        _hist_reset(h)
        h["run_start_ms"], prev = ts, None
    if not h["first_ms"]:
        h["first_ms"] = ts
        if fills == 0:
            h["run_start_ms"] = ts
    if a.get("run_start_ms"):  # v3 account stamps its own reset time
        h["run_start_ms"] = int(a["run_start_ms"])
    mark = float((t.get("state") or {}).get("mark") or 0)
    h["eq"].append((ts, float(a.get("equity") or START_EQUITY) - START_EQUITY, float(a.get("fees_paid") or 0)))
    side, entry = a.get("side") or "flat", float(a.get("entry") or 0)
    if full:
        if mark:
            h["marks"].append((ts, mark))
        if side in ("long", "short") and prev and prev["full"] and (prev["side"], prev["entry"]) != (side, entry):
            h["opens"].append({"ts_ms": ts, "side": side, "entry": entry})
    for x in a.get("history") or []:
        k = (int(x.get("ts_ms") or 0), x.get("side"), x.get("entry"))
        if k[0] and k not in h["trades"]:
            h["trades"][k] = x
    h["prev"] = {"fills": fills, "side": side, "entry": entry, "full": full}


def _hist_refresh() -> None:
    h = _hist
    try:
        st = LOG.stat()
    except OSError:
        return
    if not h["ready"] or st.st_ino != h["ino"] or st.st_size < h["off"]:
        _hist_reset(h)
        h.update(off=0, ino=st.st_ino, lines=0, parsed=0)
    boot = not h["ready"]
    full_from = st.st_size - HIST_FULL_BYTES
    with LOG.open("rb") as fh:
        fh.seek(h["off"])
        pos = h["off"]
        for line in fh:
            if not line.endswith(b"\n"):
                break  # half-written tail; pick it up next time
            full = (not boot) or pos >= full_from
            pos += len(line)
            h["lines"] += 1
            if not full and h["lines"] % HIST_STRIDE:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            h["parsed"] += 1
            _hist_ingest(h, t, full)
        h["off"] = pos
    if h["marks"]:
        cut = h["marks"][-1][0] - HIST_MARK_MS
        h["marks"] = [m for m in h["marks"] if m[0] >= cut]
        h["opens"] = [o for o in h["opens"] if o["ts_ms"] >= cut]
    h["ready"] = True
    h["checked"] = time.time()


def _thin(rows: list, n: int) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    return out + [rows[-1]]


def _history() -> dict:
    if not _hist_lock.acquire(blocking=False):
        return _hist.get("payload") or {"ok": False, "warming": True}
    try:
        if not _hist["ready"] or time.time() - _hist["checked"] > 2:
            _hist_refresh()
        h = _hist
        if not h.get("eq"):
            return {"ok": False, "reason": "no ticks with an account yet"}
        trades = sorted(h["trades"].values(), key=lambda x: int(x.get("ts_ms") or 0))
        out_trades = []
        for x in trades:
            qty, en, ex, pnl = (float(x.get(k) or 0) for k in ("qty", "entry", "exit", "pnl"))
            if x.get("gross") is not None:  # v3: exact per-leg split (taker entry, maker or taker exit)
                fee = float(x.get("fee_open_net") or 0) + float(x.get("fee_close_net") or 0)
                out_trades.append({**x, "gross": float(x["gross"]), "fee_net": fee})
            else:
                fee = (en + ex) * qty * FEE_SIDE  # open + close fee at the net 3 bps
                out_trades.append({**x, "gross": pnl + fee, "fee_net": fee})
        t0, t1 = h["eq"][0][0], h["eq"][-1][0]
        hours = max((t1 - t0) / 3.6e6, 1e-9)
        win = [t for t in out_trades if int(t.get("ts_ms") or 0) >= t0]  # closes inside the window
        pn = [t["pnl"] for t in win]
        pg = [t["gross"] for t in win]

        def _sharpe(v: list) -> float | None:
            if len(v) < 20:
                return None
            m = sum(v) / len(v)
            sd = (sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** 0.5
            return m / sd if sd else None

        peak, mdd, mdd_pct = -1e18, 0.0, 0.0
        for _, net, _f in h["eq"]:
            e = START_EQUITY + net
            peak = max(peak, e)
            if peak - e > mdd:
                mdd, mdd_pct = peak - e, (peak - e) / peak * 100
        payload = {
            "ok": True,
            "source": "logs/paper_ticks.jsonl",
            "start_equity": START_EQUITY,
            "fee_side_net": FEE_SIDE,
            "window": {"from_ms": t0, "to_ms": t1, "hours": round(hours, 2),
                       "run_start_ms": h["run_start_ms"] or None,
                       "lines": h["lines"], "parsed": h["parsed"], "stride": HIST_STRIDE},
            "equity": [[ts, round(net, 4), round(f, 4)] for ts, net, f in _thin(h["eq"], 900)],
            "marks": _thin(h["marks"], 2400),
            "opens": h["opens"][-400:],
            "trades": out_trades[-400:],
            "stats": {
                "legs": len(win),
                "legs_per_hour": len(win) / hours,
                "wins_window": sum(1 for v in pn if v >= 0),
                "sharpe_trade_net": _sharpe(pn),
                "sharpe_trade_gross": _sharpe(pg),
                "max_dd_usd": mdd,
                "max_dd_pct": mdd_pct,
            },
        }
        _hist["payload"] = payload
        return payload
    finally:
        _hist_lock.release()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASH), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/health":
            return self._json({"ok": True, "service": "jev-pulse", "port": PORT})
        if path == "/api/state":
            return self._json(self._state())
        if path == "/api/history":
            return self._json(_history())
        if path == "/api/candles":
            return self._json(self._candles())
        if path == "/api/pricehist":
            rng = (parse_qs(urlparse(self.path).query).get("range") or ["1h"])[0]
            return self._json(_pricehist(rng))
        return super().do_GET()

    def _json(self, payload: dict) -> None:
        raw = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _candles(self) -> dict:
        """1 s OHLC built by ws_public from Bitget public trades/ticker. ?since=<ms> for deltas."""
        try:
            since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
        except ValueError:
            since = 0
        live = ws_public.snapshot()
        bars = [b for b in live.get("candles") or [] if int(b["t"]) >= since]
        return {"ok": True, "granularity": "1s", "keep_s": CANDLE_KEEP, "ws": bool(live.get("ws")),
                "mark": live.get("mark"), "ts_ms": live.get("ts_ms"), "now_ms": int(time.time() * 1000),
                "candles": bars}

    def _state(self) -> dict:
        ticks = _tail_jsonl(LOG, 48)
        last = ticks[-1] if ticks else {}
        snap = _read_json(TAPE_FILE)
        live = ws_public.snapshot()
        if live.get("candles"):
            snap["candles"] = live["candles"][-STATE_CANDLES:]
            snap["granularity"] = "1s"
        if live.get("mark"):
            snap["mark"] = live["mark"]
            snap["funding_rate"] = live.get("funding") or snap.get("funding_rate")
            snap["bid"] = live.get("bid")
            snap["ask"] = live.get("ask")
            snap["live_mark"] = True
            snap["ws"] = bool(live.get("ws"))
            _live["mark"] = float(live["mark"])
            _live["funding"] = float(live.get("funding") or 0)
            _live["bid"] = float(live.get("bid") or 0)
            _live["ask"] = float(live.get("ask") or 0)
        file_acct = _read_json(ACCT)
        acct = file_acct
        if last.get("account"):
            last = dict(last)
            last["account"] = {**last["account"], **{k: acct.get(k, last["account"].get(k)) for k in ("equity", "upnl", "side", "qty", "streak", "realized", "last_settlement", "entry", "fills", "wins", "losses", "fees_paid", "volume_usdt", "gate_block", "move_bps", "last_action", "gross_realized", "fees_full", "fees_rebate", "fees_by_liq", "exits_by_kind", "round_trips", "funding_paid", "run_start_ms", "pos")}}
            last["account"]["history"] = list(acct.get("history") or [])
            if _live["mark"] and acct:
                qty = float(acct.get("qty") or 0)
                side = acct.get("side") or "flat"
                entry = float(acct.get("entry") or 0)
                upnl = 0.0
                if side == "long" and qty:
                    upnl = (_live["mark"] - entry) * qty
                elif side == "short" and qty:
                    upnl = (entry - _live["mark"]) * qty
                last["account"]["upnl"] = upnl
                last["account"]["equity"] = START_EQUITY + float(acct.get("realized") or 0) + upnl
        stream = []
        for t in ticks[-16:]:
            stance = t.get("stance") or {}
            wa = (t.get("writer_action") or {}).get("action") or "HOLD"
            events = (t.get("tape") or {}).get("events") or []
            ts = int(t.get("ts_ms") or 0)
            rg = _regime(t)
            jv = t.get("jev") or {}
            stream.append(
                {
                    "ts_ms": ts,
                    "kind": "heartbeat",
                    "exp_move_bps": jv.get("exp_move_bps"),
                    "close_now": jv.get("close_now"),
                    "gate": (t.get("writer_action") or {}).get("gate_block"),
                    "regime": rg["label"],
                    "score": rg["score"],
                    "stance": stance.get("label") or "Flat",
                    "pct": stance.get("pct") or 0,
                    "action": wa,
                }
            )
            for ev in events[:1]:
                stream.append(
                    {
                        "ts_ms": ts,
                        "kind": "print",
                        "side": ev.get("side"),
                        "usd": ev.get("usd"),
                        "bps": ev.get("bps"),
                    }
                )
        pulse = []
        cutoff = int(time.time() * 1000) - 180_000
        for t in ticks:
            ts = int(t.get("ts_ms") or 0)
            if ts < cutoff:
                continue
            pulse.append(
                {
                    "ts_ms": ts,
                    "stance": (t.get("stance") or {}).get("label") or "Flat",
                    "action": (t.get("writer_action") or {}).get("action"),
                }
            )
        wa = (last.get("writer_action") or {}) if last else {}
        acct = (last.get("account") or {}) if last else {}
        params = _read_json(PARAMS)
        v3 = None
        if last.get("strategy") == "v3-hf":
            eng = last.get("engine") or {}
            run_ms = max(1, int(time.time() * 1000) - int(file_acct.get("run_start_ms") or last.get("ts_ms") or 0))
            v3 = {
                "jev": last.get("jev") or {},
                "mode_q": last.get("mode_q"),
                "gate": {**(last.get("gate") or {}), "block": wa.get("gate_block"), "micro": wa.get("micro"),
                         "p_dir": wa.get("p_dir"), "edge": wa.get("edge"), "e_min": wa.get("e_min")},
                "engine": {**eng, "latency_ms": last.get("latency_ms"), "error": last.get("error"),
                           "model": last.get("model"), "cost_per_call": (last.get("usage") or {}).get("cost")},
                "pos": file_acct.get("pos"),
                "fees_by_liq": file_acct.get("fees_by_liq") or {},
                "exits_by_kind": file_acct.get("exits_by_kind") or {},
                "fees_full": file_acct.get("fees_full"),
                "fees_rebate": file_acct.get("fees_rebate"),
                "gross_realized": file_acct.get("gross_realized"),
                "round_trips": file_acct.get("round_trips"),
                "run_start_ms": file_acct.get("run_start_ms"),
                "rt_per_hour": (file_acct.get("round_trips") or 0) / (run_ms / 3.6e6),
                "fills_recent": list(reversed(list(file_acct.get("fill_log") or [])))[:30],
                "ws": (last.get("tape") or {}).get("ws"),
                "params": {k: params.get(k) for k in ("cadence_s", "tp_bps_min", "tp_bps_max", "sl_bps", "time_stop_s",
                                                      "passive_s", "close_passive_s", "exp_move_min", "micro_min",
                                                      "p_dir_min", "edge_min", "risk_veto", "close_now_thr",
                                                      "same_side_cooldown_s")},
            }
        return {
            "ok": True,
            "live_orders": False,
            "mode": "paper_offline",
            "start_equity": START_EQUITY,
            "tick": last,
            "tape": snap,
            "closed": list(reversed(list(file_acct.get("history") or [])))[:24],
            "regime": _regime(last) if last else {"label": "Range", "score": 50},
            "stream": list(reversed(stream[-24:])),
            "pulse": pulse,
            "ticks_n": len(ticks),
            "now_ms": int(time.time() * 1000),
            "studio": studio_sync.snapshot(),
            "v3": v3,
            "sources": {
                "mark": "bitget_public",
                "candles": "bitget_ws_1s_from_trades",
                "funding": "bitget_public",
                "book": "bitget_ws_books15" if v3 else "bitget_public",
                "trades": "bitget_ws_trade" if v3 else None,
                "stance": "typesafe_jev_openrouter",
                "writer_action": "jev_plus_local_gate",
                "equity": "mac_paper_sim_not_studio",
                "fills": "mac_paper_sim_not_studio",
                "studio_ledger_synced": False,  # paper NAV not on Playbook API
            },
            "honesty": {
                "banner": (f"LIVE book+trades · paper · Jev {params.get('cadence_s', 2.5)}s · maker TP "
                           f"{params.get('tp_bps_min', 7):g}-{params.get('tp_bps_max', 10):g} · stop {params.get('sl_bps', 4):g} · "
                           f"{params.get('time_stop_s', 120)}s · net fee T3/M1 bps") if v3
                          else "LIVE marks · paper · hold to 6 bps · fee 3 bps/side · no flip",
                "equity_note": ("Every fill is charged its net fee (taker 3 bps, maker 1 bps, after the 50% rebate)." if v3
                                else "New fills are net of rebated taker. Realized before this rule is gross."),
                "fills_note": (
                    f"{int(acct.get('fills') or 0)} fills · {acct.get('gate_block') or wa.get('action') or 'HOLD'}"
                    f" · fees ${float(acct.get('fees_paid') or 0):.2f}"
                ),
                "writer_action": wa.get("action") or "HOLD",
                "change_confidence": wa.get("change_confidence_score"),
                "gate_score_threshold": 0.35,
                "fills": int(acct.get("fills") or 0),
                "companion_interval_s": (last.get("cadence_s") or 5) if last else 5,
                "strategy": last.get("strategy") or "v1" if last else None,
            },
            "trades": list(reversed(list(file_acct.get("history") or [])))[:200],
        }


def main() -> int:
    DASH.mkdir(parents=True, exist_ok=True)
    ws_public.MAX_BARS = CANDLE_KEEP  # longer in-memory 1 s buffer; read at call time by ws_public
    ws_public.start()
    threading.Thread(target=_kline_loop, daemon=True).start()  # public REST 1m klines for 1H/6H
    threading.Thread(target=_history, daemon=True).start()  # warm the log reader
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"JEV PULSE http://{HOST}:{PORT}/  (paper desk, no OpenRouter)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
