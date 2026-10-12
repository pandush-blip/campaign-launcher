"""Sequencers (providers) and their client workspaces.

The launcher supports two sending sequencers:

* ``instantly``  - keys in ``INSTANTLY_KEY_*`` (see clients.py)
* ``outfalcon``  - keys in ``OUTFALCON_KEY_*`` (send.outfalcon.com)

The rest of the app (copy, QA, spintax, variable mapping) is provider-agnostic; only the
client/workspace selection, the lead payload writer and the campaign writer differ.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from .clients import (
    Client,
    client_key as instantly_client_key,
    client_summaries,
    list_clients,
    resolve_client,
)
from .envload import ConfigError

DEFAULT_SEQUENCER = "instantly"
SEQUENCERS = (
    {"id": "instantly", "name": "Instantly"},
    {"id": "outfalcon", "name": "OutFalcon"},
)

# Known OutFalcon workspaces (display name -> env var). Extra OUTFALCON_KEY_* variables
# are discovered automatically so new client workspaces show up without code changes.
_OUTFALCON_NAMES = {
    "OUTFALCON_KEY_THE_REVENUE_INBOX": "The Revenue Inbox",
    "OUTFALCON_KEY_HOOLI": "Hooli",
    "OUTFALCON_KEY_VANDELAY": "Vandelay",
}


def _pretty(env_var: str) -> str:
    suffix = env_var[len("OUTFALCON_KEY_") :]
    return _OUTFALCON_NAMES.get(env_var) or suffix.replace("_", " ").strip().title()


def _outfalcon_names() -> list[tuple[str, str]]:
    """[(display name, env var)] - known workspaces first, then any extra ones."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for env_var, name in _OUTFALCON_NAMES.items():
        if os.environ.get(env_var):
            pairs.append((name, env_var))
            seen.add(env_var)
    for env_var in sorted(os.environ):
        if env_var.startswith("OUTFALCON_KEY_") and env_var not in seen:
            value = (os.environ.get(env_var) or "").strip()
            if value:
                pairs.append((_pretty(env_var), env_var))
    return pairs


def normalize_sequencer(value: Optional[str]) -> str:
    text = (value or "").strip().lower()
    if not text:
        return DEFAULT_SEQUENCER
    if text in {"outfalcon", "out_falcon", "falcon", "sendingos", "send"}:
        return "outfalcon"
    if text in {"instantly", "instantly.ai"}:
        return "instantly"
    raise ConfigError(f"unknown sequencer {value!r} - use 'instantly' or 'outfalcon'")


def list_workspaces(sequencer: str) -> list[dict[str, object]]:
    """Browser-safe workspace list for one sequencer (names and env vars only)."""
    seq = normalize_sequencer(sequencer)
    if seq == "instantly":
        return [
            {
                "sequencer": "instantly",
                "name": item["name"],
                "env_var": item["env_var"],
                "configured": item["configured"],
                "is_default": item["is_default"],
            }
            for item in client_summaries()
        ]
    return [
        {
            "sequencer": "outfalcon",
            "name": name,
            "env_var": env_var,
            "configured": bool(os.environ.get(env_var)),
            "is_default": name == "The Revenue Inbox",
        }
        for name, env_var in _outfalcon_names()
    ]


def workspace_key(sequencer: str, ref: Optional[str]) -> tuple[str, str, str]:
    """Return (display name, env var, key) for a workspace reference.

    ``ref`` may be the display name or the env var. Empty picks the default workspace.
    """
    seq = normalize_sequencer(sequencer)
    if seq == "instantly":
        client: Client
        client, key = instantly_client_key(ref)
        return client.name, client.env_var, key

    pairs = _outfalcon_names()
    if not pairs:
        raise ConfigError(
            "No OutFalcon API keys configured. Set OUTFALCON_KEY_THE_REVENUE_INBOX, "
            "OUTFALCON_KEY_HOOLI, OUTFALCON_KEY_VANDELAY in the environment."
        )
    wanted = (ref or "").strip()
    if not wanted:
        for name, env_var in pairs:
            if name == "The Revenue Inbox":
                return name, env_var, os.environ[env_var].strip()
        name, env_var = pairs[0]
        return name, env_var, os.environ[env_var].strip()
    norm = re.sub(r"[^a-z0-9]+", "", wanted.lower())
    for name, env_var in pairs:
        if wanted in {name, env_var} or norm == re.sub(r"[^a-z0-9]+", "", name.lower()):
            value = (os.environ.get(env_var) or "").strip()
            if not value:
                raise ConfigError(f"{name}: {env_var} is empty or missing.")
            return name, env_var, value
    known = ", ".join(name for name, _ in pairs)
    raise ConfigError(f"unknown OutFalcon workspace {wanted!r}. Configured: {known}")


def workspace_summaries() -> dict[str, list[dict[str, object]]]:
    return {seq["id"]: list_workspaces(seq["id"]) for seq in SEQUENCERS}
