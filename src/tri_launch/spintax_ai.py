"""AI spintax pass: give every email variant natural-sounding spintax.

Runs after the plan review. The model rewrites each variant body with
``{{RANDOM | ...}}`` blocks while keeping the meaning, facts, variables, paragraph
structure and unsubscribe sentences unchanged. Everything is validated with the
deterministic spintax parser/renderer before it is written back; on any validation
error the original copy is kept and the caller reports why.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from . import spintax
from .copy_defaults import UNSUBSCRIBE_RE
from .copy_parser import CopyFile
from .proofread import proofread as proofread_samples
from .render import generate_samples

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
MAX_ATTEMPTS = 3

SYSTEM_PROMPT = """You add spintax to cold emails so that every send reads like a natural,
human variation of the same message.

Input JSON: {"campaign": "<name>", "variants": [{"step": 1, "label": "Variant A",
"subject": "<subject or empty>", "body": "<email body>"}]}

Output JSON only: {"variants": [{"step": 1, "label": "Variant A", "subject": "<same rules
as below>", "body": "<rewritten body>"}]} - exactly the same steps and labels, same order.

Spintax syntax: {{RANDOM | option one | option two | option three}}

Hard rules:
1. Meaning, claims, facts, the request and the call to action stay identical. Never invent
   numbers, names, companies, tools, links or case studies.
2. Every option inside a block must be a complete, grammatical sentence (or a complete
   clause with its own subject and verb) ending with the same punctuation, so that ANY
   combination reads correctly. Never create fragments that depend on another block.
   No comma splices: two independent clauses need a period, semicolon, colon or a dash,
   never just a comma. If an option starts with "Suppose" or "If", join it to the rest
   cleanly ("Suppose an auditor asked tomorrow to prove one requirement - how long would
   your team take to find the evidence?").
3. Keep every {{variable}} character-for-character, including fallbacks such as
   {{firstName | there}}. Never spin a variable and never add or remove one.
4. Keep every [[KEEP_UNSUB_n]] token exactly as-is on its own line, in the same position.
5. Keep the greeting, sign-off/signature and the paragraph and line structure identical.
   Do not merge or split paragraphs. Do not add or remove lines.
6. Add variation to substantial sentences and clauses: aim for 2-4 options per block and
   at least 3 blocks per email, unless the email is very short.
7. Any single rendered version must stay within about 15% of the original length and never
   exceed the configured word limit.
8. No em dashes or en dashes. At most one link per email; a link must stay identical in
   every option that contains it.
8b. Write like a native English speaker. Prefer the word the original used; do not swap
   verbs for near-synonyms that change the nuance (keep "connect", not "link"; keep
   "review", not "examine") unless the whole phrase still sounds natural. Keep the
   original tone (contractions, formality).
9. If a sentence already contains spintax, keep it working and you may add new blocks
   around it. Never nest a new {{RANDOM}} block inside another block.
9b. Never repeat the same option twice in one block. Every option must be genuinely
    different from the others. If you cannot write 3 or 4 truly different options, use 2 -
    never pad a block with repeats.
10. Subjects: if the subject already contains spintax, return it unchanged. If it has no
    spintax, add one short {{RANDOM | ...}} block that keeps the same meaning. Keep
    variables in the subject exactly as they are.
11. Plain text only - no HTML, no markdown, no commentary, no explanation.
"""


@dataclass
class VariantSpec:
    step: int
    variant_index: int
    label: str
    subject: str
    body: str
    keep_lines: dict[str, str] = field(default_factory=dict)


@dataclass
class SpinOutcome:
    status: str = "ok"  # ok | error | skipped
    error: str = ""
    model: str = ""
    attempts: int = 1
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _mask_unsubscribe(body: str) -> tuple[str, dict[str, str]]:
    """Replace unsubscribe sentences with tokens so the model cannot rewrite them."""
    lines = (body or "").split("\n")
    keeps: dict[str, str] = {}
    out: list[str] = []
    for line in lines:
        if line.strip() and UNSUBSCRIBE_RE.search(line):
            token = f"[[KEEP_UNSUB_{len(keeps) + 1}]]"
            keeps[token] = line
            out.append(token)
        else:
            out.append(line)
    return "\n".join(out), keeps


def _restore(text: str, keeps: dict[str, str]) -> str:
    out = text
    for token, original in keeps.items():
        out = out.replace(token, original)
    return out


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", text or ""))


_VARIABLE_TOKEN = re.compile(r"\{\{\s*(?!RANDOM\b)[^{}]*\}\}", re.I)


def _norm_token(token: str) -> str:
    return re.sub(r"\s+", " ", token).strip()


def _paragraph_count(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", text or "") if p.strip()])


def validate_rewrite(original: str, revised: str, keeps: dict[str, str], *, max_words: int) -> list[str]:
    """Return a list of problems; empty means the rewrite is safe to use."""
    problems: list[str] = []
    if not (revised or "").strip():
        return ["the body is empty"]
    for issue in spintax.analyze(revised):
        if issue.level == "error":
            problems.append(f"broken spintax: {issue.message}")
    if spintax.count_combinations(revised) < 2:
        problems.append("no spintax variation was added")
    duplicates = spintax.duplicate_option_blocks(revised)
    if duplicates:
        problems.append(
            f"{duplicates} spin block(s) repeat the same option - every option must be different"
        )
    original_vars = set(spintax.variables_in_text(original))
    revised_vars = set(spintax.variables_in_text(revised))
    if original_vars != revised_vars:
        missing = sorted(original_vars - revised_vars)
        added = sorted(revised_vars - original_vars)
        problems.append(f"variables changed (missing={missing}, added={added})")
    revised_tokens = {_norm_token(t) for t in _VARIABLE_TOKEN.findall(revised)}
    for token in _VARIABLE_TOKEN.findall(original):
        if _norm_token(token) not in revised_tokens:
            problems.append(f"variable {token.strip()} was changed")
    for token, line in keeps.items():
        if revised.count(token) != 1:
            problems.append(f"unsubscribe line {token} was altered")
    if "\u2014" in revised or "\u2013" in revised:
        problems.append("em/en dash added")
    if _paragraph_count(original) != _paragraph_count(revised):
        problems.append(
            f"paragraph structure changed ({_paragraph_count(original)} -> "
            f"{_paragraph_count(revised)})"
        )
    original_words = _word_count(_restore(original, keeps))
    for label, longest in (("shortest", False), ("longest", True)):
        rendered = spintax.render_extreme(_restore(revised, keeps), longest=longest)
        words = _word_count(rendered)
        if words > max_words:
            problems.append(f"{label} rendering is {words} words (max {max_words})")
        if words > max(12, original_words * 1.6):
            problems.append(
                f"{label} rendering is much longer than the original "
                f"({words} vs {original_words} words)"
            )
    if _has_mechanical_breakage(_restore(revised, keeps)):
        problems.append("rendered text has leftover unrendered {{ }} braces or stray pipes")
    return problems


def _has_mechanical_breakage(text: str) -> bool:
    rendered = spintax.render_extreme(text, longest=True)
    return "{{" in rendered or "}}" in rendered


def _call_model(
    specs: list[VariantSpec],
    *,
    campaign_name: str,
    api_key: str,
    model: str,
    timeout: float,
    transport: Optional[httpx.BaseTransport],
    extra_instructions: str,
    max_words: int,
) -> tuple[Optional[dict[str, Any]], str]:
    payload = {
        "campaign": campaign_name,
        "max_words_per_render": max_words,
        "variants": [
            {
                "step": spec.step,
                "label": spec.label,
                "subject": spec.subject,
                "body": spec.body,
            }
            for spec in specs
        ],
    }
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    if extra_instructions:
        user += "\n\nPrevious attempt problems to fix:\n" + extra_instructions
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.55,
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport) as http:
            resp = http.post(DEEPSEEK_URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
            data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip(), flags=re.I)
        parsed = json.loads(content)
        return (parsed if isinstance(parsed, dict) else None), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _proofread_staged(
    specs: list[VariantSpec],
    staged: list[tuple[VariantSpec, str, str]],
    *,
    campaign_name: str,
    api_key: str,
    model: str,
    timeout: float,
    transport: Optional[httpx.BaseTransport],
) -> list[str]:
    """Render a few spins of each staged body and ask the proofreader for problems."""
    placeholders = {
        "firstName": "Alex",
        "lastName": "Doe",
        "companyName": "Acme Corp",
        "companyShort": "Acme",
        "website": "acme.com",
        "jobTitle": "COO",
        "category": "manufacturing",
        "list_name": "Sample List",
    }
    samples: list[dict[str, Any]] = []
    original_texts: list[str] = []
    for spec, body, _subject in staged:
        values = {
            name: placeholders.get(name, "example")
            for name in spintax.variables_in_text(body)
        }
        for sample in generate_samples(body, [values], seed=4242, spins=3, leads=1):
            samples.append(
                {
                    "segment": campaign_name or spec.label,
                    "step": spec.step,
                    "variant": spec.label,
                    "sample": sample.label,
                    "text": sample.text[:1200],
                }
            )
        for sample in generate_samples(spec.body, [values], seed=4242, spins=3, leads=1):
            original_texts.append(_normalize_for_match(sample.text))
    if not samples:
        return []
    result = proofread_samples(
        samples, api_key=api_key, model=model, timeout=timeout, transport=transport
    )
    if result.status != "ok":
        return []
    issues: list[str] = []
    for issue in result.issues[:6]:
        quote = _normalize_for_match(issue.quote)
        if quote and any(quote in original for original in original_texts):
            continue  # pre-existing wording from the original copy, not caused by the spintax
        detail = f'"{issue.quote}" - {issue.problem}'
        if issue.suggestion:
            detail += f" (try: {issue.suggestion})"
        issues.append(detail)
    return issues


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def spin_copy_file(
    copy_file: CopyFile,
    *,
    campaign_name: str = "",
    max_words: int = 120,
    nudge: str = "",
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 180.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> SpinOutcome:
    """Rewrite every variant of ``copy_file`` with spintax, in place when it passes."""
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return SpinOutcome(status="skipped", error="DEEPSEEK_API_KEY is not configured.")

    specs: list[VariantSpec] = []
    notes: list[str] = []
    for step in copy_file.steps:
        for vi, variant in enumerate(step.variants):
            masked, keeps = _mask_unsubscribe(variant.body)
            spec = VariantSpec(
                step=step.number,
                variant_index=vi,
                label=variant.label,
                subject=variant.subject,
                body=masked,
                keep_lines=keeps,
            )
            specs.append(spec)
    if not specs:
        return SpinOutcome(status="error", error="copy file has no variants")

    user_nudge = (nudge or "").strip()
    extra = user_nudge
    problems: list[str] = []
    style_issues: list[str] = []
    best_staged: Optional[list[tuple[VariantSpec, str, str]]] = None
    best_issues: list[str] = []
    accepted: Optional[list[tuple[VariantSpec, str, str]]] = None
    attempts_used = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts_used = attempt
        parsed, error = _call_model(
            specs,
            campaign_name=campaign_name,
            api_key=key,
            model=model,
            timeout=timeout,
            transport=transport,
            extra_instructions=extra,
            max_words=max_words,
        )
        if parsed is None:
            return SpinOutcome(status="error", error=error, model=model, attempts=attempt)
        returned = parsed.get("variants")
        if not isinstance(returned, list) or len(returned) != len(specs):
            extra = (
                f"Returned {len(returned) if isinstance(returned, list) else 0} variants; "
                f"expected exactly {len(specs)}."
            )
            if user_nudge:
                extra = user_nudge + "\n" + extra
            problems = [extra]
            continue
        problems = []
        staged: list[tuple[VariantSpec, str, str]] = []
        for spec, item in zip(specs, returned):
            if not isinstance(item, dict):
                problems.append("a variant was not an object")
                continue
            revised = str(item.get("body") or "")
            revised, dropped = spintax.dedupe_spin_options(revised)
            if dropped:
                notes.append(
                    f"{spec.label}: removed {dropped} repeated spin option(s)"
                )
            restored = _restore(revised, spec.keep_lines)
            problems += [
                f"{spec.label}: {p}"
                for p in validate_rewrite(
                    spec.body, revised, spec.keep_lines, max_words=max_words
                )
            ]
            subject = str(item.get("subject") or spec.subject).strip() or spec.subject
            if subject != spec.subject:
                subject_problems = validate_rewrite(
                    spec.subject, subject, {}, max_words=max_words
                )
                subject_problems = [p for p in subject_problems if "spintax variation" not in p]
                if subject_problems:
                    subject = spec.subject
                else:
                    notes.append(f"{spec.label}: subject spintax added")
            staged.append((spec, restored, subject))
        if problems:
            extra = (
                "Fix these problems and return the complete JSON again. Include real "
                "{{RANDOM | ...}} blocks with distinct options and never return the "
                "unchanged text:\n" + "\n".join(f"- {p}" for p in problems[:12])
            )
            if user_nudge:
                extra = user_nudge + "\n" + extra
            continue
        style_issues = _proofread_staged(
            specs,
            staged,
            campaign_name=campaign_name,
            api_key=key,
            model=model,
            timeout=timeout,
            transport=transport,
        )
        if best_staged is None or len(style_issues) < len(best_issues):
            best_staged, best_issues = staged, style_issues
        if not style_issues:
            accepted = staged
            break
        if attempt < MAX_ATTEMPTS:
            # Let the model fix the proofreader's complaints once; keep the best version.
            extra = user_nudge + "\nProofreader found these problems - fix them:\n" + "\n".join(
                f"- {issue}" for issue in style_issues[:8]
            ) + "\nDo not repeat the same option twice in a block."
            continue
    if accepted is None:
        if best_staged is None:
            return SpinOutcome(
                status="error",
                error="the AI could not produce valid spintax: " + "; ".join(problems[:6]),
                model=model,
                attempts=attempts_used or MAX_ATTEMPTS,
            )
        accepted = best_staged
        style_issues = best_issues
    elif accepted is not best_staged:
        style_issues = []
    for spec, body, subject in accepted:
        for step in copy_file.steps:
            if step.number != spec.step:
                continue
            if spec.variant_index >= len(step.variants):
                continue
            variant = step.variants[spec.variant_index]
            variant.body = body
            variant.subject = subject
    notes.append(
        "spintax added to "
        + ", ".join(f"step {s.step} {s.label}" for s in specs)
    )
    if style_issues:
        notes.append(f"proofreader notes ({len(style_issues)}) - review or regenerate")
    else:
        notes.append("proofread clean")
    return SpinOutcome(
        status="ok",
        model=model,
        attempts=attempts_used,
        notes=notes,
        warnings=style_issues,
    )
