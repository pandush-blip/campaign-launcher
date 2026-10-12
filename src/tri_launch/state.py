"""Local state for campaigns created by this tool.

Spec rule 2: updates and deletes are only allowed on campaign IDs recorded here.
`state/` is gitignored. The file is small JSON and is written atomically.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .allowlist import GuardError


def state_dir() -> Path:
    return Path(os.environ.get("TRI_LAUNCH_STATE_DIR", "state"))


def state_file() -> Path:
    return state_dir() / "created_campaigns.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict[str, Any]:
    f = state_file()
    if not f.exists():
        return {"created": {}, "history": []}
    data = json.loads(f.read_text(encoding="utf-8"))
    data.setdefault("created", {})
    data.setdefault("history", [])
    return data


def _save(data: dict[str, Any]) -> None:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = state_file()
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(f)


def register_campaign(
    campaign_id: str,
    name: str,
    *,
    source_id: Optional[str] = None,
    purpose: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Record a campaign created by this tool. Call immediately after creation."""
    data = load()
    rec = data["created"].get(campaign_id) or {"created_at": _utcnow()}
    rec.update(
        {
            "id": campaign_id,
            "name": name,
            "source_id": source_id,
            "purpose": purpose,
            "last_seen_at": _utcnow(),
        }
    )
    if extra:
        rec.update(extra)
    data["created"][campaign_id] = rec
    _save(data)
    return rec


def is_tracked(campaign_id: str) -> bool:
    return campaign_id in load()["created"]


def require_tracked(campaign_id: str) -> dict[str, Any]:
    rec = load()["created"].get(campaign_id)
    if rec is None:
        raise GuardError(
            f"Campaign {campaign_id} is not in local state; refusing to modify it. "
            "Only campaigns created by this tool may be patched, paused or deleted."
        )
    return rec


def touch(campaign_id: str) -> None:
    """Update last_seen_at for a tracked campaign after a successful readback."""
    data = load()
    rec = data["created"].get(campaign_id)
    if rec is not None:
        rec["last_seen_at"] = _utcnow()
        _save(data)


def mark_deleted(campaign_id: str, *, deleted_at: Optional[str] = None) -> None:
    """Move a campaign out of `created` into the audit `history` list."""
    data = load()
    rec = data["created"].pop(campaign_id, None)
    if rec is not None:
        rec["deleted_at"] = deleted_at or _utcnow()
        data["history"].append(rec)
        _save(data)


def created_ids() -> list[str]:
    return sorted(load()["created"].keys())


def known_campaign(campaign_id: str) -> Optional[dict[str, Any]]:
    """A campaign this tool created, live (created) or already deleted (history)."""
    data = load()
    rec = data["created"].get(campaign_id)
    if rec is not None:
        return rec
    for h in data["history"]:
        if h.get("id") == campaign_id:
            return h
    return None


def add_lead_ids(campaign_id: str, lead_ids: Iterable[str]) -> None:
    """Record lead IDs created by this tool inside a tracked campaign."""
    ids = {i for i in lead_ids if i}
    if not ids:
        return
    data = load()
    rec = data["created"].get(campaign_id)
    if rec is None:
        return
    merged = set(rec.get("lead_ids") or []) | ids
    rec["lead_ids"] = sorted(merged)
    rec["last_seen_at"] = _utcnow()
    _save(data)
