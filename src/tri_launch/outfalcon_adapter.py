"""Translate the launcher's canonical copy/leads into OutFalcon's shapes.

OutFalcon (SendingOS) uses:
* merge tags ``{{first_name}}`` / ``{{company}}`` / custom keys ``{{category}}``
* spintax ``{option a|option b}`` (Instantly-style ``{{RANDOM | a | b}}`` must be converted)
* ``{% if %}`` conditionals and ``sender_name`` / ``sender_email`` / ``sender_signature``

Instantly-style date scriptlets (``{# 'now' | date: '%s' | plus: N | date: '%B %d, %Y' #}``)
are not part of the OutFalcon template engine - it renders fragments of them as literal
text. They are replaced with the concrete date (today + N seconds) when the copy is
written, which is what the scriptlet would have rendered at send time for a launch
happening now.

The launcher keeps one canonical copy format (Instantly-style, what the editor and QA
use) and converts it here when writing to OutFalcon.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from . import spintax
from .body_format import text_to_div_html
from .leads import VARIABLE_NAMES, canonical_field, variable_for_header

# Canonical copy variable -> OutFalcon merge token.
VARIABLE_MAP: dict[str, str] = {
    "firstName": "first_name",
    "lastName": "last_name",
    "companyName": "company",
    "jobTitle": "title",
    "website": "website",
    "phone": "phone",
    "email": "email",
    "sendingAccountFirstName": "sender_name",
    "sendingAccountLastName": "sender_name",
    "sendingAccountEmail": "sender_email",
    "accountSignature": "sender_signature",
}

_VARIABLE_LOOKUP = {name.lower(): token for name, token in VARIABLE_MAP.items()}
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")
_DATE_SCRIPTLET_RE = re.compile(r"\{#\s*(.*?)\s*#\}")


def sanitize_variable(name: str) -> str:
    """OutFalcon merge tags are identifiers - spaces and punctuation are not allowed.

    `software type` -> `software_type`, `ideal buyers` -> `ideal_buyers`. The same
    function is applied to the lead's custom-variable keys, so the tag and the stored
    key always match (the API accepts other characters but the template engine does not
    resolve them).
    """
    cleaned = _SANITIZE_RE.sub("_", (name or "").strip()).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    if not cleaned:
        cleaned = "variable"
    if cleaned[0].isdigit():
        cleaned = f"v_{cleaned}"
    return cleaned

# Canonical lead field -> OutFalcon LeadInput field.
LEAD_FIELD_MAP: dict[str, str] = {
    "first_name": "first_name",
    "last_name": "last_name",
    "company_name": "company",
    "job_title": "title",
    "website": "website",
    "phone": "phone",
}

# CSV header alias -> OutFalcon standard destination (for the CSV import mapping).
HEADER_TARGETS: dict[str, str] = {
    "email": "email",
    "first_name": "first_name",
    "last_name": "last_name",
    "company_name": "company",
    "job_title": "title",
    "website": "website",
    "phone": "phone",
}
EXTRA_HEADER_TARGETS = {
    "linkedinprofile": "linkedin",
    "companylinkedin": "company_linkedin",
    "city": "city",
    "stateorprovince": "state",
    "country": "location",
}

# Canonical copy variable -> destination inside the CSV import mapping.
COPY_VARIABLE_DESTINATIONS: dict[str, str] = {
    "firstName": "first_name",
    "lastName": "last_name",
    "companyName": "company",
    "jobTitle": "title",
    "website": "website",
    "phone": "phone",
    "email": "email",
}


def outfalcon_variable(name: str) -> str:
    """Canonical copy variable -> OutFalcon merge token (case-insensitive).

    `CompanyName` -> `company`, `FirstName` -> `first_name`, `software type` ->
    `software_type`, unknown identifiers pass through unchanged.
    """
    token = VARIABLE_MAP.get(name)
    if token is None:
        token = _VARIABLE_LOOKUP.get((name or "").strip().lower())
    if token is not None:
        return token
    return sanitize_variable(name)


def _convert_nodes(nodes: list[spintax.Node], warnings: list[str], fallbacks: str) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
            continue
        if isinstance(node, spintax.Random):
            options = [
                _convert_nodes(option, warnings, fallbacks) for option in node.options
            ]
            out.append("{" + "|".join(options) + "}")
            continue
        # Var
        mapped = outfalcon_variable(node.name)
        if node.fallback is None:
            out.append("{{" + mapped + "}}")
            continue
        fallback_text = _convert_nodes(node.fallback, warnings, fallbacks)
        if fallbacks == "conditional":
            out.append(
                "{% if " + mapped + " %}{{" + mapped + "}}{% else %}"
                + fallback_text
                + "{% endif %}"
            )
        else:
            warnings.append(f"fallback for {{{{{node.name}}}}} dropped (OutFalcon blanks empty values)")
            out.append("{{" + mapped + "}}")
    return "".join(out)


def to_outfalcon_template(
    text: str, *, fallbacks: str = "drop", now: Optional[datetime] = None
) -> tuple[str, list[str]]:
    """Convert one canonical template (subject or body) to OutFalcon syntax."""
    nodes, issues = spintax.parse(text or "")
    warnings = [
        f"spintax issue: {issue.message}" for issue in issues if issue.level == "error"
    ]
    converted = _convert_nodes(nodes, warnings, fallbacks)
    base = now or datetime.now(timezone.utc)
    converted = _DATE_SCRIPTLET_RE.sub(
        lambda match: _resolve_date_scriptlet(match.group(1), warnings, base), converted
    )
    seen: set[str] = set()
    unique: list[str] = []
    for item in warnings:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return converted, unique


def _resolve_date_scriptlet(
    inner: str, warnings: list[str], now: datetime
) -> str:
    """`'now' | date: '%s' | plus: 2209600 | date: '%B %d, %Y'` -> `November 01, 2026`.

    OutFalcon has no date scriptlets; without this the tag renders as fragments like
    ``plus: 2209600`` or ``date: '%s'`` in the sent email.
    """
    plus_seconds = 0
    fmt: Optional[str] = None
    for part in (piece.strip() for piece in inner.split("|")):
        lowered = part.lower()
        if lowered.startswith("plus:"):
            try:
                plus_seconds = int(float(part.split(":", 1)[1].strip()))
            except ValueError:
                continue
        elif lowered.startswith("date:"):
            fmt = part.split(":", 1)[1].strip().strip("'\"")
    if fmt is None:
        warnings.append(f"date tag {{{{{inner}}}}} dropped (not supported by OutFalcon)")
        return ""
    when = now + timedelta(seconds=plus_seconds)
    try:
        return when.strftime(fmt)
    except ValueError:
        warnings.append(f"date tag {{{{{inner}}}}} dropped (bad format)")
        return ""


def to_outfalcon_subject(subject: str, **kwargs: Any) -> tuple[str, list[str]]:
    return to_outfalcon_template(subject or "", **kwargs)


def to_outfalcon_body(body: str, **kwargs: Any) -> tuple[str, list[str]]:
    """Canonical plain-text body -> OutFalcon `body_html` (editor-style divs)."""
    converted, warnings = to_outfalcon_template(body or "", **kwargs)
    return text_to_div_html(converted), warnings


def to_outfalcon_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Canonical lead payload (leads.build_leads) -> OutFalcon LeadInput."""
    out: dict[str, Any] = {"email": lead.get("email", "")}
    custom = dict(lead.get("custom_variables") or {})
    for source, target in LEAD_FIELD_MAP.items():
        value = lead.get(source)
        if value:
            out[target] = value
    # OutFalcon-specific destinations that come from CSV columns.
    for header, target in EXTRA_HEADER_TARGETS.items():
        for key in list(custom.keys()):
            if key.lower().replace(" ", "") == header:
                if custom[key]:
                    out[target] = custom[key]
                break
    out["custom_variables"] = {
        str(k): str(v) for k, v in custom.items() if v not in (None, "")
    }
    return out


def csv_import_mapping(headers: Iterable[str], variable_map: Optional[dict[str, str]] = None) -> dict[str, str]:
    """CSV header -> OutFalcon destination, including the copy's explicit variable map."""
    mapping: dict[str, str] = {}
    by_lower = {h.strip().lower(): h for h in headers}
    # The copy's explicit mapping wins.
    for variable, column in (variable_map or {}).items():
        header = by_lower.get((column or "").strip().lower())
        if not header:
            continue
        destination = COPY_VARIABLE_DESTINATIONS.get(variable, f"custom:{variable}")
        if destination != "email" and mapping.get(header) == "email":
            continue
        mapping[header] = destination
    # Then auto-map every other column so the workspace keeps all data.
    for header in headers:
        if header in mapping:
            continue
        field = canonical_field(header)
        extra = EXTRA_HEADER_TARGETS.get(header.strip().lower().replace(" ", ""))
        if field == "email":
            mapping.setdefault(header, "email")
            continue
        if extra:
            mapping[header] = extra
            continue
        if field and field in HEADER_TARGETS:
            mapping[header] = HEADER_TARGETS[field]
            continue
        if field in {"personalization", "full_name"}:
            mapping[header] = f"custom:{variable_for_header(header)}"
            continue
        mapping[header] = f"custom:{header}"
    if "email" not in mapping.values():
        for header in headers:
            if canonical_field(header) == "email":
                mapping[header] = "email"
                break
    return mapping


def canonical_field_for_variable(variable: str) -> Optional[str]:
    """Reverse lookup: copy variable -> canonical lead field (firstName -> first_name)."""
    for field, name in VARIABLE_NAMES.items():
        if name == variable:
            return field
    return None


_CSV_STANDARD = (
    ("first_name", "first_name"),
    ("last_name", "last_name"),
    ("company_name", "company"),
    ("job_title", "title"),
    ("website", "website"),
    ("phone", "phone"),
)


def leads_to_csv(leads: list[dict[str, Any]]) -> tuple[str, dict[str, str]]:
    """Canonical lead payloads -> (csv_text, mapping) for the OutFalcon list import."""
    import csv as _csv
    import io

    # Custom keys must be OutFalcon-safe identifiers so the {{tag}} in the copy resolves
    # against the key stored on the lead.
    original_keys: list[str] = []
    for lead in leads:
        for key in (lead.get("custom_variables") or {}):
            if key not in original_keys:
                original_keys.append(key)
    key_map = {key: sanitize_variable(key) for key in original_keys}
    custom_keys: list[str] = []
    for key in original_keys:
        safe = key_map[key]
        if safe not in custom_keys:
            custom_keys.append(safe)
    standard = [
        (source, target)
        for source, target in _CSV_STANDARD
        if any(lead.get(source) for lead in leads)
    ]
    headers = ["email"] + [source for source, _ in standard] + custom_keys
    mapping = {"email": "email"}
    mapping.update({source: target for source, target in standard})
    mapping.update({key: f"custom:{key}" for key in custom_keys})
    buffer = io.StringIO()
    writer = _csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = {"email": lead.get("email", "")}
        for source, _target in standard:
            row[source] = lead.get(source, "")
        custom = lead.get("custom_variables") or {}
        for original, safe in key_map.items():
            value = custom.get(original, "")
            if value not in (None, "") or not row.get(safe):
                row[safe] = value
        writer.writerow(row)
    return buffer.getvalue(), mapping
