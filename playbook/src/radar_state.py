"""Build the typed state object shared by Jev and the Studio writer."""

from typing import Any


def empty_position() -> dict[str, Any]:
    return {"side": "flat", "qty": 0.0, "upnl_pct": 0.0, "age_bars": 0}


def empty_cage(*, leverage: int = 3) -> dict[str, Any]:
    return {
        "dd_pct": 0.0,
        "leverage": leverage,
        "notional_usdt": 0.0,
        "daily_pnl_pct": 0.0,
    }


def build_state(
    *,
    symbol: str,
    ts_ms: int,
    mark: float,
    funding_rate: float = 0.0,
    ret_1m: float = 0.0,
    ret_5m: float = 0.0,
    ret_1h: float = 0.0,
    vol_short: float = 0.0,
    range_pos: float = 0.5,
    position: dict[str, Any] | None = None,
    cage: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "symbol": symbol,
        "ts_ms": ts_ms,
        "mark": mark,
        "funding_rate": funding_rate,
        "ret_1m": ret_1m,
        "ret_5m": ret_5m,
        "ret_1h": ret_1h,
        "vol_short": vol_short,
        "range_pos": range_pos,
        "position": position or empty_position(),
        "cage": cage or empty_cage(),
    }
    if extra:
        state["extra"] = extra
    return state
