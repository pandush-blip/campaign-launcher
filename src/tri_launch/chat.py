"""DeepSeek agent for the web chat box.

The agent turns a free-text instruction into campaign inputs (copy files, exclusions,
naming). It never touches Instantly: its output only fills the review form, and nothing
is created until the user clicks "Approve & create".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

COPY_FORMAT = """Copy file format (plain text, exactly this shape):

# Step 1
delay: 0

## Variant A
Subject: {{RANDOM | quick question | idea for {{companyName}}}}
Body:
{{RANDOM | Hi | Hey}} {{firstName | there}},

First paragraph...

{{sendingAccountFirstName}}

## Variant B
Subject: ...
Body:
...

# Step 2
delay: 3

## Variant A
Subject:
Body:
...

Rules: delay = days before the NEXT step. Empty subject = reply in the same thread.
Use spintax {{RANDOM | option 1 | option 2}} and fallbacks {{firstName | there}}.
Variables must exist in the CSV columns or be Instantly built-ins (firstName, lastName,
companyName, website, phone, personalization, email, jobTitle, sendingAccountFirstName,
accountSignature). The launcher appends the client's unsubscribe block automatically -
do not write unsubscribe language yourself. Keep each body under 120 words, no em dashes,
at most one link."""

SYSTEM_PROMPT = (
    "You are the senior cold-email campaign builder for The Revenue Inbox. You receive "
    "the CSV column names, the campaign-list column values with lead counts, and the "
    "user's instruction. You write the email copy (with spintax) and suggest campaign "
    "settings. Work only from the information given; never invent client facts or "
    "case studies. Reply with JSON only, using these keys (omit any you do not change): "
    '{"reply": "<short message to the user, plain text>", '
    '"shared_copy": "<one copy file used by every campaign, in the format below>", '
    '"copy_map": {"<exact list value>": "<copy file for that list only>"}, '
    '"exclude": ["glob patterns for lists to skip"], '
    '"min_leads": <integer>, "naming": "<campaign name template>"}. '
    "Use the exact list values given. If the instruction is missing something you need, "
    "ask a short clarifying question in \"reply\" and omit the other fields.\n\n"
    + COPY_FORMAT
)


@dataclass
class ChatResult:
    status: str = "ok"  # ok | skipped | error
    reply: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    model: str = ""


def _extract_json(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def build_context(preview: dict[str, Any], *, client: str = "", naming: str = "") -> str:
    lines = [f"CSV columns: {', '.join(preview.get('columns') or [])}",
             f"Rows: {preview.get('rows', 0):,}"]
    if client:
        lines.append(f"Client: {client}")
    if naming:
        lines.append(f"Current naming template: {naming}")
    for cand in (preview.get("candidates") or [])[:4]:
        values = ", ".join(f"{v} ({n})" for v, n in cand["top"])
        lines.append(f"Column {cand['column']!r} values: {values}")
    return "\n".join(lines)


def chat(
    message: str,
    preview: dict[str, Any],
    *,
    history: Optional[list[dict[str, str]]] = None,
    client_name: str = "",
    naming: str = "",
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> ChatResult:
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return ChatResult(
            status="skipped", error="DEEPSEEK_API_KEY is not configured for the chat box."
        )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_context(preview, client=client_name, naming=naming)},
    ]
    for turn in (history or [])[-8:]:
        if turn.get("role") in {"user", "assistant"} and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": message})
    body = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport) as http:
            resp = http.post(
                DEEPSEEK_URL, json=body, headers={"Authorization": f"Bearer {key}"}
            )
            resp.raise_for_status()
            data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = _extract_json(content)
        fields = {
            k: parsed[k]
            for k in ("shared_copy", "copy_map", "exclude", "min_leads", "naming")
            if k in parsed and parsed[k] not in (None, "", [], {})
        }
        return ChatResult(
            status="ok",
            reply=str(parsed.get("reply") or "").strip() or "(no reply)",
            fields=fields,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        return ChatResult(status="error", error=f"{type(exc).__name__}: {exc}", model=model)
