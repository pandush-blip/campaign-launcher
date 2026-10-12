"""Hard endpoint allowlist for the Instantly API.

Every request made by this codebase passes through :func:`check_allowed`. A request
whose method/path combination is not explicitly matched raises :class:`AllowlistError`
*before* anything is sent. Activation (the only call that starts sending) is allowed for
exactly one path, ``POST /api/v2/campaigns/{id}/activate``, and only the tracked-campaign
guard in :mod:`tri_launch.instantly` may use it - the explicit "launch all" flow with a
typed confirmation. Nothing else that starts sending is reachable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


class AllowlistError(RuntimeError):
    """Raised when a request is not on the allowlist (nothing was sent)."""


class GuardError(RuntimeError):
    """Raised when a tracked-campaign guard fails (nothing was sent)."""


# Instantly campaign IDs are UUID-shaped (see OpenAPI examples).
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


@dataclass(frozen=True)
class Rule:
    method: str
    pattern: "re.Pattern[str]"
    note: str


def _r(method: str, regex: str, note: str) -> Rule:
    return Rule(method, re.compile(regex), note)


# Exact method+path allowlist. Paths are matched without a query string; callers pass
# query parameters separately, so they can never smuggle an endpoint in a query value.
ALLOWED_RULES: tuple[Rule, ...] = (
    _r("GET", r"^/api/v2/campaigns$", "list/search campaigns"),
    _r("GET", rf"^/api/v2/campaigns/{UUID}$", "read one campaign"),
    _r(
        "POST",
        rf"^/api/v2/campaigns/{UUID}/duplicate$",
        "create a campaign by duplicating the settings campaign",
    ),
    _r("PATCH", rf"^/api/v2/campaigns/{UUID}$", "set sequences on tracked campaigns only"),
    _r(
        "POST",
        rf"^/api/v2/campaigns/{UUID}/variables$",
        "register campaign variables on tracked campaigns only",
    ),
    _r("GET", r"^/api/v2/accounts$", "verify sending accounts exist"),
    _r(
        "GET",
        r"^/api/v2/block-lists-entries$",
        "read the client blocklist (read-only; uploads are checked by Instantly)",
    ),
    _r("POST", r"^/api/v2/leads/add$", "bulk add leads to a tracked campaign"),
    _r("POST", r"^/api/v2/leads/list$", "read leads back"),
    _r(
        "DELETE",
        r"^/api/v2/leads$",
        "delete test leads this tool created (cleanup only, guarded in client)",
    ),
    _r(
        "POST",
        rf"^/api/v2/campaigns/{UUID}/pause$",
        "SAFETY ONLY: pause a tracked campaign that is unexpectedly not draft",
    ),
    _r(
        "POST",
        rf"^/api/v2/campaigns/{UUID}/activate$",
        "LAUNCH: activate a tracked draft campaign (explicit confirmed launch only)",
    ),
    _r("DELETE", rf"^/api/v2/campaigns/{UUID}$", "cleanup of tracked campaigns only"),
)

# Substrings that must never pass, even if a future rule above would match them.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "/emails/test",
    "/emails/reply",
    "/emails/forward",
    "/warmup",
    "/accounts/pause",
)


def check_allowed(method: str, path: str) -> Rule:
    """Return the matching rule or raise AllowlistError without sending anything."""
    method = method.upper()
    if "?" in path or "#" in path:
        raise AllowlistError(f"Path must not contain a query string or fragment: {path!r}")
    for token in FORBIDDEN_SUBSTRINGS:
        if token in path:
            raise AllowlistError(
                f"FORBIDDEN endpoint ({token}) - never allowed: {method} {path}"
            )
    for rule in ALLOWED_RULES:
        if rule.method == method and rule.pattern.match(path):
            return rule
    raise AllowlistError(f"Endpoint not on allowlist: {method} {path}")


def campaign_id_from_path(path: str) -> Optional[str]:
    """Extract a campaign id from an allowlisted campaign path, if present."""
    m = re.match(rf"^/api/v2/campaigns/({UUID})(?:/[^/]+)?$", path)
    return m.group(1) if m else None
