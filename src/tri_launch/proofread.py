"""Optional AI proofread (DeepSeek) over rendered samples - suggestions only.

Skipped silently-but-visibly when DEEPSEEK_API_KEY is not configured. Never edits copy.
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

SYSTEM_PROMPT = (
    "You are a meticulous cold-email copy editor. You receive rendered samples of an "
    "email sequence (spintax already resolved). Find ONLY material problems: grammar "
    "errors, awkward phrasing caused by a specific spin combination, mechanical issues "
    "(double spaces, wrong a/an, missing space after punctuation), and tone breaks "
    "between options of the same spin block. Every issue must quote the exact offending "
    "text. Do not rewrite whole emails and do not suggest stylistic preferences. "
    "Em dashes and en dashes are banned in this copy and a spaced hyphen is intentional - "
    "never suggest a dash. Merge-field placeholders can render empty in samples; do not "
    "report missing merge data as a problem unless the sentence is broken in every sample. "
    'Reply with JSON: {"issues": [{"step": 1, "variant": "Variant A", "sample": "spin 3", '
    '"quote": "...", "problem": "...", "suggestion": "..."}]}. '
    "If an issue repeats across samples, report it once with the clearest quote."
)


@dataclass
class ProofIssue:
    step: Optional[int] = None
    variant: str = ""
    sample: str = ""
    quote: str = ""
    problem: str = ""
    suggestion: str = ""


@dataclass
class ProofreadResult:
    status: str = "skipped"  # ok | skipped | error
    model: str = ""
    issues: list[ProofIssue] = field(default_factory=list)
    error: Optional[str] = None


def collect_samples(qa, *, max_per_variant: int = 6, max_chars: int = 1200) -> list[dict]:
    out: list[dict] = []
    for seg in qa.segments:
        for v in seg.variants:
            for sample in v.samples[:max_per_variant]:
                out.append(
                    {
                        "segment": seg.slug,
                        "step": v.step,
                        "variant": v.label,
                        "sample": sample.label,
                        "text": sample.text[:max_chars],
                    }
                )
    return out


def build_prompt(samples: list[dict]) -> str:
    blocks = []
    for s in samples:
        blocks.append(
            f"--- step {s['step']} / {s['variant']} / {s['sample']} ---\n{s['text']}"
        )
    return "Rendered samples:\n\n" + "\n\n".join(blocks)


def _parse(content: str) -> list[ProofIssue]:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    data = json.loads(text)
    issues_raw = data.get("issues") if isinstance(data, dict) else data
    issues: list[ProofIssue] = []
    for item in issues_raw or []:
        if not isinstance(item, dict):
            continue
        issues.append(
            ProofIssue(
                step=item.get("step"),
                variant=str(item.get("variant") or ""),
                sample=str(item.get("sample") or ""),
                quote=str(item.get("quote") or ""),
                problem=str(item.get("problem") or ""),
                suggestion=str(item.get("suggestion") or ""),
            )
        )
    return issues


def proofread(
    samples: list[dict],
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 90.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> ProofreadResult:
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not samples:
        return ProofreadResult(status="skipped", error="no samples")
    if not key:
        return ProofreadResult(
            status="skipped", error="DEEPSEEK_API_KEY not set - AI proofread skipped"
        )
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(samples)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(
                DEEPSEEK_URL,
                json=body,
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return ProofreadResult(status="ok", model=model, issues=_parse(content))
    except Exception as exc:  # noqa: BLE001 - never fail the run over proofread
        return ProofreadResult(status="error", model=model, error=f"{type(exc).__name__}: {exc}")
