#!/usr/bin/env python3
"""Bitget PUBLIC market feed for the v3 HF paper desk (BTCUSDT perp).

WebSocket channels: books15 (top-15 snapshot every ~150 ms), trade (every print with
taker side), ticker (funding, mark, index). A slow REST poll adds OI and the
long/short account ratio. No keys, no private channels, no orders.

Feed is a plain object so the simulator and the replay tool can drive it from
recorded messages exactly like the live socket does.
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections import deque
from typing import Any, Callable

WS_URL = "wss://ws.bitget.com/v2/ws/public"
SYMBOL = "BTCUSDT"
TICK = 0.1


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


class Feed:
    def __init__(self, symbol: str = SYMBOL) -> None:
        self.symbol = symbol
        self.lock = threading.RLock()
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        self.book_ts = 0
        self.book_rx = 0
        self.trades: deque = deque()  # (ts_ms, px, sz, +1 buy / -1 sell)
        self.last_trade_px = 0.0
        self.last_trade_ts = 0
        self.mids: deque = deque()  # (ts_ms, mid) sampled on book updates, 330 s
        self.bars: deque = deque(maxlen=180)  # 1 s OHLCV from trades
        self.funding = 0.0
        self.mark = 0.0
        self.index = 0.0
        self.next_funding_ms = 0
        self.oi = 0.0
        self.oi_hist: deque = deque(maxlen=40)  # (ts_ms, oi)
        self.ls_ratio = 1.0
        self.ws_up = False
        self.connects = 0
        self.msgs = {"books15": 0, "trade": 0, "ticker": 0}
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
                for cb in list(self.trade_listeners):
                    cb(ts, px, sz, sd)
        elif ch == "ticker":
            r = data[0]
            with self.lock:
                self.funding = _f(r.get("fundingRate"), self.funding)
                self.mark = _f(r.get("markPrice"), self.mark)
                self.index = _f(r.get("indexPrice"), self.index)
                self.next_funding_ms = int(_f(r.get("nextFundingTime"), self.next_funding_ms))
                hold = _f(r.get("holdingAmount"))
                if hold:
                    self.oi = hold
                    if not self.oi_hist or now - self.oi_hist[-1][0] >= 15_000:
                        self.oi_hist.append((now, hold))

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

    def ready(self, now_ms: int | None = None) -> bool:
        now = now_ms or self.clock()
        with self.lock:
            return bool(self.bids and self.asks) and now - self.book_rx < 5_000

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
            "mid": round(mid, 2),
            "bid": bb,
            "ask": ba,
            "spread_bps": round((ba - bb) * bps, 4),
            "imb_l1": round(imb(1), 3),
            "imb_l5": round(imb(5), 3),
            "imb_l15": round(imb(15), 3),
            "imb_5bps": round((nb - na) / (nb + na), 3) if (nb + na) else 0.0,
            "micro_bps": round((micro - mid) * bps, 4),
            "flow_5s": round(f5, 3),
            "flow_15s": round(f15, 3),
            "flow_60s": round(f60, 3),
            "usd_15s": round(u15),
            "usd_60s": round(u60),
            "n_15s": n15,
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
        }

    def candles_1s(self, n: int = 60) -> list[dict]:
        with self.lock:
            return [dict(b) for b in list(self.bars)[-n:]]

    def top(self, n: int = 10) -> dict:
        with self.lock:
            return {"bids": [[p, s] for p, s in self.bids[:n]], "asks": [[p, s] for p, s in self.asks[:n]]}

    # ---------------------------------------------------------------- live socket
    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True, name="hf-ws").start()
        threading.Thread(target=self._rest_loop, daemon=True, name="hf-rest").start()

    async def _run(self) -> None:
        import websockets  # venv dependency, only needed live

        sub = {"op": "subscribe", "args": [
            {"instType": "USDT-FUTURES", "channel": c, "instId": self.symbol}
            for c in ("books15", "trade", "ticker")]}
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=None, close_timeout=5,
                                              max_size=2 ** 22) as ws:
                    await ws.send(json.dumps(sub))
                    self.ws_up = True
                    self.connects += 1
                    last_ping = time.time()
                    while True:
                        if time.time() - last_ping > 25:
                            await ws.send("ping")
                            last_ping = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        except asyncio.TimeoutError:
                            continue
                        if raw == "pong":
                            continue
                        try:
                            self.on_message(json.loads(raw))
                        except Exception:
                            continue
            except Exception:
                self.ws_up = False
                await asyncio.sleep(2)

    def _rest_loop(self) -> None:
        import urllib.request

        def get(path: str) -> Any:
            req = urllib.request.Request("https://api.bitget.com" + path,
                                         headers={"User-Agent": "jev-pulse-hf/3"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode()).get("data")

        while True:
            try:
                rows = get(f"/api/v2/mix/market/account-long-short?productType=USDT-FUTURES"
                           f"&symbol={self.symbol}&period=5m")
                if rows:
                    self.ls_ratio = _f(rows[-1].get("longShortAccountRatio"), self.ls_ratio)
            except Exception:
                pass
            time.sleep(60)
