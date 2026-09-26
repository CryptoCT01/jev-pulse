#!/usr/bin/env python3
"""TypeSafe Jev questions for the v3 HF desk (OpenRouter Decisions API).

Two question sets, picked by book state, so every call only pays for questions
that can change what we do:
  flat        -> direction (choice), move_60s (score), risk_stress (noul)
  in position -> close_now (noul), risk_stress (noul)
One persistent HTTPS connection is reused to keep the round trip short.
"""
from __future__ import annotations

import http.client
import json
import os
import ssl
import threading
import time
from pathlib import Path
from typing import Any

HOST = "openrouter.ai"
PATH = "/api/alpha/decisions"
MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
MOVE_BUCKETS = ["under 2 bps (noise)", "2 to 4 bps", "4 to 7 bps", "7 to 12 bps", "over 12 bps"]
MOVE_MID_BPS = [1.0, 3.0, 5.5, 9.5, 15.0]

RISK_Q = {
    "type": "noul",
    "instructions": (
        "Should NEW risk be blocked right now? Consider: spread wider than a few ticks, "
        "very thin book, a funding settlement within 2 minutes, a sudden erratic price "
        "jump, or a paper drawdown above 3%."
    ),
    "criteria": {
        "true": "Conditions are abnormal or the paper book is stressed; do not open a trade.",
        "false": "Normal BTC perp conditions; opening a small paper clip is acceptable.",
    },
}

FLAT_Q = {
    "direction": {
        "type": "choice",
        "instructions": (
            "BTCUSDT perpetual scalper, currently flat. Using order flow (flow_* = taker buy minus "
            "sell share of notional), book imbalance (imb_* > 0 = more bids), microprice offset, "
            "short returns (ret_*, bps) and the last 30 one-second returns, which way is BTC more "
            "likely to move first by at least 4 bps over the next 60-120 seconds?"
        ),
        "criteria": {
            "UP": "Buyers are in control: taker buying, bid-heavy book near the touch, returns turning up.",
            "DOWN": "Sellers are in control: taker selling, ask-heavy book near the touch, returns turning down.",
        },
    },
    "move_60s": {
        "type": "score",
        "instructions": (
            "How far is BTC likely to travel in the direction of the stronger side over the "
            "next 60 seconds, in basis points? Use sigma_120s_bps and range_60s_bps as the "
            "scale of recent movement."
        ),
        "criteria": MOVE_BUCKETS,
    },
    "risk_stress": RISK_Q,
}

POS_Q = {
    "close_now": {
        "type": "noul",
        "instructions": (
            "We hold the paper position described in state.position (entry, unrealized bps, "
            "age, resting take-profit and stop). Should it be closed now instead of waiting for "
            "the take-profit or stop? Answer true only when flow and book pressure have turned "
            "against the position or momentum has clearly stalled."
        ),
        "criteria": {
            "true": "Flow and book now point against the position or the move has stalled; exit now.",
            "false": "Flow still supports the position or is neutral; keep the take-profit working.",
        },
    },
    "risk_stress": RISK_Q,
}


def load_dotenv(root: Path) -> None:
    env = root / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


class JevClient:
    def __init__(self, timeout_s: float = 2.2) -> None:
        self.timeout_s = timeout_s
        self.conn: http.client.HTTPSConnection | None = None
        self.lock = threading.Lock()
        self.ctx = ssl.create_default_context()

    def _key(self) -> str:
        key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OR_API_KEY") or ""
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY missing")
        return key

    def ask(self, state: dict, questions: dict) -> dict:
        body = json.dumps({"model": MODEL, "state": state, "questions": questions},
                          separators=(",", ":"))
        headers = {
            "Authorization": "Bearer " + self._key(),
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "https://github.com/CryptoCT01/jev-pulse"),
            "X-OpenRouter-Title": os.environ.get("OPENROUTER_TITLE", "jev-pulse"),
            "Connection": "keep-alive",
        }
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.conn is None:
                        self.conn = http.client.HTTPSConnection(HOST, timeout=self.timeout_s, context=self.ctx)
                    self.conn.request("POST", PATH, body=body, headers=headers)
                    resp = self.conn.getresponse()
                    raw = resp.read().decode("utf-8", "replace")
                    if resp.status != 200:
                        # never echo request headers; body is the provider's error text
                        raise RuntimeError(f"Decisions HTTP {resp.status}: {raw[:200]}")
                    return json.loads(raw)
                except (http.client.HTTPException, OSError) as exc:
                    try:
                        if self.conn:
                            self.conn.close()
                    finally:
                        self.conn = None
                    if attempt == 1:
                        raise RuntimeError(f"Decisions network: {type(exc).__name__}") from None
        raise RuntimeError("unreachable")


def parse_flat(answers: dict) -> dict[str, Any]:
    d = answers.get("direction") or {}
    probs = d.get("probabilities") or {}
    pl, ps = float(probs.get("UP") or 0), float(probs.get("DOWN") or 0)
    pn = max(0.0, 1.0 - pl - ps)
    mv = answers.get("move_60s") or {}
    mprobs = mv.get("probabilities") or {}
    exp_move = sum(float(mprobs.get(str(i)) or 0) * MOVE_MID_BPS[i] for i in range(len(MOVE_MID_BPS)))
    if not mprobs and mv.get("score") is not None:  # fall back to the weighted index
        idx = float(mv["score"])
        lo = int(min(max(idx, 0), len(MOVE_MID_BPS) - 1))
        hi = min(lo + 1, len(MOVE_MID_BPS) - 1)
        exp_move = MOVE_MID_BPS[lo] + (MOVE_MID_BPS[hi] - MOVE_MID_BPS[lo]) * (idx - lo)
    return {
        "choice": {"UP": "LONG", "DOWN": "SHORT"}.get(str(d.get("choice") or "").upper(), "NONE"),
        "p_long": pl, "p_short": ps, "p_none": pn,
        "confidence": float(d.get("confidence") or 0),
        "move_score": float(mv.get("score") or 0),
        "move_conf": float(mv.get("confidence") or 0),
        "exp_move_bps": round(exp_move, 2),
        "risk_stress": float((answers.get("risk_stress") or {}).get("noul") or 0),
    }


def parse_pos(answers: dict) -> dict[str, Any]:
    return {
        "close_now": float((answers.get("close_now") or {}).get("noul") or 0),
        "risk_stress": float((answers.get("risk_stress") or {}).get("noul") or 0),
    }


class MockJev:
    """Plumbing test only (box dry runs). Never used unless HF_MOCK_JEV=1; logged as model MOCK."""

    def ask(self, state: dict, questions: dict) -> dict:
        import random
        time.sleep(0.3)
        if "direction" in questions:
            fl = state["flow"]["flow_15s"]
            pl = max(0.0, min(1.0, 0.33 + fl * 0.4 + random.uniform(-0.1, 0.1)))
            ps = max(0.0, min(1.0, 0.33 - fl * 0.4 + random.uniform(-0.1, 0.1)))
            pn = max(0.0, 1 - pl - ps)
            ch = max((("LONG", pl), ("SHORT", ps), ("NONE", pn)), key=lambda x: x[1])[0]
            mp = [0.1, 0.2, 0.3, 0.3, 0.1]
            tot = (pl + ps) or 1.0
            pl, ps = pl / tot, ps / tot
            ch = "UP" if pl >= ps else "DOWN"
            ans = {"direction": {"type": "choice", "choice": ch, "confidence": 0.5,
                                 "probabilities": {"UP": pl, "DOWN": ps}},
                   "move_60s": {"type": "score", "score": 2.1, "confidence": 0.3,
                                "probabilities": {str(i): p for i, p in enumerate(mp)}},
                   "risk_stress": {"type": "noul", "noul": 0.2}}
        else:
            ans = {"close_now": {"type": "noul", "noul": random.uniform(0.1, 0.8)},
                   "risk_stress": {"type": "noul", "noul": 0.2}}
        return {"answers": ans, "model": "MOCK", "usage": {"cost": 0.0, "input_tokens": 0}}
