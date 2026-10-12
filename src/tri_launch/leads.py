"""Lead loading, normalization, splitting and dedupe (spec Stage 2)."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

STANDARD_FIELDS = {
    "email": "email",
    "first_name": "first_name",
    "last_name": "last_name",
    "company_name": "company_name",
    "website": "website",
    "phone": "phone",
    "personalization": "personalization",
    "job_title": "job_title",
}

# CSV column -> variable name used in copy (Instantly payload keys).
VARIABLE_NAMES = {
    "email": "email",
    "first_name": "firstName",
    "last_name": "lastName",
    "company_name": "companyName",
    "website": "website",
    "phone": "phone",
    "personalization": "personalization",
    "job_title": "jobTitle",
}

# Human-readable CSV headers seen in real client files ("First Name", "Company",
# "Job Title", "Company Domain", ...) map to the canonical fields above. Without this,
# {{firstName}} silently resolves to nothing when the header is not snake_case.
FULL_NAME_FIELD = "full_name"

HEADER_ALIASES: dict[str, str] = {
    "email": "email",
    "emailaddress": "email",
    "emailaddress1": "email",
    "workemail": "email",
    "businessemail": "email",
    "contactemail": "email",
    "firstname": "first_name",
    "first": "first_name",
    "givenname": "first_name",
    "forename": "first_name",
    "fname": "first_name",
    "lastname": "last_name",
    "last": "last_name",
    "surname": "last_name",
    "familyname": "last_name",
    "lname": "last_name",
    "company": "company_name",
    "companyname": "company_name",
    "organization": "company_name",
    "organisation": "company_name",
    "org": "company_name",
    "employer": "company_name",
    "accountname": "company_name",
    "website": "website",
    "web": "website",
    "companywebsite": "website",
    "companydomain": "website",
    "domain": "website",
    "url": "website",
    "homepage": "website",
    "phone": "phone",
    "phonenumber": "phone",
    "mobile": "phone",
    "mobilephone": "phone",
    "cell": "phone",
    "cellphone": "phone",
    "workphone": "phone",
    "directphone": "phone",
    "personalization": "personalization",
    "personalisation": "personalization",
    "icebreaker": "personalization",
    "customline": "personalization",
    "jobtitle": "job_title",
    "title": "job_title",
    "role": "job_title",
    "position": "job_title",
    "jobposition": "job_title",
    "fullname": FULL_NAME_FIELD,
    "contactname": FULL_NAME_FIELD,
    "contactfullname": FULL_NAME_FIELD,
}


def _norm_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (header or "").lower())


def canonical_field(header: str) -> Optional[str]:
    """Canonical lead field for a CSV header, or None for a custom variable."""
    return HEADER_ALIASES.get(_norm_header(header))


def variable_for_header(header: str) -> str:
    """Copy variable a CSV header feeds (``First Name`` -> ``firstName``)."""
    field = canonical_field(header)
    return VARIABLE_NAMES.get(field, header) if field else header


def email_header(headers: list[str]) -> Optional[str]:
    for header in headers:
        if canonical_field(header) == "email":
            return header
    return None


def _human_header(header: str, canonical_variable: str) -> bool:
    """Keep human headers ("First Name") usable in copy; skip "first_name" duplicates."""
    return header != canonical_variable and any(
        ch.isupper() or ch.isspace() for ch in header or ""
    )


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(normalize_email(value)))


def read_csv(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    p = Path(path)
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        rows = [{k.strip(): (v or "") for k, v in row.items()} for row in reader]
    found = email_header(headers)
    if found and found != "email":
        for row in rows:
            if not (row.get("email") or "").strip():
                row["email"] = row.get(found) or ""
    return headers, rows


def row_to_lead(
    row: dict[str, str],
    headers: list[str],
    *,
    list_name: str,
    variable_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """CSV row -> Instantly leads/add item. Caller must have validated the email."""
    lead: dict[str, Any] = {"email": normalize_email(row.get("email", ""))}
    custom: dict[str, Any] = {"list_name": list_name}
    standard: dict[str, str] = {}
    full_name = ""
    explicit_standard, explicit_custom, claimed = _explicit_mapping(
        row, headers, variable_map or {}
    )
    for header in headers:
        if not header:
            continue
        if header in claimed:
            continue  # handled by the operator's explicit mapping below
        value = (row.get(header) or "").strip()
        field = canonical_field(header)
        if field == FULL_NAME_FIELD:
            if value and not full_name:
                full_name = value
            continue
        if field == "email":
            continue
        if field is not None:
            if value and not standard.get(field):
                standard[field] = value
            canonical_variable = VARIABLE_NAMES.get(field, header)
            if value and _human_header(header, canonical_variable):
                custom.setdefault(header, value)
            continue
        if value:
            custom.setdefault(header, value)
    if full_name:
        parts = full_name.split()
        if parts and not standard.get("first_name"):
            standard["first_name"] = parts[0]
        if len(parts) > 1 and not standard.get("last_name"):
            standard["last_name"] = parts[-1]
        custom.setdefault("fullName", full_name)
        custom.setdefault("full_name", full_name)
    standard.update(explicit_standard)  # the explicit mapping always wins
    custom.update(explicit_custom)
    for field, value in standard.items():
        lead[STANDARD_FIELDS[field]] = value
    lead["custom_variables"] = custom
    return lead


def _explicit_mapping(
    row: dict[str, str], headers: list[str], variable_map: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Apply the operator's variable mapping: {copy variable -> CSV column}.

    Returns (standard fields, custom variables, claimed CSV headers).
    """
    by_lower = {h.strip().lower(): h for h in headers}
    standard: dict[str, str] = {}
    custom: dict[str, str] = {}
    claimed: set[str] = set()
    for variable, column in (variable_map or {}).items():
        header = by_lower.get((column or "").strip().lower())
        if header is None or header == "email":
            continue
        claimed.add(header)
        value = (row.get(header) or "").strip()
        field = canonical_field(variable)
        if field in STANDARD_FIELDS and field != "email":
            standard[field] = value
        else:
            custom[variable] = value
    return standard, custom, claimed


def row_to_values(
    row: dict[str, str],
    headers: list[str],
    *,
    list_name: str,
    variable_map: dict[str, str] | None = None,
) -> dict[str, str]:
    """Values used for QA rendering/coverage (firstName, companyName, custom as-is)."""
    values: dict[str, str] = {
        "list_name": list_name,
        "email": (row.get("email") or "").strip(),
    }
    full_name = ""
    for header in headers:
        if not header:
            continue
        value = (row.get(header) or "").strip()
        field = canonical_field(header)
        if field == FULL_NAME_FIELD:
            if value and not full_name:
                full_name = value
            continue
        name = VARIABLE_NAMES.get(field) if field else header
        if name and (name not in values or (not values[name].strip() and value)):
            values[name] = value
        if (
            field is not None
            and field != "email"
            and header not in values
            and _human_header(header, name or header)
        ):
            values[header] = value
    if full_name:
        parts = full_name.split()
        if parts and not values.get("firstName"):
            values["firstName"] = parts[0]
        if len(parts) > 1 and not values.get("lastName"):
            values["lastName"] = parts[-1]
        values.setdefault("fullName", full_name)
    by_lower = {h.strip().lower(): h for h in headers}
    for variable, column in (variable_map or {}).items():
        header = by_lower.get((column or "").strip().lower())
        if header is None:
            continue
        values[variable] = (row.get(header) or "").strip()
    return values


@dataclass
class Removal:
    email: str
    reason: str
    segment: str
    detail: str = ""


@dataclass
class SegmentReport:
    rows_in: int = 0
    invalid: int = 0
    duplicates: int = 0
    moved_out: int = 0
    moved_in: int = 0
    rows_out: int = 0


@dataclass
class SegmentLeads:
    slug: str
    name: str
    leads: list[dict[str, Any]] = field(default_factory=list)
    values: list[dict[str, str]] = field(default_factory=list)
    report: SegmentReport = field(default_factory=SegmentReport)


@dataclass
class LeadBuild:
    segments: dict[str, SegmentLeads] = field(default_factory=dict)
    unassigned: list[dict[str, str]] = field(default_factory=list)
    removals: list[Removal] = field(default_factory=list)


def _dedupe_in_segment(
    rows: list[dict[str, str]], slug: str, build: LeadBuild
) -> list[dict[str, str]]:
    seen: dict[str, int] = {}
    kept: list[dict[str, str]] = []
    report = build.segments[slug].report
    for row in rows:
        email = normalize_email(row.get("email", ""))
        if not is_valid_email(email):
            report.invalid += 1
            build.removals.append(Removal(email or "(missing)", "invalid-email", slug))
            continue
        if email in seen:
            seen[email] += 1
            report.duplicates += 1
            build.removals.append(
                Removal(email, "duplicate-in-segment", slug, f"occurrence {seen[email]}")
            )
            continue
        seen[email] = 1
        kept.append(row)
    return kept


def build_leads(
    *,
    segments: Iterable[Any],
    lead_mode: str,
    split_column: str = "",
    dedupe_across_segments: bool = True,
    base_dir: str | Path = ".",
    variable_map: dict[str, str] | None = None,
) -> LeadBuild:
    """Load every segment's leads, normalize, split (mode B), and dedupe by priority."""
    build = LeadBuild()
    base = Path(base_dir)
    seg_list = list(segments)
    for seg in seg_list:
        build.segments[seg.slug] = SegmentLeads(slug=seg.slug, name=seg.name)

    raw: dict[str, tuple[list[str], list[dict[str, str]]]] = {}
    if lead_mode == "split_column":
        headers, all_rows = read_csv(base / "leads" / "all_leads.csv")
        if split_column not in headers:
            raise ValueError(f"split_column {split_column!r} not found in all_leads.csv")
        by_slug = {s.slug: [] for s in seg_list}
        by_name = {s.name.strip().lower(): s.slug for s in seg_list}
        for row in all_rows:
            key = (row.get(split_column) or "").strip()
            slug = key if key in by_slug else by_name.get(key.lower())
            if not slug:
                build.unassigned.append(row)
                continue
            by_slug[slug].append(row)
        for seg in seg_list:
            raw[seg.slug] = (headers, by_slug[seg.slug])
    else:
        for seg in seg_list:
            headers, rows = read_csv(base / seg.leads)
            raw[seg.slug] = (headers, rows)

    for seg in seg_list:
        headers, rows = raw[seg.slug]
        build.segments[seg.slug].report.rows_in = len(rows)
        kept = _dedupe_in_segment(rows, seg.slug, build)
        build.segments[seg.slug].report.rows_out = len(kept)
        build.segments[seg.slug].leads = [
            row_to_lead(r, headers, list_name=seg.name, variable_map=variable_map)
            for r in kept
        ]
        build.segments[seg.slug].values = [
            row_to_values(r, headers, list_name=seg.name, variable_map=variable_map)
            for r in kept
        ]

    if dedupe_across_segments and len(seg_list) > 1:
        order = {s.slug: (s.priority, i) for i, s in enumerate(seg_list)}
        owner: dict[str, str] = {}
        for seg in sorted(seg_list, key=lambda s: order[s.slug]):
            sl = build.segments[seg.slug]
            keep_leads, keep_values = [], []
            for lead, values in zip(sl.leads, sl.values):
                email = lead["email"]
                if email in owner:
                    sl.report.moved_out += 1
                    build.segments[owner[email]].report.moved_in += 1
                    build.removals.append(
                        Removal(email, "cross-segment-dedupe", seg.slug, f"kept in {owner[email]}")
                    )
                    continue
                owner[email] = seg.slug
                keep_leads.append(lead)
                keep_values.append(values)
            sl.leads, sl.values = keep_leads, keep_values
            sl.report.rows_out = len(keep_leads)

    return build
