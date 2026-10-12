"""Variable extraction and coverage checking."""

from __future__ import annotations

from dataclasses import dataclass

from . import spintax

# Instantly built-ins seen in live copy or documented in the spec. Custom variables
# from a lead file are matched case-sensitively (spaces allowed: "List Name").
DEFAULT_BUILTINS = {
    "firstName",
    "lastName",
    "companyName",
    "website",
    "phone",
    "personalization",
    "jobTitle",
    "email",
    "sendingAccountFirstName",
    "sendingAccountLastName",
    "sendingAccountEmail",
    "accountSignature",
}

# Filled by Instantly from the sending account at send time - never leads.
SENDER_BUILTINS = {
    "sendingAccountFirstName",
    "sendingAccountLastName",
    "sendingAccountEmail",
    "accountSignature",
}


@dataclass
class Coverage:
    variable: str
    empty: int
    total: int
    pct: float
    status: str  # ok | warn | block


def extract_variables(text: str) -> list[str]:
    return spintax.variables_in_text(text or "")


def unknown_variables(text: str, allowed: set[str]) -> list[str]:
    return [v for v in extract_variables(text) if v not in allowed]


def coverage(
    text: str, lead_values: list[dict], *, threshold_pct: float = 2.0
) -> list[Coverage]:
    """Emptiness of every variable used WITHOUT a fallback across the segment's leads."""
    total = len(lead_values)
    out: list[Coverage] = []
    for name in spintax.variables_without_fallback(text or ""):
        if name in SENDER_BUILTINS:
            continue
        empty = sum(1 for values in lead_values if not str(values.get(name) or "").strip())
        pct = (100.0 * empty / total) if total else 0.0
        if not empty:
            status = "ok"
        elif pct > threshold_pct:
            status = "block"
        else:
            status = "warn"
        out.append(Coverage(name, empty, total, round(pct, 1), status))
    return out
