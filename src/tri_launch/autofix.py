"""Deterministic copy cleanup applied to what the operator writes by hand.

The operator writes plain email copy. This module takes care of the technical bits the
pipeline needs, and returns one ``Fix`` per change so the app can show exactly what
was adjusted (never silent):

* missing variable fallbacks (``{{firstName}}`` -> ``{{firstName | there}}``)
* lowercase spin keyword (``{{random | a | b}}`` -> ``{{RANDOM | a | b}}``)
* em/en dashes -> hyphen (launch lint bans dashes)
* double spaces, space before punctuation, missing space after punctuation
* a small spintax variation on the greeting (``Hi`` -> ``{{RANDOM | Hi | Hey | Hello}}``)

Unknown variables and unbalanced braces are *not* fixed here - QA blocks those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Only variables with a safe generic fallback are auto-fixed.
VARIABLE_FALLBACKS: dict[str, str] = {
    "firstName": "there",
    "companyName": "your company",
    "jobTitle": "your role",
    "senderName": "the team",
}

_BARE_VAR = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9 _-]*?)\s*\}\}")
_LOWER_RANDOM = re.compile(r"\{\{\s*random(\s*\|)")
_DOUBLE_SPACE = re.compile(r"[^\S\n]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"[^\S\n]+([,.;:!?])")
_MISSING_SPACE = re.compile(r"([,;:!?])([A-Za-z])")
_GREETING = re.compile(r"^(Hi|Hey|Hello)\b([^\n]{0,80})$", re.I)
_EMAILISH = re.compile(r"@|://|www\.", re.I)


@dataclass(frozen=True)
class Fix:
    kind: str
    detail: str

    def describe(self) -> str:
        return f"{self.kind}: {self.detail}"

    def __str__(self) -> str:  # convenience for tests/logging
        return self.describe()


def _inside_spans(line: str) -> list[bool]:
    """True where the character is inside a ``{{...}}`` block."""
    mask = [False] * len(line)
    i = 0
    depth = 0
    while i < len(line):
        if line.startswith("{{", i):
            depth += 1
            mask[i] = mask[i + 1] = True
            i += 2
            continue
        if line.startswith("}}", i) and depth:
            depth -= 1
            mask[i] = mask[i + 1] = True
            i += 2
            continue
        mask[i] = depth > 0
        i += 1
    return mask


def _fix_missing_spaces(text: str, fixes: list[Fix]) -> str:
    out_lines: list[str] = []
    count = 0
    for line in text.split("\n"):
        if "{{" not in line and not _MISSING_SPACE.search(line):
            out_lines.append(line)
            continue
        mask = _inside_spans(line)
        chars = list(line)
        for match in reversed(list(_MISSING_SPACE.finditer(line))):
            pos = match.start(1)
            if mask[pos] or mask[pos + 1]:
                continue
            before = line[max(0, pos - 24) : pos + 1]
            after = line[pos + 1 : pos + 25]
            if _EMAILISH.search(before) or _EMAILISH.search(after):
                continue
            chars.insert(pos + 1, " ")
            count += 1
        out_lines.append("".join(chars))
    if count:
        fixes.append(Fix("spacing", f"added a space after punctuation ({count}x)"))
    return "\n".join(out_lines)


def _fix_variable_fallbacks(text: str, fixes: list[Fix]) -> str:
    added: dict[str, int] = {}

    def repl(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        fallback = VARIABLE_FALLBACKS.get(name)
        if fallback is None:
            return match.group(0)
        added[name] = added.get(name, 0) + 1
        return "{{" + name + " | " + fallback + "}}"

    out = _BARE_VAR.sub(repl, text)
    for name, n in sorted(added.items()):
        suffix = f" ({n}x)" if n > 1 else ""
        fixes.append(
            Fix("variables", f"added a fallback to {{{{{name}}}}}{suffix} -> "
                f"{{{{{name} | {VARIABLE_FALLBACKS[name]}}}}}")
        )
    return out


def _fix_dashes(text: str, fixes: list[Fix]) -> str:
    em = text.count("\u2014")
    en = text.count("\u2013")
    if em:
        text = re.sub(r"\s*\u2014\s*", " - ", text)
    if en:
        text = text.replace("\u2013", "-")
    if em or en:
        fixes.append(Fix("lint", f"replaced {em + en} em/en dash(es) with hyphens"))
    return text


def _fix_whitespace(text: str, fixes: list[Fix]) -> str:
    collapsed = len(_DOUBLE_SPACE.findall(text))
    if collapsed:
        fixes.append(Fix("spacing", f"collapsed double spaces ({collapsed}x)"))
    text = _DOUBLE_SPACE.sub(" ", text)
    tight = len(_SPACE_BEFORE_PUNCT.findall(text))
    if tight:
        fixes.append(Fix("spacing", f"removed space before punctuation ({tight}x)"))
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    return text


def _fix_greeting(text: str, fixes: list[Fix]) -> str:
    lines = text.split("\n")
    if not lines:
        return text
    first = lines[0]
    if "{{RANDOM" in first.upper().replace("{{ ", "{{"):
        return text
    match = _GREETING.match(first.strip())
    if not match:
        return text
    rest = match.group(2)
    if not (rest.strip().endswith(",") or rest.strip() == "" or "{{firstName" in rest):
        return text
    lines[0] = "{{RANDOM | Hi | Hey | Hello}}" + rest
    fixes.append(Fix("spintax", "added greeting variation (Hi/Hey/Hello)"))
    return "\n".join(lines)


def fix_subject(text: str, *, ban_em_dash: bool = True) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    out = (text or "").strip()
    if ban_em_dash:
        out = _fix_dashes(out, fixes)
    out = _fix_whitespace(out, fixes)
    out = _fix_variable_fallbacks(out, fixes)
    return out, fixes


def fix_body(
    text: str,
    *,
    spintax_greetings: bool = True,
    ban_em_dash: bool = True,
) -> tuple[str, list[Fix]]:
    """Clean one email body and report every change."""
    fixes: list[Fix] = []
    out = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = "\n".join(line.rstrip() for line in out.split("\n")).strip("\n")
    if not out.strip():
        return out, fixes
    if ban_em_dash:
        out = _fix_dashes(out, fixes)
    out = _fix_whitespace(out, fixes)
    out = _fix_missing_spaces(out, fixes)
    out = _fix_variable_fallbacks(out, fixes)
    if spintax_greetings:
        out = _fix_greeting(out, fixes)
    return out, fixes


def fix_copy(
    *,
    subject: str = "",
    steps: list[dict],
    spintax_greetings: bool = True,
    ban_em_dash: bool = True,
) -> tuple[dict, list[str]]:
    """Fix a structured campaign copy (UI shape) and return (copy, fix descriptions).

    ``steps`` is ``[{"delay_days": 0, "variants": ["body", ...]}, ...]``.
    The subject is only used for step 1 - follow-ups stay in the same thread.
    """
    fixed_subject, subj_fixes = fix_subject(subject or "", ban_em_dash=ban_em_dash)
    fixed_steps: list[dict] = []
    descriptions: list[str] = [f.describe() for f in subj_fixes]
    for index, step in enumerate(steps):
        variants: list[str] = []
        for variant in step.get("variants") or []:
            if index == 0:
                fixed, fixes = fix_body(
                    variant,
                    spintax_greetings=spintax_greetings,
                    ban_em_dash=ban_em_dash,
                )
            else:
                fixed, fixes = fix_body(
                    variant,
                    spintax_greetings=False,
                    ban_em_dash=ban_em_dash,
                )
            variants.append(fixed)
            descriptions += [f.describe() for f in fixes]
        fixed_steps.append(
            {
                "delay_days": step.get("delay_days", 0),
                "variants": variants,
            }
        )
    # de-duplicate identical descriptions while keeping order
    seen: set[str] = set()
    unique: list[str] = []
    for item in descriptions:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return (
        {"subject": fixed_subject, "steps": fixed_steps},
        unique,
    )


def copy_to_markdown(copy: dict) -> str:
    """Structured UI copy -> the launcher's copy-file markdown."""
    subject = (copy.get("subject") or "").strip()
    lines: list[str] = []
    for index, step in enumerate(copy.get("steps") or []):
        delay = step.get("delay_days", 0)
        try:
            delay_text = str(int(float(delay)))
        except (TypeError, ValueError):
            delay_text = "0"
        lines.append(f"# Step {index + 1}")
        lines.append(f"delay: {delay_text}")
        lines.append("")
        variants = step.get("variants") or [""]
        for vi, body in enumerate(variants):
            lines.append(f"## Variant {chr(65 + vi) if vi < 26 else vi + 1}")
            # The subject belongs to the first email only; follow-ups stay in-thread.
            lines.append(f"Subject: {subject if index == 0 else ''}")
            lines.append("Body:")
            lines.append((body or "").strip("\n"))
            lines.append("")
    return "\n".join(lines).strip("\n") + "\n"
