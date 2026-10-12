"""Stage 3: copy QA - syntax, variables, coverage, render checks, lint."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import variables as variables_mod
from .config import LaunchConfig, SegmentConfig
from .copy_defaults import has_unsubscribe
from .copy_parser import CopyFile, CopyParseError, parse_copy, parse_copy_file
from .leads import LeadBuild, build_leads
from .lint import LintIssue, lint_text
from .render import Sample, generate_samples, mechanical_issues
from .spintax import analyze, count_combinations
from .variables import DEFAULT_BUILTINS, Coverage, unknown_variables

BLOCK = "BLOCK"
WARN = "WARN"
PASS = "PASS"

_SEVERITY = {PASS: 0, WARN: 1, BLOCK: 2}


def worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _SEVERITY.get(s, 0)) if statuses else PASS


@dataclass
class QAIssue:
    level: str  # warning | error (error = blocking)
    kind: str
    message: str


@dataclass
class VariantQA:
    step: int
    delay_days: float
    label: str
    subject: str
    body: str
    combos: int = 1
    samples: list[Sample] = field(default_factory=list)
    issues: list[QAIssue] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    lint: list[LintIssue] = field(default_factory=list)
    unsubscribe_present: bool = True
    status: str = PASS


@dataclass
class SegmentQA:
    slug: str
    name: str
    status: str = PASS
    leads_count: int = 0
    variants: list[VariantQA] = field(default_factory=list)
    notices: list[QAIssue] = field(default_factory=list)
    copy_issues: list[str] = field(default_factory=list)
    leads_report: Any = None


@dataclass
class LaunchQA:
    cfg: LaunchConfig
    segments: list[SegmentQA] = field(default_factory=list)
    unassigned: int = 0
    seed: int = 1337
    generated_at: str = ""
    status: str = PASS
    proofread: Any = None


def _resolve(cfg: LaunchConfig, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    direct = cfg.path / p
    if direct.is_file():
        return direct
    return cfg.path / "copy" / p.name


def _syntax_issues(text: str) -> list[QAIssue]:
    out: list[QAIssue] = []
    for issue in analyze(text):
        level = "error" if issue.level == "error" else "warning"
        out.append(QAIssue(level, "spintax", issue.message))
    return out


def analyze_variant(
    seg: SegmentConfig, step_no: int, delay_days: float, label: str,
    subject: str, body: str, values: list[dict], allowed: set[str], cfg: LaunchConfig,
    *, seed: int,
) -> VariantQA:
    v = VariantQA(step=step_no, delay_days=delay_days, label=label, subject=subject, body=body)
    combined = f"{subject}\n{body}"

    v.issues += _syntax_issues(combined)
    if not body.strip():
        v.issues.append(QAIssue("error", "empty-body", "body is empty"))

    for name in unknown_variables(combined, allowed):
        v.issues.append(
            QAIssue("error", "unknown-variable", f"unknown variable {{{{{name}}}}} (not a column/builtin)")
        )

    v.coverage = variables_mod.coverage(combined, values, threshold_pct=2.0)
    for c in v.coverage:
        if c.status == "block":
            v.issues.append(
                QAIssue(
                    "error",
                    "coverage",
                    "{{" + c.variable + "}}: empty for "
                    f"{c.empty}/{c.total} leads ({c.pct}%)",
                )
            )
        elif c.status == "warn":
            v.issues.append(
                QAIssue(
                    "warning",
                    "coverage",
                    "{{" + c.variable + "}}: empty for "
                    f"{c.empty}/{c.total} leads ({c.pct}%)",
                )
            )

    v.combos = max(1, count_combinations(body)) * max(1, count_combinations(subject))
    v.samples = generate_samples(body, values, seed=seed, spins=10, leads=5)
    for sample in v.samples:
        for mech in mechanical_issues(sample.text):
            v.issues.append(
                QAIssue("warning", f"render:{mech.kind}", f"[{sample.label}] {mech.message}")
            )

    # Word count and link count are checked on the longest rendered version: spintax
    # options would otherwise be counted as if every alternative were sent at once.
    raw_lint = lint_text(body, cfg.lint)
    longest_text = max((s.text for s in v.samples), key=len) if v.samples else body
    rendered_lint = lint_text(longest_text, cfg.lint)
    v.lint = [i for i in raw_lint if i.kind in {"em-dash", "exclamation"}]
    v.lint += [i for i in rendered_lint if i.kind in {"word-count", "links"}]
    for li in v.lint:
        v.issues.append(QAIssue("warning", f"lint:{li.kind}", li.message))

    v.unsubscribe_present = has_unsubscribe(body)
    if not v.unsubscribe_present:
        if cfg.unsubscribe.mode == "require":
            v.issues.append(
                QAIssue("error", "unsubscribe", "no unsubscribe language and mode=require")
            )
        # mode=append: the default block is added at create time - shown in the report's
        # "Unsub" column and in the web UI's "handled automatically" summary, not a warning.

    v.status = BLOCK if any(i.level == "error" for i in v.issues) else (
        WARN if v.issues else PASS
    )
    return v


def analyze_segment(
    cfg: LaunchConfig, seg: SegmentConfig, leads, *, seed: int, index: int
) -> SegmentQA:
    q = SegmentQA(slug=seg.slug, name=seg.name, leads_count=len(leads.leads))
    q.leads_report = leads.report
    try:
        cf: CopyFile = parse_copy_file(_resolve(cfg, seg.copy))
        q.copy_issues = list(cf.issues)
    except CopyParseError as exc:
        q.copy_issues = [str(exc)]
        q.status = BLOCK
        return q

    # A copy file that parses to zero steps/variants must never pass QA - otherwise a
    # campaign with no email copy would be planned as "ready".
    blocking_notes: list[str] = []
    if not cf.steps:
        blocking_notes.append("copy file has no steps")
    else:
        empty_steps = [str(step.number) for step in cf.steps if not step.variants]
        if empty_steps:
            blocking_notes.append(f"step(s) {', '.join(empty_steps)}: no variants")
    if not any(step.variants for step in cf.steps):
        blocking_notes.append("copy file has no email variants")
    for note in blocking_notes:
        if note not in q.copy_issues:
            q.copy_issues.append(note)

    if not leads.leads:
        q.copy_issues.append("no leads left after normalization/dedupe")
        q.status = BLOCK

    allowed = set(DEFAULT_BUILTINS)
    for values in leads.values:
        allowed |= set(values.keys())
    allowed |= set(cfg.raw.get("builtins") or [])

    for si, step in enumerate(cf.steps):
        for vi, variant in enumerate(step.variants):
            vqa = analyze_variant(
                seg,
                step.number,
                step.delay_days,
                variant.label,
                variant.subject,
                variant.body,
                leads.values,
                allowed,
                cfg,
                seed=seed + index * 1000 + si * 10 + vi,
            )
            q.variants.append(vqa)

    blocking_copy = [
        i
        for i in q.copy_issues
        if "empty body" in i or "no steps" in i or "no variants" in i or "no email variants" in i
    ]
    statuses = [v.status for v in q.variants]
    q.status = worst(statuses + ([BLOCK] if blocking_copy or blocking_notes else []))
    return q


def analyze_launch(cfg: LaunchConfig, *, seed: int = 1337) -> LaunchQA:
    out = LaunchQA(
        cfg=cfg,
        seed=seed,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    build: LeadBuild = build_leads(
        segments=cfg.segments,
        lead_mode=cfg.lead_mode,
        split_column=cfg.split_column,
        dedupe_across_segments=cfg.dedupe_across_segments,
        base_dir=cfg.path,
        variable_map=cfg.variable_map,
    )
    out.unassigned = len(build.unassigned)
    for i, seg in enumerate(cfg.segments):
        out.segments.append(analyze_segment(cfg, seg, build.segments[seg.slug], seed=seed, index=i))
    out.status = worst([s.status for s in out.segments])
    return out
