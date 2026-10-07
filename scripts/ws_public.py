#!/usr/bin/env python3
"""Bitget public websocket for the desk — live ticker + locally built 1 s candles, per symbol.

REST mix klines have no 1 s (lowest is 1m). We bucket public trades/ticker into 1 s OHLC
for every symbol of the v4 universe on one socket. No OpenRouter. No private WS. No orders.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import websockets

import tape

WS_URL = "wss://ws.bitget.com/v2/ws/public"
SYMBOL = "BTCUSDT"
SYMBOLS: list[str] = [SYMBOL]
MAX_BARS = 240  # per symbol; the dash server raises it to 1800 (30 min) at start
_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}
_meta: dict[str, Any] = {"ws": False, "connects": 0, "last_rx": 0}
_started = False


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _st(sym: str) -> dict[str, Any]:
    s = _state.get(sym)
    if s is None:
        s = _state[sym] = {"mark": 0.0, "bid": 0.0, "ask": 0.0, "funding": 0.0, "chg_24h": None,
                           "ts_ms": 0, "rx_ms": 0, "candles": []}
    return s


def snapshot(symbol: str = SYMBOL) -> dict[str, Any]:
    with _lock:
        st = _st(symbol)
        uniq = {}
        for b in st["candles"]:
            uniq[int(b["t"])] = b
        candles = [dict(uniq[k]) for k in sorted(uniq)]
        return {"symbol": symbol, "mark": st["mark"], "bid": st["bid"], "ask": st["ask"], "funding": st["funding"],
                "chg_24h": st["chg_24h"], "ts_ms": st["ts_ms"], "rx_ms": st["rx_ms"], "candles": candles,
                "ws": _meta["ws"], "granularity": "1s"}


def quotes() -> dict[str, dict]:
    """Latest bid/ask/last per symbol, no candles (cheap, for /api/state)."""
    with _lock:
        return {s: {"mark": st["mark"], "bid": st["bid"], "ask": st["ask"], "funding": st["funding"],
                    "chg_24h": st["chg_24h"], "ts_ms": st["ts_ms"], "rx_ms": st["rx_ms"]} for s, st in _state.items()}


def meta() -> dict:
    with _lock:
        return dict(_meta)


def _apply_px(sym: str, px: float, ts_ms: int, vol: float = 0.0) -> None:
    if not px:
        return
    bucket = (int(ts_ms) // 1000) * 1000
    with _lock:
        st = _st(sym)
        st["mark"] = px
        st["ts_ms"] = int(ts_ms)
        bars: list = st["candles"]
        if bars and bucket < int(bars[-1]["t"]):
            for bar in reversed(bars):  # out-of-order trade: update that second if we still have it
                if int(bar["t"]) == bucket:
                    bar["h"] = max(float(bar["h"]), px)
                    bar["l"] = min(float(bar["l"]), px)
                    bar["c"] = px
                    bar["v"] = float(bar.get("v") or 0) + vol
                    break
            return
        if not bars or int(bars[-1]["t"]) != bucket:
            if bars:  # fill skipped seconds with flat candles so the chart walks
                last_t = int(bars[-1]["t"])
                last_c = float(bars[-1]["c"])
                t = last_t + 1000
                while t < bucket and len(bars) < MAX_BARS * 2:
                    bars.append({"t": t, "o": last_c, "h": last_c, "l": last_c, "c": last_c, "v": 0.0})
                    t += 1000
            bars.append({"t": bucket, "o": px, "h": px, "l": px, "c": px, "v": vol})
            if len(bars) > MAX_BARS:
                del bars[: len(bars) - MAX_BARS]
        else:
            bar = bars[-1]
            bar["h"] = max(float(bar["h"]), px)
            bar["l"] = min(float(bar["l"]), px)
            bar["c"] = px
            bar["v"] = float(bar.get("v") or 0) + vol


def _seed_from_rest(sym: str) -> None:
    try:
        fills = tape.fetch_fills(sym, 80)
    except Exception:
        fills = []
    rows = []
    for f in reversed(fills):
        ts = int(_f(f.get("ts")))
        px = _f(f.get("price"))
        sz = _f(f.get("size"))
        if ts and px:
            rows.append((ts, px, px * sz))
    try:
        tick = tape.fetch_ticker(sym)
        px = _f(tick.get("lastPr") or tick.get("last"))
        with _lock:
            st = _st(sym)
            st["mark"] = px
            st["bid"] = _f(tick.get("bidPr"))
            st["ask"] = _f(tick.get("askPr"))
            st["funding"] = _f(tick.get("fundingRate"))
            st["chg_24h"] = _f(tick.get("change24h")) if tick.get("change24h") not in (None, "") else None
        if px:
            rows.append((int(time.time() * 1000), px, 0.0))
    except Exception:
        pass
    for ts, px, vol in rows:
        _apply_px(sym, px, ts, vol)


def _handle(msg: str) -> None:
    if msg == "pong":
        return
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return
    arg = data.get("arg") or {}
    channel = arg.get("channel")
    sym = arg.get("instId")
    payload = data.get("data")
    if not payload or sym not in SYMBOLS:
        return
    now = int(time.time() * 1000)
    if channel == "ticker":
        row = payload[0] if isinstance(payload, list) else payload
        if not isinstance(row, dict):
            return
        px = _f(row.get("lastPr") or row.get("markPr"))
        ts = int(_f(row.get("ts"), now))
        with _lock:
            st = _st(sym)
            st["bid"] = _f(row.get("bidPr"))
            st["ask"] = _f(row.get("askPr"))
            st["funding"] = _f(row.get("fundingRate") or st["funding"])
            if row.get("change24h") not in (None, ""):
                st["chg_24h"] = _f(row.get("change24h"))
            st["rx_ms"] = now
        _apply_px(sym, px, ts, 0.0)
    elif channel == "trade":
        rows = payload if isinstance(payload, list) else [payload]
        if data.get("action") == "snapshot":
            rows = sorted(rows, key=lambda r: _f((r or {}).get("ts")) if isinstance(r, dict) else 0)
        for row in rows:
            if not isinstance(row, dict):
                continue
            px = _f(row.get("price"))
            ts = int(_f(row.get("ts"), now))
            sz = _f(row.get("size"))
            _apply_px(sym, px, ts, px * sz)
        with _lock:
            _st(sym)["rx_ms"] = now


async def _run() -> None:
    sub = {"op": "subscribe", "args": [{"instType": "USDT-FUTURES", "channel": c, "instId": s}
                                       for s in SYMBOLS for c in ("ticker", "trade")]}
    backoff = 1.5
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None, close_timeout=5, open_timeout=10) as ws:
                await ws.send(json.dumps(sub))
                with _lock:
                    _meta["ws"] = True
                    _meta["connects"] += 1
                last_ping = last_msg = time.time()
                while True:
                    if time.time() - last_ping > 25:
                        await ws.send("ping")
                        last_ping = time.time()
                    if time.time() - last_msg > 20:
                        raise TimeoutError("socket silent 20s")
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    except (TimeoutError, asyncio.TimeoutError):
                        continue
                    last_msg = time.time()
                    backoff = 1.5
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    with _lock:
                        _meta["last_rx"] = int(last_msg * 1000)
                    _handle(raw)
        except Exception:
            with _lock:
                _meta["ws"] = False
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)


def _thread() -> None:
    for s in SYMBOLS:
        _seed_from_rest(s)
    asyncio.run(_run())


def start(symbols: list[str] | None = None) -> None:
    global _started, SYMBOLS
    if _started:
        return
    _started = True
    if symbols:
        SYMBOLS = list(symbols)
    with _lock:
        for s in SYMBOLS:
            _st(s)
    threading.Thread(target=_thread, daemon=True, name="bitget-public-ws").start()
