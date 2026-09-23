#!/usr/bin/env python3
"""Jev Radar — OpenRouter Decisions API gate (Choice / Noul / Score only).

Offline / Mac companion. Playbook sandboxes cannot import requests — do not
upload this module into GetAgent Cloud as a network client.

Pattern notes: intended to mirror a Crossfire-style shadow bench (e.g.
scripts/shadow_jev_bench.py) if present on Desktop; that file was not found
in the agent box or public Crossfire GitHub tree, so this is a clean
Decisions client for typesafe/jev-1.13.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"

# Fixed question IDs — keep in sync with STRATEGY.md and playbook/src/gate_map.py
QUESTIONS: dict[str, dict[str, Any]] = {
    "action": {
        "type": "choice",
        "instructions": (
            "You are a BTCUSDT perp paper trader. Given state, pick the action NOW. "
            "HOLD is the default. From flat, BUY opens a long and SELL opens a short — "
            "both are allowed when the lean can pay a 6 bps round trip. If a position "
            "is already open, do not reverse it. REDUCE only to exit."
        ),
        "criteria": {
            "HOLD": "Signals conflict with no lean; staying flat or staying put is clearer than guessing.",
            "BUY": "Lean long: positive short returns, bid-heavy book, range up, or cover a short to go long.",
            "SELL": "Lean short: negative short returns, ask-heavy book, range down, or flatten a long to go short.",
            "REDUCE": "Cut existing risk toward flat without flipping side (take partial profit / de-risk).",
        },
    },
    "same_side_reentry": {
        "type": "noul",
        "instructions": (
            "If recently flat or reduced on this side, is same-side re-entry "
            "justified by state right now?"
        ),
        "criteria": {
            "true": "Fresh edge after a clean reduce/flat; re-entry justified.",
            "false": "Re-entry would be revenge trading or noise chasing.",
        },
    },
    "cage_stress": {
        "type": "noul",
        "instructions": (
            "Is the risk cage under stress (drawdown, leverage, funding shock, "
            "position heat) such that new risk should be blocked?"
        ),
        "criteria": {
            "true": "Cage is stressed; veto new BUY/SELL.",
            "false": "Cage within normal paper limits.",
        },
    },
    "change_confidence": {
        "type": "score",
        "instructions": (
            "How strong is the case for changing exposure now? Score low unless "
            "the lean can pay a 6 bps round trip. Do not score up just to force a fill."
        ),
        "criteria": [
            "No change — HOLD is correct.",
            "Slight lean — still trade small paper size.",
            "Clear lean — normal paper size BUY/SELL.",
            "Strong lean — full paper add / flip allowed.",
        ],
    },
}


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency). Does not override existing env."""
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def build_questions() -> dict[str, dict[str, Any]]:
    return json.loads(json.dumps(QUESTIONS))  # deep copy


def decide(
    state: Any,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Call OpenRouter Decisions; return parsed JSON body."""
    key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OR_API_KEY")
    if not key:
        raise SystemExit(
            "Missing OPENROUTER_API_KEY (set env or .env). "
            "Get a key at https://openrouter.ai/keys"
        )

    body = {
        "model": model,
        "state": state,
        "questions": build_questions(),
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get(
            "OPENROUTER_HTTP_REFERER", "https://github.com/CryptoCT01/jev-radar"
        ),
        "X-OpenRouter-Title": os.environ.get("OPENROUTER_TITLE", "jev-pulse"),
    }
    req = urllib.request.Request(
        DECISIONS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Decisions HTTP {exc.code}: {err_body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Decisions network error: {exc}") from exc

    return json.loads(raw)


def demo_state() -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "ts_ms": 0,
        "mark": 65000.0,
        "funding_rate": 0.0001,
        "ret_1m": 0.0002,
        "ret_5m": -0.001,
        "ret_1h": 0.004,
        "vol_short": 0.012,
        "range_pos": 0.55,
        "position": {"side": "flat", "qty": 0.0, "upnl_pct": 0.0, "age_bars": 0},
        "cage": {
            "dd_pct": 1.2,
            "leverage": 3,
            "notional_usdt": 0.0,
            "daily_pnl_pct": 0.1,
        },
        "note": "demo state for schema wiring — replace with live features",
    }


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Jev Radar Decisions gate")
    parser.add_argument(
        "--state-json",
        help="Path to state JSON file (default: built-in demo state with --demo)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use built-in demo state (still calls API unless --dry-schema)",
    )
    parser.add_argument(
        "--dry-schema",
        action="store_true",
        help="Print model/questions/state only; do not call the API",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("-o", "--out", help="Write full response JSON to path")
    args = parser.parse_args(argv)

    if args.state_json:
        state = json.loads(Path(args.state_json).read_text(encoding="utf-8"))
    else:
        state = demo_state()
        if not args.demo and not args.dry_schema:
            parser.error("pass --demo, --dry-schema, or --state-json")

    payload = {"model": args.model, "state": state, "questions": build_questions()}
    if args.dry_schema:
        print(json.dumps(payload, indent=2))
        return 0

    result = decide(state, model=args.model)
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)

    answers = result.get("answers") or result.get("data", {}).get("answers") or {}
    if answers:
        print("\n# gate summary", file=sys.stderr)
        for qid in ("action", "same_side_reentry", "cage_stress", "change_confidence"):
            print(f"{qid}: {json.dumps(answers.get(qid), default=str)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
