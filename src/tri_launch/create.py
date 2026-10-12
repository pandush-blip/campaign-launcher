"""Stage 4-5: plan, create draft campaigns, and read everything back."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Optional

from . import state
from .body_format import copy_to_sequences
from .config import LaunchConfig, SegmentConfig, render_campaign_name
from .copy_parser import CopyParseError, parse_copy_file
from .instantly import InstantlyClient, NotFound
from .leads import VARIABLE_NAMES, LeadBuild, SegmentLeads, build_leads
from .qa import BLOCK, LaunchQA, analyze_launch
from .variables import SENDER_BUILTINS, extract_variables

STANDARD_VAR_NAMES = set(VARIABLE_NAMES.values())

# Phase 0 inheritance contract: these must match the settings campaign byte-for-byte.
INHERIT_FIELDS = (
    "campaign_schedule",
    "email_list",
    "email_tag_list",
    "daily_limit",
    "daily_max_leads",
    "email_gap",
    "random_wait_max",
    "text_only",
    "first_email_text_only",
    "stop_on_reply",
    "stop_on_auto_reply",
    "stop_for_company",
    "link_tracking",
    "open_tracking",
    "insert_unsubscribe_header",
    "match_lead_esp",
    "prioritize_new_leads",
    "allow_risky_contacts",
    "disable_bounce_protect",
    "is_evergreen",
    "pl_value",
)
MAX_LEADS_PER_BATCH = 1000


@dataclass
class SegmentPlan:
    slug: str
    name: str
    campaign_name: str
    leads_count: int
    qa_status: str
    blocked: Optional[str] = None
    blocked_reasons: list[str] = field(default_factory=list)


class Interrupted(RuntimeError):
    """Raised by --interrupt-after-batches to simulate a crash mid-upload."""


@dataclass
class SegmentRun:
    slug: str
    campaign_name: str
    campaign_id: Optional[str] = None
    status: str = "PENDING"
    resumed: bool = False
    leads_uploaded: int = 0
    leads_duplicated: int = 0
    leads_skipped: int = 0
    leads_rejected: int = 0
    leads_in_campaign: int = 0
    issues: list[str] = field(default_factory=list)


@dataclass
class LaunchRun:
    cfg: LaunchConfig
    plans: list[SegmentPlan] = field(default_factory=list)
    segments: list[SegmentRun] = field(default_factory=list)
    status: str = "PENDING"
    dry_run: bool = False
    source: dict[str, Any] = field(default_factory=dict)


def launch_totals(run: LaunchRun) -> dict[str, int]:
    """Totals for the end-of-run summary ("leads added" = readback counts)."""
    return {
        "campaigns": len(run.segments),
        "added": sum(s.leads_in_campaign for s in run.segments),
        "uploaded": sum(s.leads_uploaded for s in run.segments),
        "duplicated": sum(s.leads_duplicated for s in run.segments),
        "skipped": sum(s.leads_skipped for s in run.segments),
        "rejected": sum(s.leads_rejected for s in run.segments),
    }


def _copy_path(cfg: LaunchConfig, seg: SegmentConfig):
    p = cfg.path / seg.copy
    if p.is_file():
        return p
    return cfg.path / "copy" / (seg.copy or f"{seg.slug}.md")


def build_plan(
    cfg: LaunchConfig, qa: Optional[LaunchQA] = None, build: Optional[LeadBuild] = None
) -> LaunchRun:
    qa = qa or analyze_launch(cfg)
    if build is None:
        build = build_leads(
            segments=cfg.segments,
            lead_mode=cfg.lead_mode,
            split_column=cfg.split_column,
            dedupe_across_segments=cfg.dedupe_across_segments,
            base_dir=cfg.path,
            variable_map=cfg.variable_map,
        )
    run = LaunchRun(cfg=cfg)
    by_slug = {s.slug: s for s in qa.segments}
    for seg in cfg.segments:
        sqa = by_slug.get(seg.slug)
        leads_count = len(build.segments[seg.slug].leads)
        blocked = None
        blocked_reasons: list[str] = []
        if sqa and sqa.status == BLOCK:
            blocked = "QA BLOCK"
            seen: set[str] = set()
            for v in sqa.variants:
                for issue in v.issues:
                    if issue.level != "error" or issue.message in seen:
                        continue
                    seen.add(issue.message)
                    blocked_reasons.append(f"step {v.step} {v.label}: {issue.message}")
            for message in sqa.copy_issues:
                if message not in seen:
                    seen.add(message)
                    blocked_reasons.append(message)
            blocked_reasons = blocked_reasons[:6]
        elif leads_count == 0:
            blocked = "no leads"
        run.plans.append(
            SegmentPlan(
                slug=seg.slug,
                name=seg.name,
                campaign_name=render_campaign_name(cfg, seg),
                leads_count=leads_count,
                qa_status=sqa.status if sqa else "UNKNOWN",
                blocked=blocked,
                blocked_reasons=blocked_reasons,
            )
        )
    return run


def _sequences_match(sent: dict, got: dict) -> list[str]:
    diffs: list[str] = []
    sent_steps = (sent.get("sequences") or [{}])[0].get("steps", [])
    got_steps = (got.get("sequences") or [{}])[0].get("steps", [])
    if len(sent_steps) != len(got_steps):
        return [f"step count: sent {len(sent_steps)}, got {len(got_steps)}"]
    for i, (a, b) in enumerate(zip(sent_steps, got_steps), start=1):
        if a.get("delay") != b.get("delay"):
            diffs.append(f"step{i} delay: sent {a.get('delay')}, got {b.get('delay')}")
        av, bv = a.get("variants") or [], b.get("variants") or []
        if len(av) != len(bv):
            diffs.append(f"step{i} variant count: sent {len(av)}, got {len(bv)}")
            continue
        for j, (va, vb) in enumerate(zip(av, bv), start=1):
            if (va.get("subject") or "") != (vb.get("subject") or ""):
                diffs.append(f"step{i} variant{j} subject differs")
            if (va.get("body") or "") != (vb.get("body") or ""):
                diffs.append(f"step{i} variant{j} body differs")
    return diffs


def _inherit_diffs(source: dict, got: dict) -> list[str]:
    diffs = []
    for field in INHERIT_FIELDS:
        if source.get(field) != got.get(field):
            diffs.append(f"{field}: source={source.get(field)!r} created={got.get(field)!r}")
    return diffs


def _custom_variables(cfg: LaunchConfig, seg: SegmentConfig) -> list[str]:
    copy = parse_copy_file(_copy_path(cfg, seg))
    used: list[str] = []
    for step in copy.steps:
        for v in step.variants:
            for name in extract_variables(f"{v.subject}\n{v.body}"):
                if name not in used:
                    used.append(name)
    values = [n for n in used if n not in STANDARD_VAR_NAMES and n not in SENDER_BUILTINS]
    if "list_name" not in values:
        values.append("list_name")
    return values


def create_segment(
    cfg: LaunchConfig,
    seg: SegmentConfig,
    plan: SegmentPlan,
    leads: SegmentLeads,
    client: InstantlyClient,
    source: dict,
    *,
    run: LaunchRun,
    interrupt_after_batches: Optional[int] = None,
) -> SegmentRun:
    result = SegmentRun(slug=seg.slug, campaign_name=plan.campaign_name)

    # Idempotency: tracked campaign with this exact name for this launch folder?
    known = None
    for cid, rec in state.load()["created"].items():
        if rec.get("name") == plan.campaign_name and same_launch(rec.get("launch_path"), cfg):
            known = (cid, rec)
            break
    if known:
        cid, rec = known
        camp = client.get_campaign(cid)
        if camp.get("status") != 0:
            client.pause_campaign_safety(cid, reason="safety:resume-not-draft")
            result.status = "FAILED"
            result.issues.append(f"existing campaign is not draft (status={camp.get('status')}); paused")
            return result
        result.campaign_id = cid
        result.resumed = True
    else:
        existing = client.find_campaign_by_name(plan.campaign_name)
        if existing:
            result.status = "FAILED"
            result.issues.append(
                f"campaign named {plan.campaign_name!r} already exists in Instantly but is not "
                "tracked by this tool - refusing to touch it"
            )
            return result
        dup = client.duplicate_campaign(source["id"], plan.campaign_name)
        result.campaign_id = dup["id"]
        state.register_campaign(
            dup["id"],
            dup.get("name", plan.campaign_name),
            source_id=source["id"],
            purpose="launch",
            extra={
                "launch_path": str(cfg.path),
                "client": cfg.client,
                "client_env": cfg.raw.get("api_key_env") or "",
            },
        )

    cid = result.campaign_id
    assert cid

    # Sequences
    try:
        copy = parse_copy_file(_copy_path(cfg, seg))
        if not any(v.body.strip() for step in copy.steps for v in step.variants):
            raise CopyParseError("copy file has no email variants")
        payload, notices = copy_to_sequences(
            copy,
            unsubscribe_mode=cfg.unsubscribe.mode,
            unsubscribe_text=cfg.unsubscribe.text,
        )
    except CopyParseError as exc:
        result.status = "FAILED"
        result.issues.append(f"copy parse error: {exc}")
        return result
    client.patch_campaign(cid, payload)

    # Variables (Phase 0: duplicates inherit none; leads/add would auto-create them)
    client.add_campaign_variables(cid, _custom_variables(cfg, seg))

    # Leads, batched
    skip_workspace = bool((cfg.raw.get("upload") or {}).get("skip_if_in_workspace", True))
    items = leads.leads
    batches_done = 0
    for start in range(0, len(items), MAX_LEADS_PER_BATCH):
        batch = items[start : start + MAX_LEADS_PER_BATCH]
        resp = client.add_leads(cid, batch, skip_if_in_workspace=skip_workspace)
        batches_done += 1
        result.leads_uploaded += int(resp.get("leads_uploaded") or 0)
        result.leads_duplicated += int(resp.get("duplicated_leads") or 0)
        result.leads_skipped += int(resp.get("skipped_count") or 0)
        result.leads_rejected += int(resp.get("invalid_email_count") or 0) + int(
            resp.get("in_blocklist") or 0
        )
        if notices:
            result.issues += [f"unsubscribe appended: step {n['step']} {n['variant']}" for n in notices]
            notices = []
        if interrupt_after_batches is not None and batches_done >= interrupt_after_batches:
            raise Interrupted(
                f"simulated interruption after {batches_done} batch(es) for {plan.campaign_name}"
            )

    # Readback is truth
    got = client.get_campaign(cid)
    diffs: list[str] = []
    if got.get("name") != plan.campaign_name:
        diffs.append(f"name: {got.get('name')!r} != {plan.campaign_name!r}")
    if got.get("status") != 0:
        client.pause_campaign_safety(cid, reason="safety:readback-not-draft")
        result.status = "FAILED"
        result.issues.append(f"status after create is {got.get('status')}, not draft; paused")
        return result
    diffs += _sequences_match(payload, got)
    diffs += _inherit_diffs(source, got)
    tracked = state.known_campaign(cid) or {}
    expected = len(tracked.get("lead_ids") or [])
    if expected == 0:
        expected = result.leads_uploaded + result.leads_duplicated
    actual = client.count_leads(campaign_id=cid)
    result.leads_in_campaign = actual
    if actual != expected:
        diffs.append(f"lead count: uploaded+dupes={expected}, in Instantly={actual}")

    state.register_campaign(
        cid,
        plan.campaign_name,
        source_id=source["id"],
        purpose="launch",
        extra={
            "launch_path": str(cfg.path),
            "client": cfg.client,
            "client_env": cfg.raw.get("api_key_env") or "",
            "segment": seg.slug,
            "expected_lead_count": actual,
            "sequences": payload,
        },
    )
    if diffs:
        result.status = "FAILED"
        result.issues += diffs
    else:
        result.status = "PASS"
        note = f"{actual} leads read back"
        if result.leads_skipped:
            note += f" ({result.leads_skipped} skipped by the workspace check)"
        result.issues.append(note)
    return result


def create_launch(
    cfg: LaunchConfig,
    client: InstantlyClient,
    *,
    only: Optional[list[str]] = None,
    dry_run: bool = False,
    seed: int = 1337,
    interrupt_after_batches: Optional[int] = None,
) -> LaunchRun:
    qa = analyze_launch(cfg, seed=seed)
    build = build_leads(
        segments=cfg.segments,
        lead_mode=cfg.lead_mode,
        split_column=cfg.split_column,
        dedupe_across_segments=cfg.dedupe_across_segments,
        base_dir=cfg.path,
        variable_map=cfg.variable_map,
    )
    run = build_plan(cfg, qa, build)
    run.dry_run = dry_run
    selected = [
        p for p in run.plans if (not only or p.slug in only) and not p.blocked
    ]
    if dry_run:
        run.status = "PLANNED"
        return run
    source = client.resolve_campaign(cfg.settings_campaign)
    run.source = {"id": source["id"], "name": source["name"], "status": source["status"]}
    for plan in selected:
        seg = cfg.segment(plan.slug)
        result = create_segment(
            cfg,
            seg,
            plan,
            build.segments[plan.slug],
            client,
            source,
            run=run,
            interrupt_after_batches=interrupt_after_batches,
        )
        run.segments.append(result)
    run.status = "PASS" if run.segments and all(s.status == "PASS" for s in run.segments) else "FAILED"
    if not run.segments:
        run.status = "NOTHING-TO-DO"
    return run


def verify_launch(cfg: LaunchConfig, client: InstantlyClient) -> list[dict]:
    """Re-read every tracked campaign for this launch folder."""
    out = []
    for cid, rec in state.load()["created"].items():
        if not same_launch(rec.get("launch_path"), cfg):
            continue
        item = {"id": cid, "name": rec.get("name"), "status": "UNKNOWN", "issues": []}
        try:
            camp = client.get_campaign(cid)
        except NotFound:
            item["status"] = "MISSING"
            item["issues"].append("campaign not found in Instantly")
            out.append(item)
            continue
        if camp.get("status") != 0:
            client.pause_campaign_safety(cid, reason="safety:verify-not-draft")
            item["issues"].append(f"not draft (status={camp.get('status')}); paused")
        if camp.get("name") != rec.get("name"):
            item["issues"].append("name changed")
        expected = rec.get("expected_lead_count")
        actual = client.count_leads(campaign_id=cid)
        if expected is not None and actual != expected:
            item["issues"].append(f"lead count {actual} != expected {expected}")
        item["leads"] = actual
        item["status"] = "OK" if not item["issues"] else "FAILED"
        out.append(item)
    return out


def same_launch(recorded_path: Optional[str], cfg: LaunchConfig) -> bool:
    """Records store absolute paths (web app); CLI calls may use relative ones."""
    if not recorded_path:
        return False
    try:
        return Path(recorded_path).resolve() == cfg.path.resolve()
    except OSError:
        return False
