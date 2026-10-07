#!/usr/bin/env python3
"""Bitget PUBLIC market feed for the v4 multi-asset HF paper desk (USDT-FUTURES perps).

One WebSocket carries books15 (top-15 snapshot every ~150 ms), trade (every print with
taker side) and ticker (funding, mark, index) for every symbol in the universe. A slow
REST poll adds the long/short account ratio per symbol. No keys, no private channels,
no orders.

`Feed` is one symbol (a plain object: the simulator, the tests and the replay tool drive
it from recorded messages exactly like the live socket does). `MultiFeed` owns one Feed
per symbol, the socket, reconnects and per-symbol staleness. Real tick size and size step
per symbol come from /api/v2/mix/market/contracts (`load_contracts`).
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import urllib.request
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

WS_URL = "wss://ws.bitget.com/v2/ws/public"
REST = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
SYMBOL = "BTCUSDT"
TICK = 0.1
CHANNELS = ("books15", "trade", "ticker")


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def tick_decimals(tick: float) -> int:
    return max(0, -Decimal(str(tick)).normalize().as_tuple().exponent)


def _get(path: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(REST + path, headers={"User-Agent": "jev-pulse-hf/4"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    if str(body.get("code")) not in ("00000", "200"):
        raise RuntimeError(f"bitget {path.split('?')[0]}: {body.get('code')} {body.get('msg')}")
    return body.get("data")


def parse_contract(row: dict) -> dict[str, Any]:
    """Tick = priceEndStep x 10^-pricePlace; size step = sizeMultiplier (Bitget v2 contract spec)."""
    pp = int(_f(row.get("pricePlace"), 1))
    step = int(_f(row.get("priceEndStep"), 1)) or 1
    tick = float(Decimal(step) * (Decimal(10) ** -pp))
    return {"symbol": row["symbol"], "tick": tick, "price_dp": tick_decimals(tick),
            "size_step": _f(row.get("sizeMultiplier")), "min_qty": _f(row.get("minTradeNum")),
            "min_usdt": _f(row.get("minTradeUSDT"), 5.0), "status": row.get("symbolStatus"),
            "maker_fee_list": _f(row.get("makerFeeRate")), "taker_fee_list": _f(row.get("takerFeeRate"))}


def load_contracts(symbols: list[str], cache: Path | None = None, tries: int = 3) -> tuple[dict, str]:
    """Real per-symbol specs from Bitget public REST; falls back to the last cached copy.
    Returns (specs, source). Raises if a symbol is missing from both."""
    specs: dict[str, dict] = {}
    src = "bitget_rest"
    err = ""
    for i in range(tries):
        try:
            rows = _get(f"/api/v2/mix/market/contracts?productType={PRODUCT}", timeout=10) or []
            by = {r.get("symbol"): r for r in rows}
            specs = {s: parse_contract(by[s]) for s in symbols if s in by}
            if specs:
                break
        except Exception as exc:  # network / API error: retry, then cache
            err = f"{type(exc).__name__}: {str(exc)[:80]}"
            time.sleep(1.5 * (i + 1))
    if cache is not None:
        if specs and len(specs) == len(symbols):
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_suffix(".tmp")
                tmp.write_text(json.dumps({"fetched_ms": int(time.time() * 1000), "specs": specs}))
                tmp.replace(cache)
            except OSError:
                pass
        else:
            try:
                old = json.loads(cache.read_text()).get("specs") or {}
                for s in symbols:
                    if s not in specs and s in old:
                        specs[s] = old[s]
                        src = "cache"
            except (OSError, json.JSONDecodeError):
                pass
    missing = [s for s in symbols if s not in specs]
    if missing:
        raise RuntimeError(f"no contract spec for {missing} ({err or 'not listed'})")
    return specs, src


class Feed:
    def __init__(self, symbol: str = SYMBOL, tick: float = TICK, mid_keep_s: int = 1800) -> None:
        self.symbol = symbol
        self.tick = tick
        self.dp = tick_decimals(tick)
        self.lock = threading.RLock()
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        self.book_ts = 0
        self.book_rx = 0
        self.trades: deque = deque()  # (ts_ms, px, sz, +1 buy / -1 sell)
        self.last_trade_px = 0.0
        self.last_trade_ts = 0
        self.last_trade_rx = 0
        self.mids: deque = deque()  # (ts_ms, mid) sampled on book updates, 330 s
        self.bars: deque = deque(maxlen=180)  # 1 s OHLCV from trades
        self.mid1s: deque = deque(maxlen=mid_keep_s)  # (sec_ms, last book mid in that second)
        self.funding = 0.0
        self.mark = 0.0
        self.index = 0.0
        self.last_px = 0.0
        self.chg_24h: float | None = None
        self.next_funding_ms = 0
        self.oi = 0.0
        self.oi_hist: deque = deque(maxlen=40)  # (ts_ms, oi)
        self.ls_ratio = 1.0
        self.ls_ok = False
        self.ws_up = False
        self.connects = 0
        self.msgs = {"books15": 0, "trade": 0, "ticker": 0}
        self.last_rx = 0
        self.trade_listeners: list[Callable[[int, float, float, int], None]] = []
        self.book_listeners: list[Callable[[], None]] = []
        self.clock: Callable[[], int] = lambda: int(time.time() * 1000)

    # ---------------------------------------------------------------- ingest
    def on_message(self, m: dict, rx_ms: int | None = None) -> None:
        arg = m.get("arg") or {}
        ch = arg.get("channel")
        data = m.get("data")
        if not data or ch not in self.msgs:
            return
        self.msgs[ch] += 1
        now = rx_ms or self.clock()
        self.last_rx = now
        if ch == "books15":
            row = data[0]
            bids = [(_f(p), _f(s)) for p, s in row.get("bids") or []]
            asks = [(_f(p), _f(s)) for p, s in row.get("asks") or []]
            if not bids or not asks:
                return
            with self.lock:
                self.bids, self.asks = bids, asks
                self.book_ts = int(_f(row.get("ts"), now))
                self.book_rx = now
                mid = (bids[0][0] + asks[0][0]) / 2
                self.mids.append((self.book_ts, mid))
                self._add_mid1s(self.book_ts, mid)
                cut = self.book_ts - 330_000
                while self.mids and self.mids[0][0] < cut:
                    self.mids.popleft()
            for cb in list(self.book_listeners):
                cb()
        elif ch == "trade":
            rows = data if isinstance(data, list) else [data]
            if m.get("action") == "snapshot":
                rows = sorted(rows, key=lambda r: _f(r.get("ts")))
                # history seed: keep for flow windows, do not trigger fills
                with self.lock:
                    for r in rows:
                        self._add_trade(int(_f(r.get("ts"))), _f(r.get("price")), _f(r.get("size")),
                                        1 if str(r.get("side")).lower() == "buy" else -1)
                return
            rows = sorted(rows, key=lambda r: _f(r.get("ts")))
            for r in rows:
                ts, px, sz = int(_f(r.get("ts"), now)), _f(r.get("price")), _f(r.get("size"))
                sd = 1 if str(r.get("side")).lower() == "buy" else -1
                if not px:
                    continue
                with self.lock:
                    self._add_trade(ts, px, sz, sd)
                    self.last_trade_rx = now
                for cb in list(self.trade_listeners):
                    cb(ts, px, sz, sd)
        elif ch == "ticker":
            r = data[0]
            with self.lock:
                self.funding = _f(r.get("fundingRate"), self.funding)
                self.mark = _f(r.get("markPrice"), self.mark)
                self.index = _f(r.get("indexPrice"), self.index)
                self.last_px = _f(r.get("lastPr"), self.last_px)
                c = r.get("change24h")
                if c not in (None, ""):
                    self.chg_24h = _f(c)
                self.next_funding_ms = int(_f(r.get("nextFundingTime"), self.next_funding_ms))
                hold = _f(r.get("holdingAmount"))
                if hold:
                    self.oi = hold
                    if not self.oi_hist or now - self.oi_hist[-1][0] >= 15_000:
                        self.oi_hist.append((now, hold))

    def _add_mid1s(self, ts: int, mid: float) -> None:
        sec = ts // 1000 * 1000
        if self.mid1s and self.mid1s[-1][0] == sec:
            self.mid1s[-1] = (sec, mid)
            return
        if self.mid1s and sec < self.mid1s[-1][0]:
            return
        if self.mid1s:  # carry the last mid through seconds with no book update
            t, m = self.mid1s[-1][0] + 1000, self.mid1s[-1][1]
            while t < sec and sec - t <= 60_000:
                self.mid1s.append((t, m))
                t += 1000
        self.mid1s.append((sec, mid))

    def range_stats(self, horizon_s: int = 120, lookback_s: int = 600, step_s: int = 5,
                    min_samples_s: int = 180, now_ms: int | None = None) -> dict[str, Any]:
        """v3.1 realised movement (unchanged in v4, run per asset): median high-low range (bps
        of mid) of 1 s mids over rolling `horizon_s` windows (every `step_s`) inside the last
        `lookback_s`. range_bps is None until `min_samples_s` seconds of 1 s mids exist."""
        now = now_ms or self.clock()
        with self.lock:
            pts = [m for t, m in self.mid1s if t >= now - lookback_s * 1000]
        n = len(pts)
        out: dict[str, Any] = {"range_bps": None, "samples_s": n, "windows": 0,
                               "horizon_s": horizon_s, "lookback_s": lookback_s}
        if n < max(min_samples_s, 10):
            return out
        h = min(horizon_s, n - 1)
        rs = []
        for i in range(0, n - h, max(1, step_s)):
            w = pts[i:i + h + 1]
            lo = min(w)
            if lo > 0:
                rs.append((max(w) - lo) / lo * 1e4)
        if not rs:
            return out
        rs.sort()
        med = rs[len(rs) // 2] if len(rs) % 2 else (rs[len(rs) // 2 - 1] + rs[len(rs) // 2]) / 2
        if h < horizon_s:  # short history: scale the shorter-window range up by sqrt(time)
            med *= math.sqrt(horizon_s / h)
        out.update(range_bps=round(med, 3), windows=len(rs))
        return out

    def _add_trade(self, ts: int, px: float, sz: float, sd: int) -> None:
        self.trades.append((ts, px, sz, sd))
        self.last_trade_px, self.last_trade_ts = px, max(ts, self.last_trade_ts)
        cut = ts - 125_000
        while self.trades and self.trades[0][0] < cut:
            self.trades.popleft()
        sec = ts // 1000 * 1000
        if self.bars and self.bars[-1]["t"] == sec:
            b = self.bars[-1]
            b["h"], b["l"], b["c"] = max(b["h"], px), min(b["l"], px), px
            b["v"] += px * sz
        elif not self.bars or sec > self.bars[-1]["t"]:
            if self.bars:  # fill quiet seconds with flat bars so windows are in real seconds
                t, c = self.bars[-1]["t"] + 1000, self.bars[-1]["c"]
                while t < sec and sec - t <= 180_000:
                    self.bars.append({"t": t, "o": c, "h": c, "l": c, "c": c, "v": 0.0})
                    t += 1000
            self.bars.append({"t": sec, "o": px, "h": px, "l": px, "c": px, "v": px * sz})

    # ---------------------------------------------------------------- views
    def touch(self) -> tuple[float, float]:
        with self.lock:
            if not self.bids or not self.asks:
                return 0.0, 0.0
            return self.bids[0][0], self.asks[0][0]

    def ready(self, now_ms: int | None = None, stale_ms: int = 5_000) -> bool:
        now = now_ms or self.clock()
        with self.lock:
            return bool(self.bids and self.asks) and now - self.book_rx < stale_ms

    def depth_usd(self, bps: float = 5.0) -> tuple[float, float]:
        """Resting notional (USDT) within `bps` of mid on each side of the live book."""
        with self.lock:
            bids, asks = list(self.bids), list(self.asks)
        if not bids or not asks:
            return 0.0, 0.0
        mid = (bids[0][0] + asks[0][0]) / 2
        lo, hi = mid * (1 - bps / 1e4), mid * (1 + bps / 1e4)
        return (sum(p * s for p, s in bids if p >= lo), sum(p * s for p, s in asks if p <= hi))

    def features(self, now_ms: int | None = None) -> dict[str, Any]:
        """Compact, numeric microstructure features. All bps are of mid."""
        now = now_ms or self.clock()
        with self.lock:
            bids, asks = list(self.bids), list(self.asks)
            trades = list(self.trades)
            mids = list(self.mids)
            bars = list(self.bars)
            funding, mark, index = self.funding, self.mark, self.index
            oi_hist = list(self.oi_hist)
            ls = self.ls_ratio
            nf = self.next_funding_ms
            last_trade_rx = self.last_trade_rx
            last_trade_ts = self.last_trade_ts
        if not bids or not asks:
            return {}
        bb, bs = bids[0]
        ba, as_ = asks[0]
        mid = (bb + ba) / 2
        bps = 1e4 / mid

        def imb(n: int) -> float:
            b = sum(s for _, s in bids[:n])
            a = sum(s for _, s in asks[:n])
            return (b - a) / (b + a) if (a + b) else 0.0

        # depth-weighted imbalance within 5 bps of mid (size-weighted by closeness)
        def near(levels, sign):
            tot = 0.0
            for p, s in levels:
                d = abs(p - mid) * bps
                if d > 5:
                    break
                tot += s * (1 - d / 5)
            return tot

        nb, na = near(bids, 1), near(asks, -1)
        micro = (ba * bs + bb * as_) / (bs + as_) if (bs + as_) else mid
        lo5, hi5 = mid * (1 - 5e-4), mid * (1 + 5e-4)
        dep_b = sum(p * s for p, s in bids if p >= lo5)
        dep_a = sum(p * s for p, s in asks if p <= hi5)

        def flow(win_s: int) -> tuple[float, float, int]:
            cut = now - win_s * 1000
            b = s = 0.0
            n = 0
            for ts, px, sz, sd in trades:
                if ts < cut:
                    continue
                n += 1
                if sd > 0:
                    b += px * sz
                else:
                    s += px * sz
            tot = b + s
            return ((b - s) / tot if tot else 0.0), tot, n

        f5, u5, n5 = flow(5)
        f15, u15, n15 = flow(15)
        f60, u60, n60 = flow(60)

        def ret(sec: int) -> float:
            cut = now - sec * 1000
            ref = None
            for ts, m in mids:
                if ts >= cut:
                    ref = m
                    break
            return (mid / ref - 1) * 1e4 if ref else 0.0

        closes = [b["c"] for b in bars if b["t"] >= now - 61_000]
        r1 = [(closes[i] / closes[i - 1] - 1) * 1e4 for i in range(1, len(closes)) if closes[i - 1]]
        rv1 = math.sqrt(sum(x * x for x in r1) / len(r1)) if r1 else 0.0  # per-second bps
        hi = max((b["h"] for b in bars if b["t"] >= now - 61_000), default=mid)
        lo = min((b["l"] for b in bars if b["t"] >= now - 61_000), default=mid)
        rng = (hi - lo) * bps
        oi_chg = 0.0
        if len(oi_hist) >= 2:
            old = next((v for t, v in oi_hist if t >= now - 300_000), oi_hist[0][1])
            oi_chg = (oi_hist[-1][1] / old - 1) * 100 if old else 0.0
        return {
            "symbol": self.symbol,
            "mid": round(mid, self.dp + 1),
            "bid": bb,
            "ask": ba,
            "tick": self.tick,
            "tick_bps": round(self.tick * bps, 3),
            "spread_bps": round((ba - bb) * bps, 4),
            "imb_l1": round(imb(1), 3),
            "imb_l5": round(imb(5), 3),
            "imb_l15": round(imb(15), 3),
            "imb_5bps": round((nb - na) / (nb + na), 3) if (nb + na) else 0.0,
            "micro_bps": round((micro - mid) * bps, 4),
            "depth_bid_5bps_usd": round(dep_b),
            "depth_ask_5bps_usd": round(dep_a),
            "flow_5s": round(f5, 3),
            "flow_15s": round(f15, 3),
            "flow_60s": round(f60, 3),
            "usd_15s": round(u15),
            "usd_60s": round(u60),
            "n_15s": n15,
            "n_60s": n60,
            "ret_5s": round(ret(5), 2),
            "ret_15s": round(ret(15), 2),
            "ret_30s": round(ret(30), 2),
            "ret_60s": round(ret(60), 2),
            "ret_300s": round(ret(300), 2),
            "range_60s_bps": round(rng, 2),
            "pos_in_range": round((mid - lo) / (hi - lo), 2) if hi > lo else 0.5,
            "rv_1s_bps": round(rv1, 3),
            "sigma_120s_bps": round(rv1 * math.sqrt(120), 2),
            "funding_bps": round(funding * 1e4, 3),
            "funding_in_min": round((nf - now) / 60000, 1) if nf else None,
            "basis_bps": round((mark / index - 1) * 1e4, 2) if (mark and index) else 0.0,
            "oi_chg_5m_pct": round(oi_chg, 3),
            "ls_ratio": round(ls, 3),
            "book_age_ms": now - self.book_rx if self.book_rx else None,
            "last_trade_age_s": round((now - max(last_trade_rx, last_trade_ts)) / 1000, 1) if (last_trade_rx or last_trade_ts) else None,
        }

    def candles_1s(self, n: int = 60) -> list[dict]:
        with self.lock:
            return [dict(b) for b in list(self.bars)[-n:]]

    def mids_every(self, step_s: int = 5, span_s: int = 1800) -> list[list]:
        """Last `span_s` of 1 s book mids sampled every `step_s` (the close of each bucket)."""
        with self.lock:
            pts = list(self.mid1s)
        if not pts:
            return []
        cut = pts[-1][0] - span_s * 1000
        out: dict[int, float] = {}
        step = step_s * 1000
        for t, m in pts:
            if t >= cut:
                out[t - t % step] = m
        return [[t // 1000, round(m, self.dp + 1)] for t, m in sorted(out.items())]

    def top(self, n: int = 10) -> dict:
        with self.lock:
            return {"bids": [[p, s] for p, s in self.bids[:n]], "asks": [[p, s] for p, s in self.asks[:n]]}


class MultiFeed:
    """One public socket for every symbol. Reconnects with back-off; a silent socket (no
    message for `silent_s`) is dropped and reopened; a single symbol whose book stops
    updating is re-subscribed. Per-symbol listeners get (symbol, ...)."""

    def __init__(self, symbols: list[str], specs: dict[str, dict] | None = None,
                 mid_keep_s: int = 1800, silent_s: float = 15.0, resub_s: float = 45.0) -> None:
        specs = specs or {}
        self.symbols = list(symbols)
        self.feeds = {s: Feed(s, float((specs.get(s) or {}).get("tick") or TICK), mid_keep_s) for s in self.symbols}
        self.trade_listeners: list[Callable[[str, int, float, float, int], None]] = []
        self.book_listeners: list[Callable[[str], None]] = []
        for s, fd in self.feeds.items():
            fd.trade_listeners.append(lambda ts, px, sz, sd, _s=s: [cb(_s, ts, px, sz, sd) for cb in list(self.trade_listeners)])
            fd.book_listeners.append(lambda _s=s: [cb(_s) for cb in list(self.book_listeners)])
        self.ws_up = False
        self.connects = 0
        self.last_rx = 0
        self.last_error = ""
        self.last_error_ms = 0
        self.resubs = 0
        self.sub_errors: list[str] = []
        self.silent_s = silent_s
        self.resub_s = resub_s

    def touch(self, sym: str) -> tuple[float, float]:
        fd = self.feeds.get(sym)
        return fd.touch() if fd else (0.0, 0.0)

    def on_message(self, m: dict, rx_ms: int | None = None) -> None:
        arg = m.get("arg") or {}
        fd = self.feeds.get(arg.get("instId"))
        if m.get("event") == "error":
            self.sub_errors = (self.sub_errors + [f"{arg.get('instId')}/{arg.get('channel')}: {m.get('code')} {str(m.get('msg'))[:60]}"])[-8:]
            return
        if fd is not None:
            fd.on_message(m, rx_ms)

    def status(self) -> dict:
        return {"ws": self.ws_up, "connects": self.connects, "resubs": self.resubs,
                "last_rx_age_s": round(time.time() - self.last_rx / 1000, 1) if self.last_rx else None,
                "last_error": self.last_error or None, "sub_errors": self.sub_errors[-3:] or None}

    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True, name="hf-ws").start()
        threading.Thread(target=self._rest_loop, daemon=True, name="hf-rest").start()

    def _sub(self, syms: list[str]) -> str:
        return json.dumps({"op": "subscribe", "args": [
            {"instType": PRODUCT, "channel": c, "instId": s} for s in syms for c in CHANNELS]})

    async def _run(self) -> None:
        import websockets  # venv dependency, only needed live

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=None, close_timeout=5, open_timeout=10,
                                              max_size=2 ** 22) as ws:
                    await ws.send(self._sub(self.symbols))
                    self.connects += 1
                    for fd in self.feeds.values():
                        fd.connects = self.connects
                    t_open = last_ping = last_msg = time.time()
                    last_resub: dict[str, float] = {}
                    while True:
                        now = time.time()
                        if now - last_ping > 25:
                            await ws.send("ping")
                            last_ping = now
                        if now - last_msg > self.silent_s:
                            raise TimeoutError(f"socket silent {self.silent_s:.0f}s")
                        # a single symbol stopped updating while the socket is alive: re-subscribe it
                        if now - t_open > self.resub_s:
                            stale = [s for s, fd in self.feeds.items()
                                     if (not fd.book_rx or now * 1000 - fd.book_rx > self.resub_s * 1000)
                                     and now - last_resub.get(s, 0) > 120]
                            if stale:
                                await ws.send(self._sub(stale))
                                self.resubs += 1
                                for s in stale:
                                    last_resub[s] = now
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        last_msg = time.time()
                        self.last_rx = int(last_msg * 1000)
                        if not self.ws_up:
                            self.ws_up = True
                            backoff = 1.0
                            for fd in self.feeds.values():
                                fd.ws_up = True
                        if raw == "pong":
                            continue
                        try:
                            self.on_message(json.loads(raw))
                        except Exception:
                            continue
            except Exception as exc:
                self.ws_up = False
                for fd in self.feeds.values():
                    fd.ws_up = False
                self.last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
                self.last_error_ms = int(time.time() * 1000)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    def _rest_loop(self) -> None:
        while True:
            for s, fd in self.feeds.items():
                try:
                    rows = _get(f"/api/v2/mix/market/account-long-short?productType={PRODUCT}"
                                f"&symbol={s}&period=5m")
                    if rows:
                        fd.ls_ratio = _f(rows[-1].get("longShortAccountRatio"), fd.ls_ratio)
                        fd.ls_ok = True
                except Exception:
                    pass
                time.sleep(2)
            time.sleep(45)
