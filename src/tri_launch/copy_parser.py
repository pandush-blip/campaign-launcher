"""Parser for the hand-written copy file format (spec section 3).

```
# Step 1
delay: 0

## Variant A
Subject: ...
Body:
line 1
line 2

## Variant B
...
```
`delay` is days before the NEXT step. An empty subject on a follow-up means
"send in the same thread" (Phase 0: stored as '').
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


class CopyParseError(ValueError):
    pass


@dataclass
class Variant:
    label: str
    subject: str
    body: str
    line: int


@dataclass
class Step:
    number: int
    delay_days: float
    variants: list[Variant] = field(default_factory=list)


@dataclass
class CopyFile:
    path: str
    steps: list[Step] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


STEP_RE = re.compile(r"^#\s*step\s*(\d+)\s*$", re.I)
VARIANT_RE = re.compile(r"^##\s*(.+?)\s*$")
DELAY_RE = re.compile(r"^delay\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.I)
SUBJECT_RE = re.compile(r"^subject\s*:\s*(.*)$", re.I)
BODY_RE = re.compile(r"^body\s*:\s*$", re.I)


def parse_copy(text: str, source: str = "<string>") -> CopyFile:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = CopyFile(path=source)
    if not any(line.strip() for line in lines):
        out.issues.append("copy file is empty")
        return out

    step: Step | None = None
    variant: Variant | None = None
    section: str | None = None
    body_lines: list[str] = []

    def finish_variant() -> None:
        nonlocal variant, section, body_lines
        if variant is None:
            return
        variant.body = "\n".join(body_lines).strip("\n")
        if not variant.body.strip():
            out.issues.append(
                f"line {variant.line}: variant '{variant.label}' has an empty body"
            )
        assert step is not None
        step.variants.append(variant)
        variant, section, body_lines = None, None, []

    for i, raw in enumerate(lines, start=1):
        line = raw
        m_step = STEP_RE.match(line)
        m_variant = VARIANT_RE.match(line)
        if m_step:
            finish_variant()
            if step is not None:
                out.steps.append(step)
            step = Step(number=int(m_step.group(1)), delay_days=0)
            section = None
            continue
        if m_variant:
            finish_variant()
            if step is None:
                raise CopyParseError(f"{source}:{i}: variant before any '# Step N'")
            variant = Variant(label=m_variant.group(1), subject="", body="", line=i)
            section = "variant-header"
            continue
        if step is None:
            if line.strip():
                raise CopyParseError(
                    f"{source}:{i}: content before the first '# Step N' header"
                )
            continue
        m_delay = DELAY_RE.match(line.strip())
        if m_delay and variant is None:
            step.delay_days = float(m_delay.group(1))
            continue
        if variant is None:
            if line.strip():
                raise CopyParseError(f"{source}:{i}: content outside any '## Variant' block")
            continue
        m_subject = SUBJECT_RE.match(line.strip())
        if m_subject and section in (None, "variant-header", "subject"):
            variant.subject = m_subject.group(1).strip()
            section = "subject"
            continue
        if BODY_RE.match(line.strip()) and section in (None, "variant-header", "subject"):
            section = "body"
            body_lines = []
            continue
        if section == "body":
            body_lines.append(line)
        elif section == "subject" and line.strip():
            variant.subject = f"{variant.subject} {line.strip()}".strip()
        elif line.strip():
            out.issues.append(f"line {i}: ignored text before Subject/Body: {line[:60]!r}")
    finish_variant()
    if step is not None:
        out.steps.append(step)

    if not out.steps:
        out.issues.append("no steps found")
    for s in out.steps:
        if not s.variants:
            out.issues.append(f"step {s.number}: no variants")
        if not any(v.subject.strip() for v in s.variants) and s.number == 1:
            out.issues.append("step 1: every variant has an empty subject")
    return out


def parse_copy_file(path: str | Path) -> CopyFile:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise CopyParseError(f"copy file not found: {p}") from exc
    return parse_copy(text, source=str(p))


def serialize_copy(copy_file: CopyFile) -> str:
    """CopyFile -> the hand-written markdown format (round-trips with parse_copy)."""
    lines: list[str] = []
    for step in copy_file.steps:
        delay = step.delay_days
        delay_text = str(int(delay)) if float(delay).is_integer() else str(delay)
        lines.append(f"# Step {step.number}")
        lines.append(f"delay: {delay_text}")
        lines.append("")
        for variant in step.variants:
            lines.append(f"## {variant.label}")
            lines.append(f"Subject: {variant.subject}")
            lines.append("Body:")
            lines.append(variant.body)
            lines.append("")
    return "\n".join(lines).strip("\n") + "\n"
