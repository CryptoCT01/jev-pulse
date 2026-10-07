#!/usr/bin/env python3
"""v4 asset on/off control file (.state/assets.json), shared by the dash server (writer)
and the engine (reader, every tick). One source of truth for validation.

File: {"version": 1, "seq": <int>, "updated_ms": <int>, "source": "dash", "assets": {"SOLUSDT": false, ...}}
Rules: `assets` maps symbols of the configured universe to real JSON booleans; nothing else
is accepted. A symbol missing from the file is ON (default: all ON). Writes are atomic
(temp file + fsync + rename) so the engine never reads a half-written file.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

VERSION = 1
MAX_BYTES = 4096
SYM_RE = re.compile(r"^[A-Z0-9]{2,20}$")
TOP_KEYS = {"version", "seq", "updated_ms", "source", "assets"}


def validate_file(obj: Any, universe: list[str]) -> tuple[bool, dict | None, str]:
    """Validate a whole control-file object. Returns (ok, cleaned, error)."""
    if not isinstance(obj, dict):
        return False, None, "not an object"
    extra = set(obj) - TOP_KEYS
    if extra:
        return False, None, f"unknown keys {sorted(extra)}"
    if obj.get("version", VERSION) != VERSION:
        return False, None, "bad version"
    seq = obj.get("seq", 0)
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return False, None, "bad seq"
    assets = obj.get("assets")
    if not isinstance(assets, dict):
        return False, None, "assets must be an object"
    clean: dict[str, bool] = {}
    for k, v in assets.items():
        if not isinstance(k, str) or k not in universe:
            return False, None, f"unknown symbol {str(k)[:24]!r}"
        if not isinstance(v, bool):
            return False, None, f"{k}: enabled must be true/false"
        clean[k] = v
    full = {s: clean.get(s, True) for s in universe}
    upd = obj.get("updated_ms", 0)
    if not isinstance(upd, int) or isinstance(upd, bool):
        return False, None, "bad updated_ms"
    return True, {"version": VERSION, "seq": seq, "updated_ms": upd,
                  "source": str(obj.get("source") or "")[:16], "assets": full}, ""


def validate_request(obj: Any, universe: list[str]) -> tuple[bool, tuple[str, bool] | None, str]:
    """Validate one toggle request body: exactly {"symbol": <universe symbol>, "enabled": <bool>}."""
    if not isinstance(obj, dict):
        return False, None, "body must be a JSON object"
    if set(obj) != {"symbol", "enabled"}:
        return False, None, "body must have exactly: symbol, enabled"
    sym, en = obj["symbol"], obj["enabled"]
    if not isinstance(sym, str) or not SYM_RE.match(sym) or sym not in universe:
        return False, None, "unknown symbol"
    if not isinstance(en, bool):
        return False, None, "enabled must be true or false"
    return True, (sym, en), ""


def defaults(universe: list[str]) -> dict:
    return {"version": VERSION, "seq": 0, "updated_ms": 0, "source": "default",
            "assets": {s: True for s in universe}}


def read(path: Path, universe: list[str]) -> tuple[dict, bool, str]:
    """(control, file_ok, error). Missing file -> defaults (all ON), ok. Unreadable or invalid
    -> defaults with ok False and the error (callers keep their last good copy)."""
    if not path.is_file():
        return defaults(universe), True, ""
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            return defaults(universe), False, "file too large"
        obj = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return defaults(universe), False, f"unreadable: {type(exc).__name__}"
    ok, clean, err = validate_file(obj, universe)
    if not ok:
        return defaults(universe), False, err
    return clean, True, ""


def write(path: Path, control: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    data = json.dumps(control, separators=(",", ":"), sort_keys=True).encode()
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def apply_toggle(path: Path, universe: list[str], sym: str, enabled: bool) -> dict:
    """Read-modify-write one symbol (caller holds a lock). Returns the new control."""
    cur, ok, _ = read(path, universe)
    new = {"version": VERSION, "seq": int(cur.get("seq") or 0) + 1, "updated_ms": int(time.time() * 1000),
           "source": "dash", "assets": {**cur["assets"], sym: bool(enabled)}}
    ok2, clean, err = validate_file(new, universe)
    if not ok2:
        raise ValueError(err)
    write(path, clean)
    return clean
