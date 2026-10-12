"""DeepSeek drafting for the per-campaign copy builder.

The agent receives the detected lists (one campaign each), the CSV columns, and
the operator's instruction - which may be a short brief or a long paste of finished email
copy for a specific audience. It returns structured copy per list; the app then runs
the deterministic cleanup (variables, spintax, unsubscribe) and QA before anything is
sent to Instantly. The agent never touches the Instantly API.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .leads import variable_for_header

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
MAX_INSTRUCTION_CHARS = 60_000
MAX_OUTPUT_TOKENS = 8192

SYSTEM_PROMPT = """You are the senior cold-email copywriter for The Revenue Inbox, a
done-for-you outbound agency. You receive the lead-list values (each one becomes one
Instantly campaign), the number of leads per list, the CSV column names, and the
operator's instruction. The instruction is often a long paste of finished email copy
plus a note saying which audience should receive it - when that is the case, use their
copy nearly verbatim and only fix obvious mechanics. Do not invent facts, client names,
case studies, numbers, or links that are not in the instruction.

Return JSON only:
{
  "reply": "<short plain-text message to the operator>",
  "campaigns": {
    "<exact list value>": {
      "subject": "<one subject line for the FIRST email only>",
      "step1_variants": ["<first email body>", "<optional second variant>"],
      "followups": [
        {"delay_days": 3, "variants": ["<follow-up body>"]}
      ]
    }
  }
}

Rules:
- One campaign per list value you were given; use the exact list values as keys.
- The subject belongs to step 1 (the first email) only. Follow-ups are replies in the
  same thread and have no subject.
- Write plain email copy. Do not write unsubscribe language - the app appends the
  operator's own unsubscribe block automatically. Do not add spintax unless a greeting
  variation helps; the app validates and repairs spintax anyway.
- Variables: only use {{firstName | there}}, {{companyName | your company}},
  {{jobTitle | your role}} when the matching column exists, plus
  {{sendingAccountFirstName}}, {{sendingAccountLastName}}, {{accountSignature}}.
  Always include the fallback shown.
- Keep each email body under 120 words, no em dashes, at most one link, one clear ask.
- Use the number of step-1 variants the operator's instruction implies per list (some
  lists get one, some get two or more, as described). If the instruction is silent for a
  list, use one. Variants of the same step must be genuinely different (different angle
  or structure), not paraphrases.
- When follow-ups are requested, each follow-up adds one new angle or a short nudge and
  keeps the ask easy to answer.
- If the operator's instruction only covers some lists, draft those and leave the rest
  out of "campaigns"; say so in "reply".
"""


@dataclass
class DraftCampaign:
    value: str
    subject: str = ""
    step1_variants: list[str] = field(default_factory=list)
    followups: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "step1_variants": self.step1_variants,
            "followups": self.followups,
        }


@dataclass
class DraftResult:
    status: str = "ok"  # ok | skipped | error
    reply: str = ""
    campaigns: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: Optional[str] = None
    model: str = ""


def build_context(
    lists: list[dict[str, Any]],
    *,
    columns: list[str] | None = None,
    client: str = "",
    follow_ups: bool = False,
    follow_up_steps: int = 1,
    variants: int = 1,
    existing: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    lines = [f"Client: {client or '(not set)'}"]
    if columns:
        mapped = [
            f"{c} -> {{{{{variable_for_header(c)}}}}}"
            for c in columns
            if variable_for_header(c) != c
        ]
        lines.append(f"CSV columns: {', '.join(columns)}")
        if mapped:
            lines.append("Mapped lead variables: " + "; ".join(mapped))
    if variants > 0:
        lines.append(f"Requested variants per step: {variants}")
    else:
        lines.append(
            "Variants per step: decide per list from the instruction (some lists may get "
            "one, some two or more); do not default to one when the instruction says otherwise"
        )
    if follow_ups:
        lines.append(f"Requested follow-up steps: {max(1, follow_up_steps)}")
    else:
        lines.append("Requested follow-up steps: 0 (step 1 only)")
    lines.append("Lists (campaigns to draft):")
    for item in lists:
        lines.append(f"- {item.get('value')!r} - {int(item.get('leads') or 0)} leads")
    lines.append(
        "Only the lists above are in scope for this request. If the instruction mentions "
        "other lists, ignore those sections - they are handled in a separate request."
    )
    if existing:
        lines.append("")
        lines.append("Existing copy per list (keep what the operator already wrote unless they ask for changes):")
        for value, copy in list(existing.items())[:20]:
            lines.append(json.dumps({"list": value, **copy})[:4000])
    return "\n".join(lines)


def _extract_json(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _scan_object_end(text: str, start: int) -> Optional[int]:
    """Index of the '}' closing the object that starts at ``start`` (None if cut off)."""
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def salvage_campaigns(content: str) -> dict[str, Any]:
    """Pull the complete campaign objects out of a truncated JSON response.

    DeepSeek cuts long responses off when they hit the output limit. Everything before
    the cut is still usable: this scans ``"campaigns": {...}`` and keeps every campaign
    whose object closed before the truncation point.
    """
    match = re.search(r'"campaigns"\s*:\s*\{', content or "")
    if not match:
        return {}
    out: dict[str, Any] = {}
    i = match.end()
    depth = 1
    while i < len(content) and depth >= 1:
        ch = content[i]
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if ch != '"':
            i += 1
            continue
        # key string
        j = i + 1
        escape = False
        while j < len(content):
            if escape:
                escape = False
            elif content[j] == "\\":
                escape = True
            elif content[j] == '"':
                break
            j += 1
        if j >= len(content):
            break
        key = content[i : j + 1]
        k = j + 1
        while k < len(content) and content[k] in " \t\r\n":
            k += 1
        if k >= len(content) or content[k] != ":":
            i = j + 1
            continue
        k += 1
        while k < len(content) and content[k] in " \t\r\n":
            k += 1
        if k >= len(content) or content[k] != "{":
            i = j + 1
            continue
        end = _scan_object_end(content, k)
        if end is None:
            break  # truncated inside this campaign
        try:
            parsed = json.loads("{" + key + ":" + content[k : end + 1] + "}")
            if isinstance(parsed, dict):
                out.update(parsed)
        except Exception:  # noqa: BLE001 - skip unusable fragments
            pass
        i = end + 1
    return out


def _parse_campaigns(data: dict[str, Any], allowed: list[str]) -> dict[str, dict[str, Any]]:
    raw = data.get("campaigns")
    if not isinstance(raw, dict):
        return {}
    by_norm = {re.sub(r"\s+", " ", v).strip().lower(): v for v in allowed}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        exact = by_norm.get(re.sub(r"\s+", " ", str(key)).strip().lower(), str(key))
        variants = [str(v).strip() for v in (value.get("step1_variants") or value.get("variants") or []) if str(v).strip()]
        followups: list[dict[str, Any]] = []
        for fu in value.get("followups") or []:
            if not isinstance(fu, dict):
                continue
            fu_variants = [str(v).strip() for v in (fu.get("variants") or []) if str(v).strip()]
            if not fu_variants:
                continue
            try:
                delay = float(fu.get("delay_days", 3))
            except (TypeError, ValueError):
                delay = 3.0
            followups.append({"delay_days": max(0.0, delay), "variants": fu_variants})
        if not variants and not followups:
            continue
        out[exact] = {
            "subject": str(value.get("subject") or "").strip(),
            "step1_variants": variants,
            "followups": followups,
        }
    return out


def draft(
    instruction: str,
    lists: list[dict[str, Any]],
    *,
    columns: list[str] | None = None,
    client: str = "",
    follow_ups: bool = False,
    follow_up_steps: int = 1,
    variants: int = 1,
    existing: Optional[dict[str, dict[str, Any]]] = None,
    history: Optional[list[dict[str, str]]] = None,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 180.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> DraftResult:
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return DraftResult(status="skipped", error="DEEPSEEK_API_KEY is not configured.")
    if not instruction.strip():
        return DraftResult(status="error", error="describe what you want first")
    if not lists:
        return DraftResult(status="error", error="no lists selected")
    context = build_context(
        lists,
        columns=columns,
        client=client,
        follow_ups=follow_ups,
        follow_up_steps=follow_up_steps,
        variants=variants,
        existing=existing,
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]
    for turn in (history or [])[-6:]:
        if turn.get("role") in {"user", "assistant"} and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])[:8000]})
    messages.append({"role": "user", "content": instruction[:MAX_INSTRUCTION_CHARS]})
    body = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.35,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport) as http:
            resp = http.post(DEEPSEEK_URL, json=body, headers={"Authorization": f"Bearer {key}"})
            resp.raise_for_status()
            data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        try:
            parsed = _extract_json(content)
        except json.JSONDecodeError:
            salvaged = salvage_campaigns(content)
            if not salvaged:
                raise
            parsed = {
                "reply": (
                    "The AI reply was cut off before it finished, so only part of it could "
                    "be used. The remaining lists were retried."
                ),
                "campaigns": salvaged,
            }
        allowed = [str(item.get("value")) for item in lists]
        return DraftResult(
            status="ok",
            reply=str(parsed.get("reply") or "").strip() or "(no reply)",
            campaigns=_parse_campaigns(parsed, allowed),
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        return DraftResult(status="error", error=f"{type(exc).__name__}: {exc}", model=model)


def _batch_size(instruction: str, total: int) -> int:
    """Small batches when the instruction (and therefore the reply) is large."""
    size = len(instruction or "")
    if size > 12_000:
        return 1
    if size > 6_000:
        return 2
    if size > 2_500:
        return 3
    return max(1, min(4, total))


def draft_all(
    instruction: str,
    lists: list[dict[str, Any]],
    **kwargs: Any,
) -> DraftResult:
    """Draft every list, in batches small enough that the JSON reply cannot be cut off.

    A batch failure falls back to drafting its lists one by one; whatever succeeds is
    merged, and the reply names any list that could not be drafted.
    """
    key = kwargs.pop("api_key", None) or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return DraftResult(status="skipped", error="DEEPSEEK_API_KEY is not configured.")
    if not instruction.strip():
        return DraftResult(status="error", error="describe what you want first")
    if not lists:
        return DraftResult(status="error", error="no lists selected")

    size = _batch_size(instruction, len(lists))
    batches = [lists[i : i + size] for i in range(0, len(lists), size)]
    campaigns: dict[str, Any] = {}
    replies: list[str] = []
    failed: list[str] = []
    model_used = str(kwargs.get("model") or DEFAULT_MODEL)

    def merge(result: DraftResult) -> None:
        nonlocal model_used
        if result.model:
            model_used = result.model
        campaigns.update(result.campaigns or {})
        text = (result.reply or "").strip()
        if text and text not in replies:
            replies.append(text)

    for batch in batches:
        result = draft(instruction, batch, api_key=key, **kwargs)
        if result.status == "skipped":
            return result
        if result.status == "ok":
            merge(result)
            missing = [
                item for item in batch if str(item.get("value")) not in (result.campaigns or {})
            ]
            for item in missing:
                one = draft(instruction, [item], api_key=key, **kwargs)
                if one.status == "ok":
                    merge(one)
                else:
                    failed.append(str(item.get("value")))
            continue
        if len(batch) == 1:
            failed.append(str(batch[0].get("value")))
            continue
        for item in batch:
            one = draft(instruction, [item], api_key=key, **kwargs)
            if one.status == "ok":
                merge(one)
            else:
                failed.append(str(item.get("value")))

    if not campaigns:
        return DraftResult(
            status="error",
            error="the AI could not draft any list - try again or draft in smaller groups",
            model=model_used,
        )
    reply = " ".join(replies).strip()
    if failed:
        reply += (" " if reply else "") + "Could not draft: " + ", ".join(failed) + "."
    return DraftResult(
        status="ok",
        reply=reply or f"Drafted {len(campaigns)} campaign(s).",
        campaigns=campaigns,
        model=model_used,
    )
