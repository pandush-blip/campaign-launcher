"""Create OutFalcon draft campaigns from a launch folder.

Same contract as the Instantly pipeline: duplicate the client's settings campaign, replace
the sequence with the segment's copy, import + enroll its leads, then verify everything by
reading it back. Campaigns stay in `draft` until the operator launches them explicitly.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import state
from .config import LaunchConfig, SegmentConfig, render_campaign_name
from .copy_parser import CopyParseError, parse_copy_file
from .create import (
    LaunchRun,
    SegmentPlan,
    SegmentRun,
    build_plan,
    same_launch,
)
from .leads import LeadBuild, SegmentLeads, build_leads
from .outfalcon import OutFalconApiError, OutFalconClient, OutFalconNotFound
from .outfalcon_adapter import leads_to_csv, to_outfalcon_body, to_outfalcon_subject
from .qa import BLOCK, analyze_launch

IMPORT_CHUNK = 5000  # OutFalcon imports up to 5,000 rows inline per call
# Enrolling one huge list is a single heavy API call; lists are split so every enroll
# stays bounded (and a retry only redoes that slice - already-enrolled leads are skipped).
ENROLL_CHUNK = max(500, int(os.environ.get("TRI_LAUNCH_OF_ENROLL_CHUNK", "2500") or 2500))


def _copy_path(cfg: LaunchConfig, seg: SegmentConfig) -> Path:
    path = cfg.path / seg.copy
    if path.is_file():
        return path
    return cfg.path / "copy" / (seg.copy or f"{seg.slug}.md")


def _tracked_campaign(cfg: LaunchConfig, name: str) -> Optional[dict[str, Any]]:
    for cid, rec in state.load()["created"].items():
        if (
            rec.get("name") == name
            and same_launch(rec.get("launch_path"), cfg)
            and (rec.get("provider") or "instantly") == "outfalcon"
        ):
            return {"id": cid, **rec}
    return None


def _variant_html_and_subject(copy_file, step_index: int) -> list[dict[str, str]]:
    """Convert one copy step's variants into OutFalcon templates."""
    step = copy_file.steps[step_index]
    out: list[dict[str, str]] = []
    for variant in step.variants:
        subject, _ = to_outfalcon_subject(variant.subject if step_index == 0 else "")
        body, _ = to_outfalcon_body(variant.body)
        out.append({"label": variant.label, "subject": subject, "body_html": body})
    return out


def _replace_sequence(client: OutFalconClient, campaign_id: str, copy_file) -> dict[str, Any]:
    """Delete the inherited steps and write the segment's steps + variants."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _replace_sequence_once(client, campaign_id, copy_file)
        except Exception as exc:  # noqa: BLE001 - 5xx storms on the write path are transient
            message = str(exc)
            transient = any(
                marker in message
                for marker in ("-> 429", "-> 500", "-> 502", "-> 503", "-> 504", "ReadTimeout", "ConnectError")
            )
            last_error = exc
            if not transient or attempt == 2:
                raise
            print(
                f"[progress] sequence write failed ({message[:90]}), retrying "
                f"{attempt + 1}/2 in 15s",
                flush=True,
            )
            time.sleep(15)
    raise last_error or RuntimeError("sequence write failed")


def _replace_sequence_once(client: OutFalconClient, campaign_id: str, copy_file) -> dict[str, Any]:
    sequences = client.get_sequences(campaign_id)
    for step in sequences.get("steps") or []:
        step_id = step.get("id")
        if step_id:
            try:
                client.delete_step(step_id)
            except OutFalconNotFound:
                pass
    expected: list[dict[str, Any]] = []
    for index, step in enumerate(copy_file.steps):
        created = client.add_step(
            campaign_id,
            delay_days=int(step.delay_days),
            delay_hours=int(round((float(step.delay_days) % 1) * 24)),
        )
        step_id = created.get("id") if isinstance(created, dict) else None
        # Re-read to get the default 'A' variant the API seeds with the step.
        fresh = client.get_sequences(campaign_id)
        steps = fresh.get("steps") or []
        target = next((s for s in steps if s.get("id") == step_id), steps[-1] if steps else None)
        if not target:
            raise OutFalconApiError("POST", "sequences", 200, "step not found after create")
        variants = _variant_html_and_subject(copy_file, index)
        weight = 100 // max(1, len(variants))
        existing = list(target.get("variants") or [])
        written: list[dict[str, Any]] = []
        for vi, variant in enumerate(variants):
            if vi < len(existing):
                variant_id = existing[vi].get("id")
            else:
                added = client.add_variant(target["id"], f"Variant {chr(65 + vi)}")
                variant_id = added.get("id")
            client.patch_variant(
                variant_id,
                subject=variant["subject"],
                body_html=variant["body_html"],
                weight=100 if len(variants) == 1 else weight,
            )
            written.append({"variant_id": variant_id, **variant})
        # Remove any extra inherited variants beyond the ones we wrote.
        for extra in existing[len(variants) :]:
            if extra.get("id"):
                try:
                    client.delete_variant(extra["id"])
                except OutFalconNotFound:
                    pass
        expected.append({"step_id": target["id"], "variants": written})
    return {"expected": expected}


def _import_leads(
    client: OutFalconClient, list_name: str, leads: SegmentLeads, campaign_id: str
) -> dict[str, int]:
    """Import the segment's leads into (chunked) lists and enroll each list."""
    imported = 0
    skipped = 0
    errors = 0
    added = 0
    already = 0
    other_campaigns = 0
    total = len(leads.leads)
    chunks = [
        leads.leads[start : start + ENROLL_CHUNK]
        for start in range(0, total, ENROLL_CHUNK)
    ] or [[]]
    for index, chunk in enumerate(chunks, start=1):
        chunk_name = list_name if len(chunks) == 1 else f"{list_name} {index}-{len(chunks)}"
        existing = client.find_lead_list(chunk_name)
        list_id = existing.get("id") if existing else None
        if list_id is None:
            list_id = client.create_lead_list(
                chunk_name, description="Created by the launcher"
            ).get("id")
        if chunk:
            for start in range(0, len(chunk), IMPORT_CHUNK):
                slice_rows = chunk[start : start + IMPORT_CHUNK]
                csv_text, mapping = leads_to_csv(slice_rows)
                result = client.import_csv(list_id, csv_text, mapping)
                if result.get("job_id"):
                    job = client.wait_for_job(result["job_id"])
                    result = job.get("result") or {}
                imported += int(result.get("imported") or 0)
                skipped += int(result.get("skipped") or 0)
                errors += len(result.get("errors") or [])
        enrollment = client.enroll_list(campaign_id, list_id)
        added += int(enrollment.get("added") or 0)
        already += int(enrollment.get("skipped") or 0)
        other_campaigns += int(enrollment.get("skippedOtherCampaigns") or 0)
    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "added": added,
        "already": already,
        "other_campaigns": other_campaigns,
        "lists": len(chunks),
    }


def create_segment_outfalcon(
    cfg: LaunchConfig,
    seg: SegmentConfig,
    plan: SegmentPlan,
    leads: SegmentLeads,
    client: OutFalconClient,
    source: dict[str, Any],
    *,
    run: LaunchRun,
) -> SegmentRun:
    result = SegmentRun(slug=seg.slug, campaign_name=plan.campaign_name)

    known = _tracked_campaign(cfg, plan.campaign_name)
    current_leads = 0
    if known:
        campaign_id = known["id"]
        result.campaign_id = campaign_id
        result.resumed = True
        campaign = client.get_campaign(campaign_id)
        current_leads = int(campaign.get("lead_count") or 0)
        if campaign.get("status") != "draft":
            result.status = "FAILED"
            result.issues.append(
                f"existing campaign is {campaign.get('status')}, not draft - refusing to touch it"
            )
            return result
    else:
        existing = client.find_campaign_by_name(plan.campaign_name)
        if existing:
            result.status = "FAILED"
            result.issues.append(
                f"campaign named {plan.campaign_name!r} already exists in OutFalcon but is "
                "not tracked by this tool - refusing to touch it"
            )
            return result
        duplicated = client.duplicate_campaign(source["id"], plan.campaign_name)
        campaign_id = duplicated.get("id")
        result.campaign_id = campaign_id
        state.register_campaign(
            campaign_id,
            duplicated.get("name") or plan.campaign_name,
            source_id=source.get("id"),
            purpose="launch",
            extra={
                "launch_path": str(cfg.path),
                "client": cfg.client,
                "client_env": cfg.raw.get("api_key_env") or "",
                "provider": "outfalcon",
                "segment": seg.slug,
            },
        )

    try:
        copy_file = parse_copy_file(_copy_path(cfg, seg))
    except CopyParseError as exc:
        result.status = "FAILED"
        result.issues.append(f"copy parse error: {exc}")
        return result
    if not any(v.body.strip() for step in copy_file.steps for v in step.variants):
        result.status = "FAILED"
        result.issues.append("copy file has no email variants")
        return result

    try:
        sequence = _replace_sequence(client, campaign_id, copy_file)
        if current_leads >= plan.leads_count and current_leads > 0:
            # Resumed campaign that already has its leads: only the sequence needed the
            # fix (e.g. variable tags), so skip the re-import entirely.
            imported = {"imported": 0, "skipped": 0, "errors": 0, "added": 0,
                        "already": current_leads, "other_campaigns": 0, "lists": 0}
            print(
                f"[progress] {plan.campaign_name}: {current_leads} leads already enrolled, "
                "sequence re-patched only",
                flush=True,
            )
        else:
            imported = _import_leads(
                client, f"{plan.campaign_name} - leads", leads, campaign_id
            )
    except Exception as exc:  # noqa: BLE001
        result.status = "FAILED"
        result.issues.append(f"write failed: {type(exc).__name__}: {exc}")
        return result

    result.leads_uploaded = int(imported.get("added") or 0)
    result.leads_skipped = int(imported.get("already") or 0) + int(imported.get("skipped") or 0)
    result.leads_rejected = int(imported.get("other_campaigns") or 0) + int(imported.get("errors") or 0)

    # -- readback ---------------------------------------------------------------
    diffs: list[str] = []
    got = client.get_campaign(campaign_id)
    if got.get("name") != plan.campaign_name:
        diffs.append(f"name: {got.get('name')!r} != {plan.campaign_name!r}")
    if got.get("status") != "draft":
        diffs.append(f"status after create is {got.get('status')!r}, not draft")
    fresh = client.get_sequences(campaign_id).get("steps") or []
    if len(fresh) != len(sequence["expected"]):
        diffs.append(f"step count: sent {len(sequence['expected'])}, got {len(fresh)}")
    else:
        for index, (sent_step, got_step) in enumerate(zip(sequence["expected"], fresh), start=1):
            sent_variants = sent_step["variants"]
            got_variants = got_step.get("variants") or []
            if len(sent_variants) != len(got_variants):
                diffs.append(
                    f"step{index} variant count: sent {len(sent_variants)}, got {len(got_variants)}"
                )
                continue
            by_id = {v.get("id"): v for v in got_variants}
            for sent_variant in sent_variants:
                found = by_id.get(sent_variant["variant_id"])
                if found is None:
                    diffs.append(f"step{index} {sent_variant['label']}: variant missing")
                    continue
                if (found.get("subject") or "") != sent_variant["subject"]:
                    diffs.append(f"step{index} {sent_variant['label']}: subject differs")
                if (found.get("body_html") or "") != sent_variant["body_html"]:
                    diffs.append(f"step{index} {sent_variant['label']}: body differs")
    expected_leads = result.leads_uploaded + result.leads_skipped
    # `lead_count` on the campaign is engine-maintained and one request; paginating every
    # campaign lead (100/page) took minutes on multi-thousand-lead campaigns.
    actual = got.get("lead_count")
    if actual is None:
        actual = client.count_campaign_leads(campaign_id)
    actual = int(actual)
    result.leads_in_campaign = actual
    if actual != expected_leads:
        diffs.append(f"lead count: enrolled {expected_leads}, in OutFalcon {actual}")

    state.register_campaign(
        campaign_id,
        plan.campaign_name,
        source_id=source.get("id"),
        purpose="launch",
        extra={
            "launch_path": str(cfg.path),
            "client": cfg.client,
            "client_env": cfg.raw.get("api_key_env") or "",
            "provider": "outfalcon",
            "segment": seg.slug,
            "expected_lead_count": actual,
            "outfalcon_sequences": json.dumps(sequence["expected"])[:20000],
        },
    )
    if diffs:
        result.status = "FAILED"
        result.issues += diffs
    else:
        result.status = "PASS"
        note = f"{actual} leads read back"
        if result.leads_skipped:
            note += f" ({result.leads_skipped} skipped)"
        result.issues.append(note)
    return result


def create_launch_outfalcon(
    cfg: LaunchConfig,
    client: OutFalconClient,
    *,
    only: Optional[list[str]] = None,
) -> LaunchRun:
    qa = analyze_launch(cfg)
    build: LeadBuild = build_leads(
        segments=cfg.segments,
        lead_mode=cfg.lead_mode,
        split_column=cfg.split_column,
        dedupe_across_segments=cfg.dedupe_across_segments,
        base_dir=cfg.path,
        variable_map=cfg.variable_map,
    )
    run = build_plan(cfg, qa, build)
    source = client.resolve_campaign(cfg.settings_campaign)
    run.source = {"id": source["id"], "name": source.get("name"), "status": source.get("status")}
    selected = [
        p
        for p in run.plans
        if (not only or p.slug in only) and not p.blocked
    ]
    for plan in selected:
        seg = cfg.segment(plan.slug)
        print(
            f"[progress] campaign={plan.slug} state=start leads={plan.leads_count}",
            flush=True,
        )
        result = create_segment_outfalcon(
            cfg, seg, plan, build.segments[plan.slug], client, source, run=run
        )
        run.segments.append(result)
        print(
            f"[progress] campaign={plan.slug} state={result.status} "
            f"in_campaign={result.leads_in_campaign}",
            flush=True,
        )
    run.status = (
        "PASS" if run.segments and all(s.status == "PASS" for s in run.segments) else "FAILED"
    )
    if not run.segments:
        run.status = "NOTHING-TO-DO"
    return run
