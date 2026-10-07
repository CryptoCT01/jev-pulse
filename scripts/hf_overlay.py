#!/usr/bin/env python3
"""Jev Pulse v4.1 playbook overlay: the ONLY knobs the overnight coach may turn.

playbook/overlay.json holds per-asset and global adjustments on top of scripts/hf_params.json.
Everything here is bounded twice:
  * absolute limits (ABS) that no overlay can leave, and
  * a per-night step limit (at most one step from the current value) enforced by the coach.
The engine re-reads the file every tick (mtime check), validates it with validate_overlay(), and
applies a new value to an asset only while that asset is flat, so an open trade keeps the TP /
stop / time cap / close-now threshold it was opened with.

NEVER overlay-able (rejected if present): fees, trade size, max positions, the user's asset
toggles, feed / safety settings (spread cap, risk veto, cadence, budget ...), TP / stop absolute
clamps, and anything that places orders. Paper only: nothing here talks to an exchange.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY_PATH = Path(os.environ.get("HF_OVERLAY") or ROOT / "playbook" / "overlay.json")
MAX_BYTES = 64_000
VERSION = 1

# per-asset knobs: absolute [min, max], default, and the largest move allowed per nightly review
#   step_rel = fraction of the current value, step_abs = absolute (the larger of the two applies)
ASSET_KNOBS: dict[str, dict] = {
    "room_bias":        {"min": 0.5, "max": 1.5, "default": 1.0, "step_rel": 0.20, "step_abs": 0.0,
                         "what": "multiplier on the room score (ranking AND the room >= room_min test)"},
    "tp_k":             {"min": 0.5, "max": 2.0, "default": None, "step_rel": 0.20, "step_abs": 0.0,
                         "what": "TP = clamp(tp_k x R, 5, 25) bps (the 5-25 clamp is fixed)"},
    "sl_mult":          {"min": 0.5, "max": 1.2, "default": None, "step_rel": 0.20, "step_abs": 0.0,
                         "what": "stop = clamp(sl_mult x TP, 4, 20) bps (the 4-20 clamp is fixed)"},
    "time_cap_min_s":   {"min": 120, "max": 300, "default": None, "step_rel": 0.20, "step_abs": 0.0,
                         "what": "lower end of the adaptive time cap, seconds"},
    "time_cap_max_s":   {"min": 120, "max": 300, "default": None, "step_rel": 0.20, "step_abs": 0.0,
                         "what": "upper end of the adaptive time cap, seconds"},
    "e_min_offset_bps": {"min": 0.0, "max": 4.0, "default": 0.0, "step_rel": 0.0, "step_abs": 0.8,
                         "what": "extra bps added to E_min (E_min itself never goes below the 4 bps cost)"},
}
GLOBAL_KNOBS: dict[str, dict] = {
    "close_now_thr":    {"min": 0.60, "max": 0.85, "default": None, "step_rel": 0.0, "step_abs": 0.05,
                         "what": "Jev close-now probability that triggers a maker-first exit"},
}
PARAM_OF = {"tp_k": "tp_range_k", "sl_mult": "sl_mult", "time_cap_min_s": "time_cap_min_s",
            "time_cap_max_s": "time_cap_max_s", "close_now_thr": "close_now_thr"}
IMMUTABLE = ("fees", "fee", "taker_fee", "maker_fee", "rebate", "clip_usdt", "size", "qty", "notional",
             "max_open_total", "max_open_per_asset", "enabled", "toggle", "toggles", "universe",
             "cadence_s", "spread_max_bps", "risk_veto", "jev_budget_usd_h", "tp_bps_min", "tp_bps_max",
             "sl_bps_min", "sl_bps_max", "cost_rt_bps", "room_min", "idle_range_bps", "order", "orders")


def defaults(P: dict) -> tuple[dict, dict]:
    """(asset defaults, global defaults) taken from hf_params.json."""
    a = {}
    for k, spec in ASSET_KNOBS.items():
        a[k] = float(P[PARAM_OF[k]]) if spec["default"] is None else float(spec["default"])
    g = {k: float(P[PARAM_OF[k]]) for k in GLOBAL_KNOBS}
    return a, g


def default_overlay(universe: list[str], P: dict) -> dict:
    a, g = defaults(P)
    return {"version": VERSION, "updated_ms": 0, "source": "default", "run_id": None, "model": None,
            "global": dict(g), "assets": {s: dict(a) for s in universe}}


def _clamp(v: float, spec: dict) -> float:
    return min(float(spec["max"]), max(float(spec["min"]), float(v)))


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if v == v and abs(v) != float("inf") else None


def validate_overlay(obj, universe: list[str], P: dict) -> tuple[dict, list[str]]:
    """Clean overlay (defaults for anything missing, absolute clamps, time_cap_min <= max) and a
    list of problems. Unknown / immutable keys are dropped and reported, never applied."""
    out = default_overlay(universe, P)
    problems: list[str] = []
    if not isinstance(obj, dict):
        return out, ["overlay is not an object"]
    for k in obj:
        if k not in ("version", "updated_ms", "source", "run_id", "model", "global", "assets", "note"):
            problems.append(f"unknown top-level key {k!r} ignored")
    for k in ("updated_ms",):
        if _num(obj.get(k)) is not None:
            out[k] = int(obj[k])
    for k in ("source", "run_id", "model", "note"):
        if isinstance(obj.get(k), str) and len(obj[k]) <= 200:
            out[k] = obj[k]
    g = obj.get("global") or {}
    if isinstance(g, dict):
        for k, v in g.items():
            if k in GLOBAL_KNOBS and _num(v) is not None:
                out["global"][k] = round(_clamp(v, GLOBAL_KNOBS[k]), 4)
            else:
                problems.append(f"global.{k} rejected" + (" (immutable)" if k in IMMUTABLE else ""))
    A = obj.get("assets") or {}
    if isinstance(A, dict):
        for s, kv in A.items():
            if s not in universe or not isinstance(kv, dict):
                problems.append(f"assets.{s} rejected")
                continue
            for k, v in kv.items():
                if k in ASSET_KNOBS and _num(v) is not None:
                    out["assets"][s][k] = round(_clamp(v, ASSET_KNOBS[k]), 4)
                else:
                    problems.append(f"assets.{s}.{k} rejected" + (" (immutable)" if k in IMMUTABLE else ""))
            a = out["assets"][s]
            if a["time_cap_min_s"] > a["time_cap_max_s"]:
                problems.append(f"assets.{s} time_cap_min_s > time_cap_max_s: both reset to defaults")
                d, _ = defaults(P)
                a["time_cap_min_s"], a["time_cap_max_s"] = d["time_cap_min_s"], d["time_cap_max_s"]
    return out, problems


def load(path: Path, universe: list[str], P: dict) -> tuple[dict, bool, str]:
    """(overlay, ok, error). Missing file -> defaults, ok. Unreadable / invalid JSON -> defaults, not ok
    (the engine keeps its last good overlay in that case)."""
    try:
        if not path.exists():
            return default_overlay(universe, P), True, ""
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            return default_overlay(universe, P), False, "overlay too large"
        ov, probs = validate_overlay(json.loads(raw.decode("utf-8")), universe, P)
        return ov, True, "; ".join(probs)[:300]
    except (OSError, ValueError) as exc:
        return default_overlay(universe, P), False, f"{type(exc).__name__}"


def write_atomic(path: Path, overlay: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".overlay.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(overlay, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def overlay_hash(ov: dict) -> str:
    core = {"global": ov.get("global"), "assets": ov.get("assets")}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def asset_params(P: dict, ov: dict | None, sym: str) -> dict:
    """hf_params with this asset's overlay knobs substituted (TP/stop clamps, fees, size, caps
    untouched). Extra keys: room_bias, e_min_offset_bps."""
    Ps = dict(P)
    a_def, g_def = defaults(P)
    a = dict(a_def)
    g = dict(g_def)
    if ov:
        a.update((ov.get("assets") or {}).get(sym) or {})
        g.update(ov.get("global") or {})
    for k in ("tp_k", "sl_mult", "time_cap_min_s", "time_cap_max_s"):
        Ps[PARAM_OF[k]] = float(a[k])
    Ps["close_now_thr"] = float(g["close_now_thr"])
    Ps["room_bias"] = float(a["room_bias"])
    Ps["e_min_offset_bps"] = float(a["e_min_offset_bps"])
    return Ps


def step_limits(cur: float, spec: dict) -> tuple[float, float]:
    """[lo, hi] reachable from cur in one nightly step, inside the absolute limits."""
    d = max(abs(cur) * float(spec["step_rel"]), float(spec["step_abs"]))
    return max(float(spec["min"]), cur - d), min(float(spec["max"]), cur + d)


def bounds_table(ov: dict) -> dict:
    """Bounds as shown to the coach model: absolute limits, max step and reachable range tonight."""
    t = {"asset_knobs": {}, "global_knobs": {}}
    for k, spec in ASSET_KNOBS.items():
        t["asset_knobs"][k] = {"abs_min": spec["min"], "abs_max": spec["max"], "what": spec["what"],
                               "max_step": (f"{int(spec['step_rel']*100)}% of current" if spec["step_rel"]
                                            else f"{spec['step_abs']:g} absolute")}
    for k, spec in GLOBAL_KNOBS.items():
        lo, hi = step_limits(float(ov["global"][k]), spec)
        t["global_knobs"][k] = {"abs_min": spec["min"], "abs_max": spec["max"], "what": spec["what"],
                                "max_step": f"{spec['step_abs']:g} absolute", "tonight": [round(lo, 4), round(hi, 4)]}
    return t


if __name__ == "__main__":  # print the current overlay (validated) - no secrets involved
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    P = json.loads((ROOT / "scripts" / "hf_params.json").read_text())
    ov, ok, err = load(OVERLAY_PATH, list(P["universe"]), P)
    print(json.dumps({"ok": ok, "error": err, "hash": overlay_hash(ov), "overlay": ov}, indent=1))
