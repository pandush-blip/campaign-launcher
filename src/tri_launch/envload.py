"""Load `.env` and resolve secrets by name. Values are never logged."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


def load_env(env_file: str | Path = ".env", *, optional: bool = False) -> Path:
    p = Path(env_file)
    if not p.is_file():
        if optional:
            return p
        raise ConfigError(
            f"No .env found at {p.resolve()}. Create one (see .env.example) with "
            "INSTANTLY_API_KEY=... and TEST_SETTINGS_CAMPAIGN='<name or id>'."
        )
    load_dotenv(p, override=False)
    return p


def get_api_key(name: str | None = None) -> tuple[str, str]:
    """Return (env var name, key value). The value is never logged by callers."""
    if name:
        value = os.environ.get(name)
        if not value:
            raise ConfigError(f"{name} is empty or missing in .env.")
        return name, value
    for name in ("TEST_INSTANTLY_KEY",):
        if os.environ.get(name):
            return name, os.environ[name]
    candidates = sorted(
        n
        for n, v in os.environ.items()
        if v and (n == "INSTANTLY_API_KEY" or n.startswith("INSTANTLY_KEY_"))
    )
    if len(candidates) == 1:
        return candidates[0], os.environ[candidates[0]]
    if not candidates:
        raise ConfigError(
            "No Instantly API key found in .env. Set INSTANTLY_API_KEY "
            "(or exactly one INSTANTLY_KEY_* variable)."
        )
    raise ConfigError(
        "Ambiguous Instantly API key: found " + ", ".join(candidates) + ". "
        "Set INSTANTLY_API_KEY to pick one."
    )


def get_test_settings_campaign() -> str:
    v = (os.environ.get("TEST_SETTINGS_CAMPAIGN") or "").strip()
    if not v:
        raise ConfigError("TEST_SETTINGS_CAMPAIGN is empty or missing in .env.")
    if v.lower() in {"name-or-id", "changeme", "todo"}:
        raise ConfigError("TEST_SETTINGS_CAMPAIGN still holds the placeholder value.")
    return v
