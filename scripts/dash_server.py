#!/usr/bin/env python3
"""JEV PULSE desk v4 (multi-asset) — file-backed. No OpenRouter. No Bitget on the request path.

Reads what the engine writes (logs/paper_ticks.jsonl, logs/paper_decisions.jsonl,
logs/mids_live.json, .state/paper_account.json) plus its own Bitget PUBLIC socket
(ws_public: 1 s candles per symbol) and a background Bitget PUBLIC REST 1m-kline fetcher
(per symbol) for the 1H/6H views. The only write is the asset on/off control file
(.state/assets.json) via POST /api/assets, accepted only from this machine.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dash"
LOG = ROOT / "logs" / "paper_ticks.jsonl"
DECISIONS = ROOT / "logs" / "paper_decisions.jsonl"
MIDS = ROOT / "logs" / "mids_live.json"
TAPE_FILE = ROOT / "logs" / "tape_latest.json"
ACCT = ROOT / ".state" / "paper_account.json"
CONTROL_FILE = ROOT / ".state" / "assets.json"
PARAMS = ROOT / "scripts" / "hf_params.json"  # v4 strategy parameters (read-only here)
COACH_RUNS = ROOT / "logs" / "coach_runs.jsonl"        # v4.1 overnight coach: one line per run (HOLDs too)
COACH_STATE = ROOT / ".state" / "coach_state.json"     # v4.1 coach scheduler heartbeat / next run
CONTRACTS = ROOT / ".state" / "contracts.json"  # real tick / size step per symbol (engine cache)
sys.path.insert(0, str(ROOT / "scripts"))

import hf_control  # noqa: E402
import tape  # noqa: E402
import ws_public  # noqa: E402
import studio_sync  # noqa: E402

HOST = "0.0.0.0"
PORT = int(os.environ.get("JEV_DASH_PORT", "8790"))
START_EQUITY = 10_000.0
DEFAULT_UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "LTCUSDT", "XAUUSDT", "XAGUSDT"]
CANDLE_KEEP = 1800  # 1 s candles kept in memory per symbol for the 1S view (30 min). Nothing written.
# --- /api/pricehist: 1H / 6H per symbol: Bitget PUBLIC REST 1m klines (last-trade closes, same
# price type as the 1 s WS candles), fetched by ONE background thread (each symbol every
# KLINE_EVERY_S) and cached in memory, then merged with the live 1 s buffer. Never on the request path.
KLINE_EVERY_S = 30
PRICE_RANGES = {"1h": (3600, 60), "6h": (21600, 60)}  # span s, point res s (1 m both: REST 1m + live 1 s closes bucketed to 1 m, evenly spaced)
LOCAL_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_kl: dict = {}
_kl_lock = threading.Lock()
_ctl_lock = threading.Lock()


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def universe() -> list[str]:
    u = _read_json(PARAMS).get("universe")
    return list(u) if isinstance(u, list) and u else list(DEFAULT_UNIVERSE)


def _tail_jsonl(path: Path, n: int = 48) -> list[dict]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            buf = b""
            while size > 0 and buf.count(b"\n") <= n:
                step = min(262144, size)
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


def _kline_loop(syms: list[str]) -> None:
    """Background: refresh the cached 1m kline closes of every symbol. Keeps the last good rows
    of a symbol if its fetch fails (flagged in the response)."""
    while True:
        t0 = time.time()
        for s in syms:
            with _kl_lock:
                k = _kl.setdefault(s, {"rows": [], "ok": False, "err": "", "fetched_ms": 0, "tries": 0})
            try:
                data = tape._get(f"/api/v2/mix/market/candles?symbol={s}&productType=USDT-FUTURES&granularity=1m&limit=400",
                                 timeout=8.0) or []
                rows = {}
                for r in data:
                    t, c = int(r[0]) // 1000, float(r[4])
                    if t > 0 and c > 0:
                        rows[t] = c
                if not rows:
                    raise ValueError("empty kline response")
                with _kl_lock:
                    k.update(rows=sorted(rows.items()), ok=True, err="", fetched_ms=int(time.time() * 1000))
            except Exception as exc:
                with _kl_lock:
                    k.update(ok=False, err=f"{type(exc).__name__}: {str(exc)[:120]}")
            with _kl_lock:
                k["tries"] += 1
            time.sleep(0.4)
        time.sleep(max(1.0, KLINE_EVERY_S - (time.time() - t0)))


def _pricehist(rng: str, sym: str) -> dict:
    """Real closes only: Bitget REST 1m klines up to where the live 1 s buffer starts, then the
    live 1 s closes bucketed to the range resolution. No interpolation."""
    span, step = PRICE_RANGES.get(rng, PRICE_RANGES["1h"])
    now = time.time()
    live = ws_public.snapshot(sym)
    bars = sorted((int(b["t"]) // 1000, float(b["c"])) for b in live.get("candles") or [] if float(b.get("c") or 0) > 0)
    with _kl_lock:
        k = _kl.get(sym) or {"rows": [], "ok": False, "err": "not fetched yet", "fetched_ms": 0, "tries": 0}
        kl = list(k["rows"])
        kmeta = {"ok": k["ok"], "err": k["err"], "fetched_ms": k["fetched_ms"], "tries": k["tries"],
                 "age_s": round(now - k["fetched_ms"] / 1000, 1) if k["fetched_ms"] else None}
    live_from = -(-bars[0][0] // step) * step if bars else None
    start = int(now) - span
    pts: list = [[t, c] for t, c in kl if t >= start and (live_from is None or t < live_from)]
    n_kl = len(pts)
    buckets: dict = {}
    for t, c in bars:
        if t >= start and live_from is not None and t >= live_from:
            buckets[t - t % step] = c
    last_kl = pts[-1][0] if pts else -1
    pts.extend([t, c] for t, c in sorted(buckets.items()) if t > last_kl)
    segs = []
    if n_kl:
        segs.append({"source": "bitget_rest_klines_1m", "from_ms": pts[0][0] * 1000, "to_ms": pts[n_kl - 1][0] * 1000, "n": n_kl, "res_s": 60})
    if len(pts) > n_kl:
        segs.append({"source": "ws_1s_closes", "from_ms": pts[n_kl][0] * 1000, "to_ms": pts[-1][0] * 1000, "n": len(pts) - n_kl, "res_s": step})
    cov = (pts[-1][0] - pts[0][0]) if len(pts) > 1 else 0
    return {"ok": bool(pts), "symbol": sym, "range": rng if rng in PRICE_RANGES else "1h", "span_s": span, "res_s": step,
            "coverage_s": cov, "points": pts, "segments": segs, "kline": kmeta, "ws": bool(live.get("ws")),
            "live_buffer_from_ms": bars[0][0] * 1000 if bars else None, "now_ms": int(now * 1000)}


# --- /api/history: read-only equity + trades (all assets) from paper_ticks.jsonl ---
HIST_STRIDE = 10
HIST_FULL_BYTES = 60 << 20
HIST_MARK_MS = 6 * 3600 * 1000
_hist_lock = threading.Lock()
_hist: dict = {"off": 0, "ino": None, "ready": False, "checked": 0.0, "lines": 0, "parsed": 0}


def _hist_reset(h: dict) -> None:
    h.update(eq=[], opens={}, trades={}, prev=None, run_start_ms=0, first_ms=0)


def _hist_ingest(h: dict, t: dict, full: bool) -> None:
    a = t.get("account") or {}
    ts = int(t.get("ts_ms") or 0)
    if not a or not ts:
        return
    fills = int(a.get("fills") or 0)
    prev = h["prev"]
    if prev is not None and fills < prev["fills"]:  # paper account was reset: new run
        _hist_reset(h)
        prev = None
    if not h["first_ms"]:
        h["first_ms"] = ts
    if a.get("run_start_ms"):
        h["run_start_ms"] = int(a["run_start_ms"])
    h["eq"].append((ts, float(a.get("equity") or START_EQUITY) - START_EQUITY, float(a.get("fees_paid") or 0)))
    for s, p in (a.get("positions") or {}).items():
        k = (s, int(p.get("opened_ms") or 0))
        if k[1] and k not in h["opens"]:
            h["opens"][k] = {"symbol": s, "ts_ms": k[1], "side": "long" if (p.get("dir") or 0) > 0 else "short",
                             "entry": p.get("entry")}
    for x in a.get("history") or []:
        k = (int(x.get("ts_ms") or 0), x.get("symbol"), x.get("side"), x.get("entry"))
        if k[0] and k not in h["trades"]:
            h["trades"][k] = x
    h["prev"] = {"fills": fills}


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
                break
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
    if h["eq"]:
        cut = h["eq"][-1][0] - HIST_MARK_MS
        h["opens"] = {k: v for k, v in h["opens"].items() if v["ts_ms"] >= cut}
    h["ready"] = True
    h["checked"] = time.time()


def _thin(rows: list, n: int) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)] + [rows[-1]]


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
        out_trades = [{**x, "gross": float(x.get("gross") or 0),
                       "fee_net": float(x.get("fee_open_net") or 0) + float(x.get("fee_close_net") or 0)} for x in trades]
        t0, t1 = h["eq"][0][0], h["eq"][-1][0]
        hours = max((t1 - t0) / 3.6e6, 1e-9)
        win = [t for t in out_trades if int(t.get("ts_ms") or 0) >= t0]
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
            "ok": True, "source": "logs/paper_ticks.jsonl", "start_equity": START_EQUITY,
            "window": {"from_ms": t0, "to_ms": t1, "hours": round(hours, 2), "run_start_ms": h["run_start_ms"] or None,
                       "lines": h["lines"], "parsed": h["parsed"], "stride": HIST_STRIDE},
            "equity": [[ts, round(net, 4), round(f, 4)] for ts, net, f in _thin(h["eq"], 900)],
            "opens": sorted(h["opens"].values(), key=lambda o: o["ts_ms"])[-400:],
            "trades": out_trades[-400:],
            "stats": {"legs": len(win), "legs_per_hour": len(win) / hours, "wins_window": sum(1 for v in pn if v > 0),
                      "sharpe_trade_net": _sharpe(pn), "sharpe_trade_gross": _sharpe(pg),
                      "max_dd_usd": mdd, "max_dd_pct": mdd_pct},
        }
        _hist["payload"] = payload
        return payload
    finally:
        _hist_lock.release()


def _mid(q: dict) -> float:
    b, a = float(q.get("bid") or 0), float(q.get("ask") or 0)
    return (b + a) / 2 if b and a and a >= b else float(q.get("mark") or 0)


def _grid() -> dict:
    """Small per-asset charts: engine book mids every 5 s (last 30 min), entries/exits and the
    open entry per symbol. Read from files the engine writes; nothing fetched."""
    m = _read_json(MIDS)
    acct = _read_json(ACCT)
    now = int(time.time() * 1000)
    span = int(m.get("span_s") or 1800) * 1000
    marks: dict = {}
    for x in acct.get("history") or []:
        s = x.get("symbol")
        if not s or int(x.get("ts_ms") or 0) < now - span:
            continue
        marks.setdefault(s, []).append({"t": int(x["opened_ms"]) // 1000, "kind": "entry", "side": x.get("side"), "px": x.get("entry"),
                                        "win": float(x.get("pnl") or 0) > 0})
        marks[s].append({"t": int(x["ts_ms"]) // 1000, "kind": "exit", "side": x.get("side"), "px": x.get("exit"),
                         "win": float(x.get("pnl") or 0) > 0, "exit_kind": x.get("exit_kind"), "pnl": x.get("pnl")})
    opens = {}
    for s, p in (acct.get("positions") or {}).items():
        opens[s] = {"side": "long" if p.get("dir", 0) > 0 else "short", "entry": p.get("entry"), "tp_px": p.get("tp_px"),
                    "sl_px": p.get("sl_px"), "opened_ms": p.get("opened_ms")}
        marks.setdefault(s, []).append({"t": int(p.get("opened_ms") or 0) // 1000, "kind": "entry", "side": opens[s]["side"],
                                        "px": p.get("entry"), "open": True})
    age = (now - int(m.get("ts_ms") or 0)) / 1000 if m.get("ts_ms") else None
    return {"ok": bool(m.get("mids")), "source": m.get("source") or "engine_book_mids", "step_s": m.get("step_s"),
            "span_s": m.get("span_s"), "age_s": round(age, 1) if age is not None else None,
            "mids": m.get("mids") or {}, "marks": marks, "opens": opens, "now_ms": now}


def _coach() -> dict:
    """Overnight coach card: last applied review, last dry run (labelled), scheduler, overlay. Real files only."""
    runs = _tail_jsonl(COACH_RUNS, 60)
    def slim(r):
        if not r:
            return None
        o = {k: r.get(k) for k in ("ts_ms", "time_local", "run_id", "mode", "result", "hold_reason", "model_used",
                                   "cost_usd", "applied_changes", "held_assets", "rejected", "coach_summary", "totals",
                                   "applied_to_overlay", "input_summary_sha256")}
        o["models_tried"] = [{k: m.get(k) for k in ("model", "ok", "error", "latency_s", "cost_usd")} for m in r.get("models_tried") or []]
        return o
    last_apply = next((r for r in reversed(runs) if r.get("mode") == "apply"), None)
    last_dry = next((r for r in reversed(runs) if r.get("mode") == "dry_run"), None)
    st = _read_json(COACH_STATE)
    alive = False
    pid = st.get("pid")
    if isinstance(pid, int) and not st.get("stopped_ms") and time.time() * 1000 - float(st.get("heartbeat_ms") or 0) < 180_000:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    ov_view = None
    try:
        import hf_overlay
        P = _read_json(PARAMS)
        ov, ok, err = hf_overlay.load(hf_overlay.OVERLAY_PATH, universe(), P)
        dflt = hf_overlay.default_overlay(universe(), P)
        diffs = [{"symbol": sy, "key": k, "value": v, "default": dflt["assets"][sy][k]}
                 for sy, kv in ov["assets"].items() for k, v in kv.items() if abs(v - dflt["assets"][sy][k]) > 1e-9]
        diffs += [{"symbol": None, "key": k, "value": v, "default": dflt["global"][k]}
                  for k, v in ov["global"].items() if abs(v - dflt["global"][k]) > 1e-9]
        ov_view = {"file_ok": ok, "error": err or None, "exists": hf_overlay.OVERLAY_PATH.exists(), "source": ov.get("source"),
                   "updated_ms": ov.get("updated_ms"), "run_id": ov.get("run_id"), "model": ov.get("model"),
                   "hash": hf_overlay.overlay_hash(ov), "non_default": diffs}
    except Exception as exc:  # noqa: BLE001
        ov_view = {"file_ok": False, "error": type(exc).__name__}
    return {"ok": True, "now_ms": int(time.time() * 1000),
            "models": {"primary": "xiaomi/mimo-v2.6-pro", "fallback": "deepseek/deepseek-v4.1-flash"},
            "scheduler": {"alive": alive, "pid": pid if alive else None, "at_local": st.get("at_local") or "03:07",
                          "next_run_ms": st.get("next_run_ms") if alive else None, "heartbeat_ms": st.get("heartbeat_ms"),
                          "mechanism": st.get("mechanism"), "last_loop_run": st.get("last_run")},
            "last_apply": slim(last_apply), "last_dry_run": slim(last_dry), "runs_in_log": len(runs),
            "overlay": ov_view}


def _control_view(last: dict) -> dict:
    u = universe()
    ctl, ok, err = hf_control.read(CONTROL_FILE, u)
    eng = last.get("control") or {}
    assets = last.get("assets") or {}
    return {"ok": True, "universe": u, "file": ctl, "file_ok": ok, "file_error": err or None,
            "engine_seq": eng.get("seq"), "engine_file_ok": eng.get("file_ok"), "engine_error": eng.get("error"),
            "engine_enabled": eng.get("enabled"), "effective": {s: (a or {}).get("eff") for s, a in assets.items()},
            "engine_tick_ms": last.get("ts_ms"), "pending": (ctl.get("seq") or 0) > (eng.get("seq") or 0)}


def _state() -> dict:
    ticks = _tail_jsonl(LOG, 40)
    last = ticks[-1] if ticks else {}
    now = int(time.time() * 1000)
    acct_file = _read_json(ACCT)
    q = ws_public.quotes()
    u = universe()
    assets_eng = last.get("assets") or {}
    by_asset = acct_file.get("by_asset") or {}
    positions = acct_file.get("positions") or {}
    specs = {s: {k: v for k, v in sp.items() if k in ("tick", "price_dp", "size_step", "min_qty")}
             for s, sp in (_read_json(CONTRACTS).get("specs") or {}).items()}
    # live mark-to-market of open positions from the desk's own public socket (mid of bid/ask)
    upnl_tot = 0.0
    pos_out = []
    for s, p in positions.items():
        m = _mid(q.get(s) or {}) or float(p.get("mark") or 0)
        d, qty, en = int(p.get("dir") or 0), float(p.get("qty") or 0), float(p.get("entry") or 0)
        u_ = (m - en) * qty * d if m and en else float(p.get("upnl") or 0)
        upnl_tot += u_
        pos_out.append({**p, "live_mark": m, "live_upnl": u_, "live_move_bps": (m / en - 1) * 1e4 * d if m and en else None})
    pos_out.sort(key=lambda p: p.get("opened_ms") or 0)
    realized = float(acct_file.get("realized") or 0)
    equity = START_EQUITY + realized + upnl_tot
    ctl = _control_view(last)
    assets = []
    for s in u:
        a = dict(assets_eng.get(s) or {"symbol": s, "status": "no_data", "eff": None})
        b = by_asset.get(s) or {}
        qq = q.get(s) or {}
        pu = next((p["live_upnl"] for p in pos_out if p.get("symbol") == s), 0.0)
        a.update(symbol=s, clicked=bool((ctl["file"]["assets"] or {}).get(s, True)),
                 live_px=_mid(qq) or a.get("mid"), last_px=qq.get("mark") or None, chg_24h=qq.get("chg_24h"),
                 quote_age_s=round((now - int(qq.get("rx_ms") or 0)) / 1000, 1) if qq.get("rx_ms") else None,
                 pnl_realized=float(b.get("realized") or 0), pnl_upnl=pu, pnl=float(b.get("realized") or 0) + pu,
                 trades=int(b.get("round_trips") or 0), wins=int(b.get("wins") or 0), fees=float(b.get("fees_paid") or 0),
                 volume=float(b.get("volume_usdt") or 0), exits=b.get("exits_by_kind") or {},
                 price_dp=(specs.get(s) or {}).get("price_dp"), tick=(specs.get(s) or {}).get("tick"))
        assets.append(a)
    matrix = sorted([a for a in assets if a.get("room") is not None], key=lambda a: -a["room"]) + \
        [a for a in assets if a.get("room") is None]
    stream = []
    for t in ticks[-16:]:
        for d in t.get("decisions") or []:
            w = d.get("writer_action") or {}
            j = d.get("jev") or {}
            st = d.get("stance") or {}
            stream.append({"ts_ms": int(t.get("ts_ms") or 0), "kind": "decision", "symbol": d.get("symbol"),
                           "mode": d.get("mode_q"), "action": w.get("action") or "HOLD", "gate": w.get("gate_block"),
                           "exp_move_bps": j.get("exp_move_bps"), "e_min": d.get("e_min"), "close_now": j.get("close_now"),
                           "stance": st.get("label") or "Flat", "pct": st.get("pct") or 0, "latency_ms": d.get("latency_ms")})
        for ev in ((t.get("tape") or {}).get("events") or [])[-1:]:
            stream.append({"ts_ms": int(ev.get("ts") or t.get("ts_ms") or 0), "kind": "print", "symbol": ev.get("symbol"),
                           "side": ev.get("side"), "usd": ev.get("usd")})
    seen = set()
    uniq = []
    for r in sorted(stream, key=lambda r: -r["ts_ms"]):
        k = (r["ts_ms"], r["kind"], r.get("symbol"), r.get("mode"))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    pulse = [{"ts_ms": int(t.get("ts_ms") or 0), "stance": (t.get("stance") or {}).get("label") or "Flat",
              "symbol": (t.get("stance") or {}).get("symbol"), "action": (t.get("writer_action") or {}).get("action"),
              "calls": len([d for d in t.get("decisions") or [] if d.get("latency_ms") is not None])}
             for t in ticks if int(t.get("ts_ms") or 0) >= now - 180_000]
    params = _read_json(PARAMS)
    eng = last.get("engine") or {}
    run_ms = max(1, now - int(acct_file.get("run_start_ms") or last.get("ts_ms") or now))
    acct = {k: acct_file.get(k) for k in ("realized", "gross_realized", "fees_paid", "fees_full", "fees_rebate", "fees_by_liq",
                                          "exits_by_kind", "soft_exits", "funding_paid", "volume_usdt", "fills", "round_trips",
                                          "wins", "losses", "streak", "last_settlement", "last_close", "run_start_ms",
                                          "gate_block", "last_action", "open_count")}
    acct.update(equity=equity, upnl=upnl_tot)
    closed = list(reversed(list(acct_file.get("history") or [])))
    v4 = bool(str(last.get("strategy") or "").startswith("v4"))
    return {
        "ok": True, "live_orders": False, "mode": "paper_offline", "start_equity": START_EQUITY, "now_ms": now,
        "strategy": last.get("strategy"), "strategy_version": last.get("strategy_version"), "v4": v4,
        "tick": {k: last.get(k) for k in ("ts_ms", "symbol", "symbols_asked", "mode_q", "jev", "writer_action", "stance",
                                          "latency_ms", "error", "model", "cadence_s", "tick_cost_usd")},
        "account": acct, "positions": pos_out, "assets": assets, "matrix": [a["symbol"] for a in matrix],
        "control": ctl, "budget": last.get("budget"), "engine": {**eng, "latency_ms": last.get("latency_ms"),
                                                              "error": last.get("error"), "model": last.get("model")},
        "feed": (last.get("tape") or {}).get("feed"), "ws_desk": ws_public.meta(),
        "gate": last.get("gate"), "rt_per_hour": (acct_file.get("round_trips") or 0) / (run_ms / 3.6e6),
        "closed": closed[:40], "trades": closed[:200], "fills_recent": list(reversed(list(acct_file.get("fill_log") or [])))[:30],
        "stream": uniq[:40], "pulse": pulse, "ticks_n": len(ticks), "studio": studio_sync.snapshot(),
        "specs": specs,
        "params": {k: params.get(k) for k in ("cadence_s", "universe", "clip_usdt", "max_open_per_asset", "max_open_total",
                                              "jev_top_n", "jev_budget_usd_h", "cost_rt_bps", "room_min", "depth_ref_usd",
                                              "idle_range_bps", "idle_no_trade_s", "tp_bps_min", "tp_bps_max", "tp_range_k",
                                              "sl_mult", "sl_bps_min", "sl_bps_max", "time_cap_min_s", "time_cap_max_s",
                                              "soft_exit_window_s", "exp_move_min", "micro_min", "p_dir_min", "edge_min",
                                              "risk_veto", "close_now_thr", "spread_max_bps", "same_side_cooldown_s")},
        "sources": {"prices": "bitget_public_ws", "book": "bitget_ws_books15 (engine)", "stance": "typesafe_jev_openrouter",
                    "equity": "mac_paper_sim_not_studio", "fills": "mac_paper_sim_not_studio", "studio_ledger_synced": False},
        "honesty": {
            "banner": (f"v4.1 MULTI-ASSET · {len(u)} Bitget perps · paper · Jev {params.get('cadence_s', 2.5)}s top-{params.get('jev_top_n', 2)} "
                       f"by room · ≤{params.get('max_open_total', 3)} open · TP {params.get('tp_bps_min', 5):g}-{params.get('tp_bps_max', 25):g} bps (R) · "
                       f"stop {params.get('sl_mult', .8):g}×TP · net fee T3/M1"),
            "equity_note": "Every fill is charged its net fee (taker 3 bps, maker 1 bps, after the 50% rebate).",
            "companion_interval_s": params.get("cadence_s", 2.5),
        },
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASH), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _q(self, k: str, d: str = "") -> str:
        return (parse_qs(urlparse(self.path).query).get(k) or [d])[0]

    def _sym(self) -> str | None:
        s = self._q("symbol", "BTCUSDT")
        return s if s in universe() else None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/health":
            return self._json({"ok": True, "service": "jev-pulse", "version": "v4.1", "port": PORT})
        if path == "/api/state":
            return self._json(_state())
        if path == "/api/history":
            return self._json(_history())
        if path == "/api/grid":
            return self._json(_grid())
        if path == "/api/coach":
            return self._json(_coach())
        if path == "/api/assets":
            last = (_tail_jsonl(LOG, 1) or [{}])[-1]
            return self._json(_control_view(last))
        if path in ("/api/candles", "/api/pricehist"):
            sym = self._sym()
            if not sym:
                return self._json({"ok": False, "error": "unknown symbol"}, 400)
            if path == "/api/candles":
                return self._json(self._candles(sym))
            return self._json(_pricehist(self._q("range", "1h"), sym))
        if path.startswith("/api/"):
            return self._json({"ok": False, "error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/assets":
            return self._json({"ok": False, "error": "not found"}, 404)
        # local-only control: loopback peer, local Host (no DNS rebinding), same-origin page
        # (Origin, if sent, must be this desk), and a custom header a cross-site form cannot send.
        allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.client_address[0] not in LOCAL_HOSTS:
            return self._json({"ok": False, "error": "toggles are accepted only from this Mac (localhost)"}, 403)
        if (self.headers.get("Host") or "") not in allowed_hosts:
            return self._json({"ok": False, "error": "bad Host"}, 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in allowed_hosts}:
            return self._json({"ok": False, "error": "cross-origin toggle refused"}, 403)
        if self.headers.get("X-Jev-Control") != "1":
            return self._json({"ok": False, "error": "missing X-Jev-Control header"}, 403)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json({"ok": False, "error": "Content-Type must be application/json"}, 415)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > 512:
            return self._json({"ok": False, "error": "body must be 1-512 bytes"}, 400)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json({"ok": False, "error": "invalid JSON"}, 400)
        ok, val, err = hf_control.validate_request(body, universe())
        if not ok:
            return self._json({"ok": False, "error": err}, 400)
        sym, en = val
        with _ctl_lock:
            try:
                ctl = hf_control.apply_toggle(CONTROL_FILE, universe(), sym, en)
            except (OSError, ValueError) as exc:
                return self._json({"ok": False, "error": f"write failed: {type(exc).__name__}"}, 500)
        return self._json({"ok": True, "symbol": sym, "enabled": en, "seq": ctl["seq"], "assets": ctl["assets"],
                           "note": "written; the engine applies it on its next tick (<= 2.5 s)"})

    def _json(self, payload: dict, code: int = 200) -> None:
        raw = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _candles(self, sym: str) -> dict:
        """1 s OHLC per symbol built by ws_public from Bitget public trades/ticker. ?since=<ms>."""
        try:
            since = int(self._q("since", "0"))
        except ValueError:
            since = 0
        live = ws_public.snapshot(sym)
        bars = [b for b in live.get("candles") or [] if int(b["t"]) >= since]
        return {"ok": True, "symbol": sym, "granularity": "1s", "keep_s": CANDLE_KEEP, "ws": bool(live.get("ws")),
                "mark": live.get("mark"), "ts_ms": live.get("ts_ms"), "now_ms": int(time.time() * 1000), "candles": bars}


def main() -> int:
    DASH.mkdir(parents=True, exist_ok=True)
    u = universe()
    ws_public.MAX_BARS = CANDLE_KEEP
    ws_public.start(u)
    threading.Thread(target=_kline_loop, args=(u,), daemon=True).start()  # public REST 1m klines for 1H/6H
    threading.Thread(target=_history, daemon=True).start()  # warm the log reader
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"JEV PULSE v4.1 http://{HOST}:{PORT}/  (paper desk, no OpenRouter; toggles localhost-only)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
