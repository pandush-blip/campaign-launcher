"""qa_report.html writer (spec Stage 3 output)."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import BaseLoader, Environment, select_autoescape
from markupsafe import Markup, escape

from .copy_defaults import DEFAULT_UNSUBSCRIBE
from .qa import BLOCK, PASS, WARN, LaunchQA

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>QA report - {{ qa.cfg.client }}</title>
<style>
 body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1c2530;background:#f7f8fa}
 h1{margin:0 0 4px} h2{margin:28px 0 8px} .meta{color:#5b6b7c;font-size:13px}
 .badge{display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;font-size:12px}
 .PASS{background:#d9f2e2;color:#14653a}.WARN{background:#fdf0cf;color:#8a5b00}
 .BLOCK{background:#fbd8d8;color:#8a1616}
 table{border-collapse:collapse;width:100%;background:#fff;margin:8px 0 16px}
 th,td{border:1px solid #e3e7ee;padding:6px 8px;font-size:13px;vertical-align:top}
 th{background:#eef1f6;text-align:left}
 details{margin:4px 0 10px} summary{cursor:pointer;font-weight:600}
 pre{white-space:pre-wrap;background:#fbfcfd;border:1px solid #eceff4;padding:8px;font-size:12.5px}
 mark{background:#ffe08a} .issue{font-size:12.5px;margin:2px 0}
 .issue.error{color:#8a1616} .issue.warning{color:#8a5b00}
 .small{font-size:12px;color:#5b6b7c}
</style></head><body>
<h1>QA report - {{ qa.cfg.client }}</h1>
<div class="meta">{{ qa.cfg.path }} | seed {{ qa.seed }} | {{ qa.generated_at }} |
 overall <span class="badge {{ qa.status }}">{{ qa.status }}</span>
 {% if qa.unassigned %}| {{ qa.unassigned }} unassigned rows{% endif %}</div>
{% for seg in qa.segments %}
<h2>{{ seg.name }} <span class="badge {{ seg.status }}">{{ seg.status }}</span>
 <span class="small">{{ seg.leads_count }} leads
 {% if seg.leads_report %}(in {{ seg.leads_report.rows_in }}, invalid {{ seg.leads_report.invalid }},
 dupes {{ seg.leads_report.duplicates }}, moved {{ seg.leads_report.moved_out }}){% endif %}</span></h2>
{% for issue in seg.copy_issues %}<div class="issue error">copy: {{ issue }}</div>{% endfor %}
<table><tr><th>Step</th><th>Variant</th><th>Subject</th><th>Combinations</th><th>Unsub</th>
<th>Status</th><th>Issues</th></tr>
{% for v in seg.variants %}
<tr><td>{{ v.step }} (delay {{ v.delay_days }}d)</td><td>{{ v.label }}</td>
<td>{{ v.subject or "(same thread)" }}</td><td>{{ "{:,}".format(v.combos) }}</td>
<td>{{ "yes" if v.unsubscribe_present else "will be appended" }}</td>
<td><span class="badge {{ v.status }}">{{ v.status }}</span></td>
<td>{% for i in v.issues %}<div class="issue {{ i.level }}">{{ i.kind }}: {{ i.message }}</div>{% endfor %}</td></tr>
{% endfor %}</table>
{% for v in seg.variants %}
<details><summary>Step {{ v.step }} / {{ v.label }} - {{ v.samples|length }} rendered samples</summary>
{% if v.coverage %}<table><tr><th>Variable</th><th>Empty</th><th>%</th><th>Status</th></tr>
{% for c in v.coverage %}<tr><td>{{ c.variable }}</td><td>{{ c.empty }}/{{ c.total }}</td><td>{{ c.pct }}%</td>
<td>{{ c.status }}</td></tr>{% endfor %}</table>{% endif %}
{% for s in v.samples %}<div class="small">{{ s.label }}{% if s.lead_email %} - {{ s.lead_email }}{% endif %}</div>
<pre>{{ s.text|highlight }}</pre>{% endfor %}
</details>
{% endfor %}
{% endfor %}
{% if qa.proofread %}
<h2>AI proofread <span class="small">{{ qa.proofread.status }}{% if qa.proofread.error %} - {{ qa.proofread.error }}{% endif %}</span></h2>
{% if qa.proofread.issues %}
<table><tr><th>Step/Variant</th><th>Sample</th><th>Quote</th><th>Problem</th><th>Suggestion</th></tr>
{% for i in qa.proofread.issues %}<tr><td>{{ i.step }} {{ i.variant }}</td><td>{{ i.sample }}</td>
<td>{{ i.quote }}</td><td>{{ i.problem }}</td><td>{{ i.suggestion }}</td></tr>{% endfor %}</table>
{% else %}<p class="small">No issues reported.</p>{% endif %}
{% endif %}
<p class="small">Default unsubscribe block (appended when missing):<br>{{ default_unsub }}</p>
</body></html>"""

_HL = [
    (re.compile(r" {2,}"), None),
    (re.compile(r"\s+[,.;:!?]"), None),
    (re.compile(r"[,.!?](?=[A-Za-z])"), None),
    (re.compile(r"\b(\w{2,})(\s+)\1\b", re.I), None),
    (re.compile(r"\{\{|\}\}"), None),
]


def highlight(text: str) -> Markup:
    safe = str(escape(text))
    for pattern, _ in _HL:
        safe = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", safe)
    return Markup(safe)


def write_qa_report(qa: LaunchQA, out_path: str | Path | None = None) -> Path:
    env = Environment(loader=BaseLoader(), autoescape=select_autoescape(["html"]))
    env.filters["highlight"] = highlight
    html = env.from_string(TEMPLATE).render(
        qa=qa, default_unsub=DEFAULT_UNSUBSCRIBE, BLOCK=BLOCK, WARN=WARN, PASS=PASS
    )
    path = Path(out_path) if out_path else qa.cfg.path / "qa_report.html"
    path.write_text(html, encoding="utf-8")
    return path


def write_launch_report(run, out_path: str | Path | None = None) -> Path:
    """launch_report.md (spec Stage 6)."""
    from datetime import datetime, timezone

    cfg = run.cfg
    lines = [
        f"# Launch report - {cfg.client}",
        "",
        f"- Folder: `{cfg.path}`",
        f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Settings source: `{run.source.get('name', cfg.settings_campaign)}`"
        + (f" ({run.source.get('id')})" if run.source.get("id") else ""),
        f"- Overall: **{run.status}**",
        "",
        "| Segment | Campaign | ID | Status | Uploaded | Dupes | Skipped | Rejected | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for plan in run.plans:
        seg_run = next((s for s in run.segments if s.slug == plan.slug), None)
        if plan.blocked:
            lines.append(
                f"| {plan.name} | {plan.campaign_name} | - | SKIPPED | - | - | - | - | "
                f"{plan.blocked} |"
            )
        elif seg_run:
            notes = "; ".join(seg_run.issues)[:400]
            lines.append(
                f"| {plan.name} | {seg_run.campaign_name} | {seg_run.campaign_id} | "
                f"{seg_run.status} | {seg_run.leads_uploaded} | {seg_run.leads_duplicated} | "
                f"{seg_run.leads_skipped} | {seg_run.leads_rejected} | {notes} |"
            )
        else:
            lines.append(
                f"| {plan.name} | {plan.campaign_name} | - | NOT-CREATED | - | - | - | - | - |"
            )
    drafts = [s for s in run.segments if s.status == "PASS"]
    from .create import launch_totals

    totals = launch_totals(run)
    lines += [
        "",
        f"**{totals['added']:,} leads** are in the created campaigns "
        f"({totals['uploaded']:,} uploaded, {totals['skipped']:,} skipped because they were "
        f"already in the workspace, {totals['rejected']:,} rejected) across "
        f"{totals['campaigns']} campaign(s).",
        "",
        f"All {len(drafts)} campaigns are in DRAFT. Nothing was launched.",
    ]
    path = Path(out_path) if out_path else cfg.path / "launch_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
