"""AgentHub-equivalent tape via getagent.data (Studio-legal). No MCP. No urllib."""
from typing import Any

from getagent import data


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _records(raw: Any) -> list[Any]:
    if raw is None:
        return []
    try:
        recs = data.to_records(raw)
        if isinstance(recs, list):
            return recs
        if recs is not None:
            return [recs]
    except Exception:
        pass
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    return []


def _last(raw: Any) -> dict[str, Any]:
    recs = _records(raw)
    for row in reversed(recs):
        if isinstance(row, dict):
            return row
    return {}


def _px_sz(lvl: Any) -> tuple[float, float]:
    if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
        return _f(lvl[0]), _f(lvl[1])
    if isinstance(lvl, dict):
        return (
            _f(lvl.get("price") or lvl.get("px") or lvl.get("bidPrice") or lvl.get("askPrice")),
            _f(lvl.get("size") or lvl.get("sz") or lvl.get("amount") or lvl.get("qty")),
        )
    return 0.0, 0.0


def _book_sides(book: Any) -> tuple[list[Any], list[Any]]:
    recs = _records(book)
    row: Any = recs[0] if recs else book
    if not isinstance(row, dict):
        return [], []
    bids = row.get("bids") or row.get("bid") or []
    asks = row.get("asks") or row.get("ask") or []
    if isinstance(bids, dict):
        bids = bids.get("levels") or []
    if isinstance(asks, dict):
        asks = asks.get("levels") or []
    return list(bids or []), list(asks or [])


def snapshot(symbol: str, mark: float) -> dict[str, Any]:
    extra: dict[str, Any] = {"source": "getagent.data.bitget", "evaluation_mode": "live"}
    try:
        book = data.crypto.futures.order_book(symbol=symbol, limit=20, exchange="bitget")
        bids, asks = _book_sides(book)
        bid_n = ask_n = 0.0
        for lvl in bids[:8]:
            px, sz = _px_sz(lvl)
            bid_n += px * sz
        for lvl in asks[:8]:
            px, sz = _px_sz(lvl)
            ask_n += px * sz
        tot = bid_n + ask_n
        extra["book_imb"] = round(((bid_n - ask_n) / tot) if tot else 0.0, 4)
        extra["bid_n"] = round(bid_n, 2)
        extra["ask_n"] = round(ask_n, 2)
    except Exception:
        extra["book_imb"] = 0.0

    try:
        ls = _last(
            data.crypto.futures.long_short_ratio(
                symbol=symbol, interval="5m", exchange="bitget", limit=8
            )
        )
        extra["ls_ratio"] = round(_f(ls.get("long_short_ratio") or ls.get("longShortAccountRatio"), 1.0), 4)
    except Exception:
        extra["ls_ratio"] = 1.0

    try:
        liq = _last(
            data.crypto.futures.liquidations(
                symbol=symbol, interval="5m", exchange="bitget", limit=8, days=1
            )
        )
        extra["liq_long"] = round(_f(liq.get("long_liquidations")), 2)
        extra["liq_short"] = round(_f(liq.get("short_liquidations")), 2)
    except Exception:
        extra["liq_long"] = 0.0
        extra["liq_short"] = 0.0

    try:
        tv = _last(
            data.crypto.futures.taker_volume(
                symbol=symbol, interval="5m", limit=8, exchange="bitget"
            )
        )
        buy_v = _f(tv.get("buy_vol"))
        sell_v = _f(tv.get("sell_vol"))
        flow = buy_v + sell_v
        extra["taker_buy_ratio"] = round(buy_v / flow, 4) if flow else 0.5
    except Exception:
        extra["taker_buy_ratio"] = 0.5

    large = None
    try:
        trades = _records(data.crypto.futures.trades(symbol=symbol, limit=40, exchange="bitget"))
        for fill in trades[:40]:
            if not isinstance(fill, dict):
                continue
            usd = _f(fill.get("cost"))
            if usd <= 0:
                usd = _f(fill.get("price")) * _f(fill.get("amount") or fill.get("size"))
            if usd < 80_000:
                continue
            side = str(fill.get("side") or "").lower()
            row = {"side": "buy" if side in {"buy", "bid"} else "sell", "usd": round(usd, 0)}
            if large is None or usd > float(large["usd"]):
                large = row
    except Exception:
        large = None
    extra["large"] = large
    return extra
