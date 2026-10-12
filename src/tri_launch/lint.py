"""Config-driven copy lint (spec Stage 3): em dashes, exclamation, words, links."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import LintConfig

URL_RE = re.compile(r"https?://|www\.", re.I)
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")


@dataclass
class LintIssue:
    kind: str
    message: str
    level: str = "warning"


def lint_text(text: str, cfg: LintConfig) -> list[LintIssue]:
    out: list[LintIssue] = []
    if cfg.ban_em_dash and ("\u2014" in text or "\u2013" in text):
        out.append(LintIssue("em-dash", "em/en dash found (banned by launch.yaml)"))
    if cfg.ban_exclamation and "!" in text:
        out.append(LintIssue("exclamation", "exclamation mark found (banned by launch.yaml)"))
    words = WORD_RE.findall(text or "")
    if len(words) > cfg.max_body_words:
        out.append(
            LintIssue(
                "word-count",
                f"{len(words)} words exceeds max_body_words={cfg.max_body_words}",
            )
        )
    links = URL_RE.findall(text or "")
    if len(links) > cfg.max_links:
        out.append(
            LintIssue("links", f"{len(links)} links exceeds max_links={cfg.max_links}")
        )
    return out
