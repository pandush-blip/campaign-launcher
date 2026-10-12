"""Instantly workspace clients and their API keys.

Every client of The Revenue Inbox has its own Instantly workspace and API key. The
keys live in environment variables only - they are never sent to the browser, logged,
or committed. The web UI picks a client, the server resolves the matching key here.

Env-var naming: ``INSTANTLY_KEY_<CLIENT>``. The Revenue Inbox itself may also use the
legacy ``INSTANTLY_API_KEY`` variable set up during Phase 0.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .envload import ConfigError

DEFAULT_CLIENT = "The Revenue Inbox"


@dataclass(frozen=True)
class Client:
    name: str
    env_var: str
    fallback_env: tuple[str, ...] = ()
    key: str = ""  # resolved value; never rendered or logged


# Order here is the order the UI shows the clients in.
KNOWN_CLIENTS: tuple[Client, ...] = (
    Client(
        "The Revenue Inbox",
        "INSTANTLY_KEY_THE_REVENUE_INBOX",
        fallback_env=("INSTANTLY_API_KEY", "TEST_INSTANTLY_KEY"),
    ),
    Client("Acme", "INSTANTLY_KEY_ACME"),
    Client("Northwind", "INSTANTLY_KEY_NORTHWIND"),
    Client("Contoso", "INSTANTLY_KEY_CONTOSO"),
    Client("Fabrikam", "INSTANTLY_KEY_FABRIKAM"),
    Client("Globex", "INSTANTLY_KEY_GLOBEX"),
    Client("Initech", "INSTANTLY_KEY_INITECH"),
)

_ENV_RE = re.compile(r"^INSTANTLY_KEY_([A-Z0-9_]+)$")


def _resolve_key(client: Client) -> str:
    for env_name in (client.env_var, *client.fallback_env):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    return ""


def _with_key(client: Client) -> Client:
    key = _resolve_key(client)
    return Client(client.name, client.env_var, client.fallback_env, key) if key else client


def _pretty_name(env_var: str) -> str:
    suffix = env_var[len("INSTANTLY_KEY_") :]
    return suffix.replace("_", " ").strip().title()


def list_clients() -> list[Client]:
    """Every known client plus any extra ``INSTANTLY_KEY_*`` set in the environment."""
    resolved: list[Client] = [_with_key(c) for c in KNOWN_CLIENTS]
    known_envs = {c.env_var for c in KNOWN_CLIENTS}
    extra: list[Client] = []
    for env_var in sorted(os.environ):
        m = _ENV_RE.match(env_var)
        if not m or env_var in known_envs:
            continue
        value = (os.environ.get(env_var) or "").strip()
        extra.append(
            Client(_pretty_name(env_var), env_var, key=value)
            if value
            else Client(_pretty_name(env_var), env_var)
        )
    return resolved + extra


def configured_clients() -> list[Client]:
    return [c for c in list_clients() if c.key]


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def resolve_client(ref: Optional[str]) -> Client:
    """Resolve a client by display name, env-var name, or slug.

    An empty ref resolves to the default client (The Revenue Inbox).
    """
    clients = list_clients()
    if not ref or not str(ref).strip():
        for client in clients:
            if client.name == DEFAULT_CLIENT:
                return client
        raise ConfigError("no default client configured")
    wanted = str(ref).strip()
    for client in clients:
        if wanted in {client.name, client.env_var} or _normalize(wanted) == _normalize(client.name):
            return client
    known = ", ".join(c.name for c in clients)
    raise ConfigError(f"unknown client {wanted!r}. Configured clients: {known}")


def client_key(ref: Optional[str]) -> tuple[Client, str]:
    """Return (client, key). Raises ConfigError when the key is missing."""
    client = resolve_client(ref)
    if not client.key:
        raise ConfigError(
            f"{client.name}: no Instantly API key configured. Set {client.env_var} "
            "in the server environment (.env)."
        )
    return client, client.key


def client_summaries() -> list[dict[str, object]]:
    """Browser-safe client list: names and env-var names only, never keys."""
    return [
        {
            "name": c.name,
            "env_var": c.env_var,
            "configured": bool(c.key),
            "is_default": c.name == DEFAULT_CLIENT,
        }
        for c in list_clients()
    ]
