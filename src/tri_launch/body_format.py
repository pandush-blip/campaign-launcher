"""Convert hand-written copy to Instantly's stored body format.

Phase 0 finding (docs/phase0-findings.md): Instantly stores editor-style HTML -
one `<div>` per paragraph, `<div><br /></div>` between paragraphs, `<br />` for
intra-paragraph line breaks. Raw newlines are stored verbatim (and do not render as
paragraphs), and `<br/><br/>`-joined bodies lose their text content entirely, so that
form must never be used.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from .copy_defaults import ensure_unsubscribe

if TYPE_CHECKING:  # pragma: no cover
    from .copy_parser import CopyFile

_ENTITY = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)")
_BR = re.compile(r"<br\s*/?>", re.I)


def escape_text(text: str) -> str:
    """Escape &, < and > but leave existing HTML entities and {{...}} blocks alone."""
    return _ENTITY.sub("&amp;", text).replace("<", "&lt;").replace(">", "&gt;")


def paragraph_html(paragraph: str) -> str:
    return f"<div>{escape_text(paragraph).replace(chr(10), '<br />')}</div>"


def text_to_div_html(text: str) -> str:
    """Plain copy text -> editor-style div HTML (the Phase 0 verified format)."""
    norm = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not norm.strip():
        return ""
    paragraphs = re.split(r"\n[ \t]*\n", norm)
    parts: list[str] = []
    for i, p in enumerate(paragraphs):
        if i:
            parts.append("<div><br /></div>")
        parts.append(paragraph_html(p))
    return "".join(parts)


def div_html_to_plain(body: str) -> str:
    """Inverse-ish for QA/rendering: HTML body -> plain text with newlines."""
    s = re.sub(r"<div><br\s*/?></div>", "\n\n", body or "", flags=re.I)
    s = re.sub(r"</div>", "\n", s, flags=re.I)
    s = re.sub(r"<div[^>]*>", "", s, flags=re.I)
    s = _BR.sub("\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def copy_to_sequences(
    copy_file: "CopyFile",
    *,
    unsubscribe_mode: str = "append",
    unsubscribe_text: str | None = None,
) -> tuple[dict, list[dict]]:
    """Build the PATCH `sequences` payload.

    Returns (payload, notices) where each notice describes an unsubscribe append or
    other transformation, so QA/reporting can show what changed.
    """
    steps = []
    notices: list[dict] = []
    for step in copy_file.steps:
        variants = []
        for v in step.variants:
            body = text_to_div_html(v.body)
            body, changed = ensure_unsubscribe(
                body, mode=unsubscribe_mode, template=unsubscribe_text
            )
            if changed:
                notices.append(
                    {
                        "kind": "unsubscribe-appended",
                        "step": step.number,
                        "variant": v.label,
                    }
                )
            variants.append({"subject": v.subject, "body": body})
        steps.append(
            {
                "type": "email",
                "delay": step.delay_days,
                "delay_unit": "days",
                "variants": variants,
            }
        )
    return {"sequences": [{"steps": steps}]}, notices
