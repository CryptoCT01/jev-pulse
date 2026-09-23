#!/usr/bin/env python3
"""Public Bitget / Agent Hub tape — no OpenRouter, no live orders."""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

UA = {"User-Agent": "jev-pulse-tape/0.1"}
BASE = "https://api.bitget.com"


def _get(path: str, timeout: float = 12.0) -> Any:
    req = urllib.request.Request(BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if str(body.get("code")) not in {"00000", "200"}:
        raise RuntimeError(f"bitget {path}: {body.get('code')} {body.get('msg')}")
    return body.get("data")


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fetch_ticker(symbol: str = "BTCUSDT") -> dict[str, Any]:
    data = _get(f"/api/v2/mix/market/ticker?productType=USDT-FUTURES&symbol={symbol}")
    row = data[0] if isinstance(data, list) else data
    return row or {}


def fetch_candles(symbol: str = "BTCUSDT", granularity: str = "1m", limit: int = 60) -> list[list]:
    data = _get(
        f"/api/v2/mix/market/candles?productType=USDT-FUTURES&symbol={symbol}"
        f"&granularity={granularity}&limit={limit}"
    )
    rows = list(data or [])
    def _ts(row):
        try:
            return float(row[0])
        except (TypeError, ValueError, IndexError):
            return 0.0
    rows.sort(key=_ts)
    return rows


def fetch_book(symbol: str = "BTCUSDT", limit: int = 12) -> dict[str, Any]:
    data = _get(f"/api/v2/mix/market/merge-depth?productType=USDT-FUTURES&symbol={symbol}&limit={limit}")
    return data or {}


def fetch_fills(symbol: str = "BTCUSDT", limit: int = 40) -> list[dict[str, Any]]:
    data = _get(f"/api/v2/mix/market/fills?productType=USDT-FUTURES&symbol={symbol}&limit={limit}")
    if isinstance(data, list):
        return data
    return data or []


def fetch_oi(symbol: str = "BTCUSDT") -> dict[str, Any]:
    data = _get(f"/api/v2/mix/market/open-interest?productType=USDT-FUTURES&symbol={symbol}")
    return data or {}


def fetch_ls(symbol: str = "BTCUSDT") -> dict[str, Any]:
    data = _get(
        f"/api/v2/mix/market/account-long-short?productType=USDT-FUTURES&symbol={symbol}&period=5m"
    )
    rows = data if isinstance(data, list) else (data or [])
    return rows[-1] if rows else {}


def compact_extra(book: dict, fills: list, ls: dict, oi: dict, mark: float) -> dict[str, Any]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    def notion(levels: list, n: int = 8) -> float:
        tot = 0.0
        for lvl in levels[:n]:
            if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                tot += _f(lvl[0]) * _f(lvl[1])
            elif isinstance(lvl, dict):
                tot += _f(lvl.get("price") or lvl.get("px")) * _f(lvl.get("size") or lvl.get("sz"))
        return tot

    bid_n = notion(bids)
    ask_n = notion(asks)
    tot = bid_n + ask_n
    imb = ((bid_n - ask_n) / tot) if tot else 0.0
    best_bid = _f(bids[0][0]) if bids and isinstance(bids[0], (list, tuple)) else mark
    best_ask = _f(asks[0][0]) if asks and isinstance(asks[0], (list, tuple)) else mark
    spread_bps = ((best_ask - best_bid) / mark * 1e4) if mark else 0.0

    large = None
    buy_n = sell_n = 0.0
    events: list[dict[str, Any]] = []
    for fill in fills[:40]:
        px = _f(fill.get("price"))
        sz = _f(fill.get("size"))
        usd = px * sz
        side = str(fill.get("side") or "").lower()
        if side in {"buy", "bid"}:
            buy_n += usd
        else:
            sell_n += usd
        if usd >= 80_000:
            events.append(
                {
                    "side": "bid" if side in {"buy", "bid"} else "ask",
                    "usd": round(usd, 0),
                    "px": px,
                    "bps": round((px - mark) / mark * 1e4, 2) if mark else 0,
                    "ts": fill.get("ts"),
                }
            )
            if large is None or usd > large["usd"]:
                large = events[-1]

    flow = buy_n + sell_n
    oi_list = (oi.get("openInterestList") or []) if isinstance(oi, dict) else []
    oi_sz = _f(oi_list[0].get("size")) if oi_list and isinstance(oi_list[0], dict) else 0.0

    return {
        "book_imb": round(imb, 4),
        "spread_bps": round(spread_bps, 3),
        "taker_buy_ratio": round(buy_n / flow, 4) if flow else 0.5,
        "ls_ratio": round(_f(ls.get("longShortAccountRatio"), 1.0), 4),
        "long_acct": round(_f(ls.get("longAccountRatio")), 4),
        "oi": oi_sz,
        "large": large,
        "events": events[:6],
        "source": "bitget_public_agenthub",
    }


def snapshot(symbol: str = "BTCUSDT") -> dict[str, Any]:
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_ticker = pool.submit(fetch_ticker, symbol)
        f_candles = pool.submit(fetch_candles, symbol, "1m", 48)
        f_book = pool.submit(fetch_book, symbol)
        f_fills = pool.submit(fetch_fills, symbol)
        f_oi = pool.submit(fetch_oi, symbol)
        f_ls = pool.submit(fetch_ls, symbol)
        ticker = f_ticker.result()
        candles = f_candles.result()
        book = f_book.result()
        fills = f_fills.result()
        oi = f_oi.result()
        ls = f_ls.result()
    mark = _f(ticker.get("lastPr") or ticker.get("last"))
    extra = compact_extra(book, fills, ls, oi, mark)
    candle_rows = []
    for row in candles:
        try:
            candle_rows.append(
                {
                    "t": int(_f(row[0])),
                    "o": _f(row[1]),
                    "h": _f(row[2]),
                    "l": _f(row[3]),
                    "c": _f(row[4]),
                    "v": _f(row[5]) if len(row) > 5 else 0.0,
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return {
        "symbol": symbol,
        "ts_ms": int(time.time() * 1000),
        "fetch_ms": int((time.time() - t0) * 1000),
        "mark": mark,
        "funding_rate": _f(ticker.get("fundingRate")),
        "bid": _f(ticker.get("bidPr")),
        "ask": _f(ticker.get("askPr")),
        "chg_24h": _f(ticker.get("change24h")),
        "candles": candle_rows,
        "book": {
            "bids": (book.get("bids") or [])[:10],
            "asks": (book.get("asks") or [])[:10],
        },
        "extra": extra,
    }
