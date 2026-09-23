#!/usr/bin/env python3
"""Poll published Jev Pulse package on GetAgent (not Studio paper NAV — no API)."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = ROOT / ".state" / "upload.json"
_cache: dict[str, Any] = {"ts": 0.0, "data": {}}


def _key() -> str:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "GETAGENT_PLAYBOOK_API_KEY":
            return v.strip().strip("'").strip('"')
    return ""


def snapshot() -> dict[str, Any]:
    now = time.time()
    if _cache["data"] and now - float(_cache["ts"]) < 25:
        return _cache["data"]
    meta: dict[str, Any] = {}
    if UPLOAD.is_file():
        try:
            meta = json.loads(UPLOAD.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    vid = str(meta.get("version_id") or "")
    out = {
        "ok": False,
        "name": "jev-pulse",
        "version": meta.get("version") or "?",
        "status": meta.get("status") or "unknown",
        "version_id": vid,
        "strategy_id": meta.get("strategy_id") or "",
        "published_at": meta.get("published_at") or "",
        "paper_nav_api": False,
        "note": "Studio paper NAV is not on the Playbook API. Package identity is.",
    }
    key = _key()
    if key and vid:
        url = f"https://api.bitget.com/api/v1/playbook/detail?version_id={vid}"
        req = urllib.request.Request(url, headers={"ACCESS-KEY": key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = json.loads(resp.read().decode())
            d = body.get("data") or {}
            out.update(
                {
                    "ok": str(body.get("code")) in {"200", "00000"} and d.get("status") == "published",
                    "name": d.get("name") or out["name"],
                    "version": d.get("version") or out["version"],
                    "status": d.get("status") or out["status"],
                    "version_id": d.get("version_id") or vid,
                    "strategy_id": d.get("strategy_id") or out["strategy_id"],
                    "display_name": d.get("display_name") or "Jev Pulse",
                    "official_evidence_kind": d.get("official_evidence_kind"),
                    "execution_mode": d.get("execution_mode"),
                }
            )
        except Exception as exc:
            out["error"] = type(exc).__name__
    _cache["ts"] = now
    _cache["data"] = out
    return out
