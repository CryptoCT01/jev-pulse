#!/usr/bin/env python3
"""Bitget public websocket — instant ticker + locally built 1s candles.

REST mix klines have no 1s (lowest is 1m). We bucket public trades/ticker into 1s OHLC.
No OpenRouter. No private WS. No orders.
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
MAX_BARS = 240  # 4 minutes of 1s
_lock = threading.Lock()
_state: dict[str, Any] = {
    "mark": 0.0,
    "bid": 0.0,
    "ask": 0.0,
    "funding": 0.0,
    "ts_ms": 0,
    "candles": [],
    "ws": False,
    "granularity": "1s",
}
_started = False


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def snapshot() -> dict[str, Any]:
    with _lock:
        uniq = {}
        for b in _state["candles"]:
            uniq[int(b["t"])] = b
        candles = [uniq[k] for k in sorted(uniq)]
        return {
            "mark": _state["mark"],
            "bid": _state["bid"],
            "ask": _state["ask"],
            "funding": _state["funding"],
            "ts_ms": _state["ts_ms"],
            "candles": candles,
            "ws": _state["ws"],
            "granularity": "1s",
        }


def _apply_px(px: float, ts_ms: int, vol: float = 0.0) -> None:
    if not px:
        return
    bucket = (int(ts_ms) // 1000) * 1000
    with _lock:
        _state["mark"] = px
        _state["ts_ms"] = int(ts_ms)
        bars: list = _state["candles"]
        if bars and bucket < int(bars[-1]["t"]):
            # out-of-order trade: update that second if we still have it
            for bar in reversed(bars):
                if int(bar["t"]) == bucket:
                    bar["h"] = max(float(bar["h"]), px)
                    bar["l"] = min(float(bar["l"]), px)
                    bar["c"] = px
                    bar["v"] = float(bar.get("v") or 0) + vol
                    break
            return
        if not bars or int(bars[-1]["t"]) != bucket:
            # fill skipped seconds with flat candles so the chart walks
            if bars:
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


def _seed_from_rest() -> None:
    try:
        fills = tape.fetch_fills(SYMBOL, 80)
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
        tick = tape.fetch_ticker(SYMBOL)
        px = _f(tick.get("lastPr") or tick.get("last"))
        with _lock:
            _state["mark"] = px
            _state["bid"] = _f(tick.get("bidPr"))
            _state["ask"] = _f(tick.get("askPr"))
            _state["funding"] = _f(tick.get("fundingRate"))
        if px:
            rows.append((int(time.time() * 1000), px, 0.0))
    except Exception:
        pass
    for ts, px, vol in rows:
        _apply_px(px, ts, vol)


def _handle(msg: str) -> None:
    if msg == "pong":
        return
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return
    arg = data.get("arg") or {}
    channel = arg.get("channel")
    payload = data.get("data")
    if not payload:
        return
    now = int(time.time() * 1000)
    if channel == "ticker":
        row = payload[0] if isinstance(payload, list) else payload
        if not isinstance(row, dict):
            return
        px = _f(row.get("lastPr") or row.get("markPr"))
        ts = int(_f(row.get("ts"), now))
        with _lock:
            _state["bid"] = _f(row.get("bidPr"))
            _state["ask"] = _f(row.get("askPr"))
            _state["funding"] = _f(row.get("fundingRate") or _state["funding"])
            _state["ws"] = True
        _apply_px(px, ts, 0.0)
    elif channel == "trade":
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict):
                continue
            px = _f(row.get("price"))
            ts = int(_f(row.get("ts"), now))
            sz = _f(row.get("size"))
            _apply_px(px, ts, px * sz)


async def _run() -> None:
    sub = {
        "op": "subscribe",
        "args": [
            {"instType": "USDT-FUTURES", "channel": "ticker", "instId": SYMBOL},
            {"instType": "USDT-FUTURES", "channel": "trade", "instId": SYMBOL},
        ],
    }
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None, close_timeout=5) as ws:
                await ws.send(json.dumps(sub))
                with _lock:
                    _state["ws"] = True
                last_ping = time.time()
                while True:
                    if time.time() - last_ping > 25:
                        await ws.send("ping")
                        last_ping = time.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    except TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    _handle(raw)
        except Exception:
            with _lock:
                _state["ws"] = False
            await asyncio.sleep(1.5)


def _thread() -> None:
    _seed_from_rest()
    asyncio.run(_run())


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_thread, daemon=True, name="bitget-public-ws").start()
