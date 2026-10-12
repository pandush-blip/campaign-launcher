"""Deterministic sample rendering + mechanical checks (spec Stage 3, render/read)."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from . import spintax


@dataclass
class MechIssue:
    kind: str
    message: str
    level: str = "warning"


CHECKS = (
    ("double-space", re.compile(r"[^\S\n]{2,}"), "double spaces"),
    ("space-before-punctuation", re.compile(r"\s+[,.;:!?]"), "space before punctuation"),
    (
        "missing-space-after-punctuation",
        re.compile(r"[,.!?](?=[A-Za-z])"),
        "missing space after punctuation",
    ),
    ("doubled-word", re.compile(r"\b(\w{2,})\s+\1\b", re.I), "doubled word"),
    ("a-an", re.compile(r"\ba\s+[aeiouAEIOU]|\ban\s+[^aeiouAEIOU\s\d]", re.I), "a/an mismatch"),
    ("lowercase-sentence-start", re.compile(r"(^|[.!?]\s+)[a-z]"), "lowercase sentence start"),
    ("empty-greeting", re.compile(r"\b(?:Hi|Hey|Hello)\s*[,!]", re.I), "empty greeting"),
    ("unresolved-braces", re.compile(r"\{\{|\}\}"), "leftover {{ or }}"),
    ("stray-pipe", re.compile(r"(?<!\{\{)[^|\n]*\|"), "stray pipe"),
)


def mechanical_issues(text: str) -> list[MechIssue]:
    out: list[MechIssue] = []
    for kind, pattern, label in CHECKS:
        if kind == "stray-pipe":
            # only a pipe outside any remaining block matters; unresolved blocks are
            # already reported, so skip this check when braces are present
            if "{{" in text:
                continue
        if pattern.search(text or ""):
            for m in pattern.finditer(text or ""):
                snippet = text[max(0, m.start() - 25) : m.end() + 25].replace("\n", " ")
                out.append(MechIssue(kind, f"{label}: …{snippet}…"))
                break
    empty_greeting = re.search(r"\b(?:Hi|Hey|Hello)\s*(?:,)\s*(?:\n|$)", text or "", re.I)
    if empty_greeting:
        out.append(MechIssue("empty-greeting-line", "greeting followed by an empty line"))
    return out


@dataclass
class Sample:
    label: str
    text: str
    lead_email: str = ""


def generate_samples(
    text: str,
    lead_values: list[dict],
    *,
    seed: int,
    spins: int = 10,
    leads: int = 5,
) -> list[Sample]:
    """10 seeded random spins over up to 5 leads, plus shortest/longest renderings."""
    rng = random.Random(seed)
    pool = list(lead_values)
    rng.shuffle(pool)
    pool = pool[:leads] or [{}]
    samples: list[Sample] = []
    for i in range(spins):
        values = pool[i % len(pool)]
        samples.append(
            Sample(f"spin {i + 1}", spintax.render(text, values, rng=rng), values.get("email", ""))
        )
    base = pool[0]
    samples.append(Sample("shortest options", spintax.render_extreme(text, base, longest=False)))
    samples.append(Sample("longest options", spintax.render_extreme(text, base, longest=True)))
    return samples
