"""Instantly spintax parser / renderer / analyser.

Supported syntax (verified against live campaigns in Phase 0):

* spin blocks       ``{{RANDOM | option A | option B}}``
* fallbacks         ``{{firstName | there}}``
* nesting           ``{{RANDOM | I saw {{companyName | your company}} sells X | ...}}``
* multi-block lines are plain concatenations of the above

The parser is permissive (it stores/labels malformed input instead of crashing) so QA
can report exactly what would break.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional, Union


class SpintaxError(ValueError):
    pass


@dataclass
class Issue:
    level: str  # "error" | "warning"
    message: str
    position: int


@dataclass
class Var:
    name: str
    fallback: Optional[list["Node"]] = None


@dataclass
class Random:
    options: list[list["Node"]] = field(default_factory=list)
    raw: str = ""


Node = Union[str, Var, Random]

MAX_COMBINATIONS = 10**9


def _find_block_end(text: str, start: int) -> int:
    depth = 0
    i = start
    while i < len(text) - 1:
        two = text[i : i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return -1


def _split_top(inner: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    i = 0
    while i < len(inner):
        two = inner[i : i + 2]
        if two == "{{":
            depth += 1
            cur.append(two)
            i += 2
            continue
        if two == "}}":
            depth -= 1
            cur.append(two)
            i += 2
            continue
        if inner[i] == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(inner[i])
        i += 1
    parts.append("".join(cur))
    return parts


def _parse_block(inner: str, pos: int, issues: list[Issue]) -> Node:
    parts = _split_top(inner)
    head = parts[0].strip()
    if head.upper() == "RANDOM":
        if head != "RANDOM":
            issues.append(
                Issue("warning", f"lowercase spin keyword used: {head!r} (position {pos})", pos)
            )
        options = [_parse_nodes(p.strip(), pos, issues) for p in parts[1:]]
        if not options or len(parts) < 3:
            issues.append(Issue("error", f"spin block with fewer than 2 options at {pos}", pos))
        for opt, raw_opt in zip(options, parts[1:]):
            if not raw_opt.strip():
                issues.append(Issue("warning", f"empty spin option at position {pos}", pos))
        return Random(options=options, raw=f"{{{{{inner}}}}}")
    if not head:
        issues.append(Issue("error", f"empty variable name at position {pos}", pos))
        return Var(name="")
    fallback = None
    if len(parts) > 1:
        fallback = _parse_nodes(" | ".join(parts[1:]).strip(), pos, issues)
    return Var(name=head, fallback=fallback)


def _parse_nodes(text: str, base_pos: int, issues: list[Issue]) -> list[Node]:
    nodes: list[Node] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("{{", i):
            end = _find_block_end(text, i)
            if end < 0:
                issues.append(
                    Issue("error", f"unbalanced '{{{{' at position {base_pos + i}", base_pos + i)
                )
                buf.append(text[i:])
                break
            if buf:
                nodes.append("".join(buf))
                buf = []
            nodes.append(_parse_block(text[i + 2 : end - 2], base_pos + i, issues))
            i = end
            continue
        if text[i] == "{":
            if text.startswith("{", i) and not text.startswith("{{", i):
                nxt = text[i : i + 2]
                if nxt == "{}":
                    issues.append(Issue("warning", f"empty braces at position {base_pos + i}", base_pos + i))
                else:
                    issues.append(
                        Issue("warning", f"single '{{' at position {base_pos + i} (not Instantly syntax)", base_pos + i)
                    )
        elif text[i] == "|":
            issues.append(
                Issue("warning", f"stray '|' outside a block at position {base_pos + i}", base_pos + i)
            )
        buf.append(text[i])
        i += 1
    if buf:
        nodes.append("".join(buf))
    return nodes


def parse(text: str) -> tuple[list[Node], list[Issue]]:
    issues: list[Issue] = []
    nodes = _parse_nodes(text or "", 0, issues)
    return nodes, issues


def analyze(text: str) -> list[Issue]:
    return parse(text)[1]


def _count(nodes: list[Node]) -> int:
    total = 1
    for n in nodes:
        if isinstance(n, Random):
            total *= max(1, sum(_count(o) for o in n.options))
        elif isinstance(n, Var) and n.fallback is not None:
            total *= max(1, _count(n.fallback))
        if total >= MAX_COMBINATIONS:
            return MAX_COMBINATIONS
    return total


def count_combinations(text: str) -> int:
    nodes, _ = parse(text)
    return _count(nodes)


def render(
    text: str,
    values: Optional[dict] = None,
    *,
    rng: Optional[random.Random] = None,
    missing: Optional[list[str]] = None,
) -> str:
    nodes, _ = parse(text)
    return _render_nodes(nodes, values or {}, rng or random.Random(0), missing, longest=None)


def _render_nodes(
    nodes: list[Node],
    values: dict,
    rng: random.Random,
    missing: Optional[list[str]],
    *,
    longest: Optional[bool],
) -> str:
    out: list[str] = []
    for n in nodes:
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, Var):
            v = values.get(n.name)
            if v is not None and str(v).strip() != "":
                out.append(str(v))
            elif n.fallback is not None:
                out.append(_render_nodes(n.fallback, values, rng, missing, longest=longest))
            else:
                if missing is not None:
                    missing.append(n.name)
        else:
            if not n.options:
                continue
            if longest is None:
                choice = rng.choice(n.options)
            else:
                rendered = [
                    _render_nodes(o, values, rng, missing, longest=longest) for o in n.options
                ]
                idx = max(range(len(rendered)), key=lambda i: len(rendered[i]))
                if not longest:
                    idx = min(range(len(rendered)), key=lambda i: len(rendered[i]))
                choice = n.options[idx]
                out.append(rendered[idx])
                continue
            out.append(_render_nodes(choice, values, rng, missing, longest=longest))
    return "".join(out)


def render_extreme(text: str, values: Optional[dict] = None, *, longest: bool) -> str:
    nodes, _ = parse(text)
    return _render_nodes(nodes, values or {}, random.Random(0), None, longest=longest)


def variables_in_text(text: str) -> list[str]:
    """Every {{name...}} referenced, including inside fallbacks and spin options."""
    nodes, _ = parse(text)
    found: list[str] = []

    def walk(ns: list[Node]) -> None:
        for n in ns:
            if isinstance(n, Var):
                if n.name and n.name not in found:
                    found.append(n.name)
                if n.fallback:
                    walk(n.fallback)
            elif isinstance(n, Random):
                for o in n.options:
                    walk(o)

    walk(nodes)
    return found


def variables_without_fallback(text: str) -> list[str]:
    nodes, _ = parse(text)
    found: list[str] = []

    def walk(ns: list[Node]) -> None:
        for n in ns:
            if isinstance(n, Var):
                if n.name and n.fallback is None and n.name not in found:
                    found.append(n.name)
                if n.fallback:
                    walk(n.fallback)
            elif isinstance(n, Random):
                for o in n.options:
                    walk(o)

    walk(nodes)
    return found


def duplicate_option_blocks(text: str) -> int:
    """Count spin blocks that contain the same option more than once."""
    nodes, _ = parse(text)
    count = 0

    def walk(ns: list[Node]) -> None:
        nonlocal count
        for n in ns:
            if isinstance(n, Random):
                rendered = [
                    re.sub(r"\s+", " ", _render_nodes(o, {}, random.Random(0), None, longest=True))
                    .strip()
                    .lower()
                    for o in n.options
                ]
                if len(set(rendered)) < len(rendered):
                    count += 1
                for o in n.options:
                    walk(o)

    walk(nodes)
    return count


def dedupe_spin_options(text: str) -> tuple[str, int]:
    """Drop repeated options inside spin blocks (keeps the first occurrence).

    The AI sometimes pads a block to 3-4 options by repeating the same sentence. That
    would give no real variation, so the duplicates are removed deterministically; the
    block keeps its remaining distinct options.
    """
    out: list[str] = []
    removed = 0
    i = 0
    while i < len(text):
        if text.startswith("{{", i):
            end = _find_block_end(text, i)
            if end < 0:
                out.append(text[i:])
                break
            inner = text[i + 2 : end - 2]
            parts = _split_top(inner)
            head = parts[0].strip()
            if head.upper() == "RANDOM":
                seen: set[str] = set()
                options: list[str] = []
                for raw_option in parts[1:]:
                    key = re.sub(
                        r"\s+",
                        " ",
                        _render_nodes(
                            _parse_nodes(raw_option.strip(), 0, []),
                            {},
                            random.Random(0),
                            None,
                            longest=True,
                        ),
                    ).strip().lower()
                    if not key or key in seen:
                        removed += 1
                        continue
                    seen.add(key)
                    cleaned, nested_removed = dedupe_spin_options(raw_option.strip())
                    removed += nested_removed
                    options.append(cleaned)
                if len(options) == 1:
                    # No variation left after removing duplicates: unwrap the block
                    # instead of emitting an invalid single-option {{RANDOM}}.
                    out.append(options[0])
                elif options:
                    out.append("{{RANDOM | " + " | ".join(options) + "}}")
                i = end
                continue
            out.append(text[i])
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out), removed
