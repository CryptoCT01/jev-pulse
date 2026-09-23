#!/usr/bin/env python3
"""JEV PULSE desk — file-backed. No OpenRouter. No Bitget on the request path."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dash"
LOG = ROOT / "logs" / "paper_ticks.jsonl"
TAPE_FILE = ROOT / "logs" / "tape_latest.json"
ACCT = ROOT / ".state" / "paper_account.json"
sys.path.insert(0, str(ROOT / "scripts"))

import tape  # noqa: E402
import ws_public  # noqa: E402
import studio_sync  # noqa: E402

HOST = "0.0.0.0"
PORT = 8790
START_EQUITY = 10_000.0
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


def _regime(tick: dict) -> dict:
    st = tick.get("state") or {}
    vol = float(st.get("vol_short") or 0)
    r5 = abs(float(st.get("ret_5m") or 0))
    rp = float(st.get("range_pos") or 0.5)
    label = "Range" if (vol < 0.0018 or r5 < 0.0025) else "Trend"
    score = int(max(8, min(92, 50 + (0.5 - abs(rp - 0.5)) * 80)))
    return {"label": label, "score": score}


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
        return super().do_GET()

    def _json(self, payload: dict) -> None:
        raw = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _state(self) -> dict:
        ticks = _tail_jsonl(LOG, 48)
        last = ticks[-1] if ticks else {}
        snap = _read_json(TAPE_FILE)
        live = ws_public.snapshot()
        if live.get("candles"):
            snap["candles"] = live["candles"]
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
            last["account"] = {**last["account"], **{k: acct.get(k, last["account"].get(k)) for k in ("equity", "upnl", "side", "qty", "streak", "realized", "last_settlement", "entry", "fills", "wins", "losses", "fees_paid", "volume_usdt", "gate_block", "move_bps", "last_action")}}
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
            stream.append(
                {
                    "ts_ms": ts,
                    "kind": "heartbeat",
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
            "sources": {
                "mark": "bitget_public",
                "candles": "bitget_ws_1s_from_trades",
                "funding": "bitget_public",
                "book": "bitget_public",
                "stance": "typesafe_jev_openrouter",
                "writer_action": "jev_plus_local_gate",
                "equity": "mac_paper_sim_not_studio",
                "fills": "mac_paper_sim_not_studio",
                "studio_ledger_synced": False,  # paper NAV not on Playbook API
            },
            "honesty": {
                "banner": "LIVE marks · paper · hold to 6 bps · fee 3 bps/side · no flip",
                "equity_note": "New fills are net of rebated taker. Realized before this rule is gross.",
                "fills_note": (
                    f"{int(acct.get('fills') or 0)} fills · {acct.get('gate_block') or wa.get('action') or 'HOLD'}"
                    f" · fees ${float(acct.get('fees_paid') or 0):.2f}"
                ),
                "writer_action": wa.get("action") or "HOLD",
                "change_confidence": wa.get("change_confidence_score"),
                "gate_score_threshold": 0.35,
                "fills": int(acct.get("fills") or 0),
                "companion_interval_s": 5,
            },
            "trades": list(reversed(list(file_acct.get("history") or [])))[:200],
        }


def main() -> int:
    DASH.mkdir(parents=True, exist_ok=True)
    ws_public.start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"JEV PULSE http://{HOST}:{PORT}/  (paper desk, no OpenRouter)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
