"""Phase 0 discovery driver (spec section 5).

Uses only `TEST_SETTINGS_CAMPAIGN`: duplicate it to a scratch draft, run the discovery
experiments, read everything back, delete the scratch campaign. Never activates
anything. Raw evidence is written to `state/phase0/phase0_<date>.json`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import state
from .allowlist import UUID
from .envload import get_api_key, get_test_settings_campaign, load_env
from .instantly import InstantlyApiError, InstantlyClient, NotFound

log = logging.getLogger("tri_launch.phase0")

IDENTITY_KEYS = {
    "id",
    "name",
    "timestamp_created",
    "timestamp_updated",
    "organization",
    "created_by",
    "owned_by",
    "ai_sdr_id",
}


def scratch_name(date_str: str) -> str:
    return f"TEST | launcher phase0 | {date_str}"


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k in sorted(obj):
            out.update(_flatten(obj[k], f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def deep_diff(original: dict[str, Any], duplicate: dict[str, Any]) -> dict[str, Any]:
    fa, fb = _flatten(original), _flatten(duplicate)
    equal, different, only_original, only_duplicate = [], {}, {}, {}
    for k in sorted(set(fa) | set(fb)):
        if k in IDENTITY_KEYS:
            continue
        if k not in fa:
            only_duplicate[k] = fb[k]
        elif k not in fb:
            only_original[k] = fa[k]
        elif fa[k] == fb[k]:
            equal.append(k)
        else:
            different[k] = {"original": fa[k], "duplicate": fb[k]}
    return {
        "inherited_equal": equal,
        "different": different,
        "only_in_original": only_original,
        "only_in_duplicate": only_duplicate,
        "identity_fields": {
            k: {"original": fa.get(k), "duplicate": fb.get(k)}
            for k in sorted(IDENTITY_KEYS)
            if k in fa or k in fb
        },
    }


def text_to_html(text: str) -> str:
    """Provisional converter: blank line -> paragraph break, newline -> line break."""
    paragraphs = [p.strip("\n") for p in text.strip("\n").split("\n\n")]
    return "<br/><br/>".join(p.replace("\n", "<br/>") for p in paragraphs)


UNSUB_PRIMARY_FALLBACK = (
    "{{RANDOM | Note: | Quick note: | Just a note: | Important: | FYI: | Heads up:}} "
    "{{RANDOM | If you don't want to hear from me anymore | If you'd prefer not to hear "
    "from me again | If you don't want to receive messages from me | If you don't want to "
    "hear from me | If you'd rather not receive any more messages from me | If you want to "
    "be taken out of my list | If you don't want any more communication from me | If you "
    "want to end our correspondence | If you'd like to end receiving my emails | If you "
    "prefer no further contact}}, just {{RANDOM | write | reply | respond with | send | "
    "message | email | shoot a message saying | drop a note with | simply type | say}} "
    "'no thanks' {{RANDOM | and I'll make sure it happens | and I'll respect your wishes | "
    "and I'll make sure to let you out of my list | and you'll not hear from me again | and "
    "I will not be sending you emails | and that will be the end of it | and I'll ensure no "
    "more emails are sent to you | and you'll be unsubscribed | and you'll no longer "
    "receive my messages}}."
)
UNSUB_FOLLOWUP_FALLBACK = (
    "{{RANDOM | Note: | Quick note: | FYI: | Heads up:}} {{RANDOM | If you'd prefer not to "
    "hear from me again | If you don't want any more messages from me | If you'd rather I "
    "stop reaching out}}, {{RANDOM | reply with 3 and I'll close this out | just send back "
    "3 and I'll remove you from my follow-ups | send me a 3 and you won't hear from me "
    "again}}."
)


def extract_unsubscribe(reference: dict[str, Any]) -> dict[str, Any]:
    """Pull the house unsubscribe sentences out of a campaign's sequences.

    His copy stores paragraphs as `<div>...</div>` blocks; the unsubscribe sentence is
    the div containing 'no thanks' (primary) or 'send me a 3' (follow-up/short step).
    Returns the raw inner HTML so the Phase 0 test copy uses his wording verbatim.
    """
    steps = (reference.get("sequences") or [{}])[0].get("steps", [])
    found: list[dict[str, Any]] = []
    for si, st in enumerate(steps or []):
        for vi, v in enumerate(st.get("variants") or []):
            body = v.get("body") or ""
            for m in re.finditer(r"<div[^>]*>(.*?)</div>", body, re.S):
                inner = m.group(1).strip()
                if re.search(
                    r"(?i)no thanks|send me a 3|reply with 3|send back 3|remove you from my follow-ups",
                    inner,
                ):
                    found.append({"step": si + 1, "variant": vi + 1, "html": inner})
    primary = next((f for f in found if re.search(r"(?i)no thanks", f["html"])), None)
    followup = next((f for f in found if re.search(r"(?i)send me a 3|reply with 3", f["html"])), None)
    return {"found": found, "primary": primary, "followup": followup}


def _div_html(*paragraphs: str) -> str:
    """Instantly-editor style: one <div> per paragraph, blank <div><br /></div> between."""
    parts: list[str] = []
    for i, p in enumerate(paragraphs):
        if i:
            parts.append("<div><br /></div>")
        parts.append(f"<div>{p.replace(chr(10), '<br />')}</div>")
    return "".join(parts)


def _phase0_sequences(unsub_primary: str, unsub_followup: str) -> dict[str, Any]:
    plain_paras = [
        "Hi {{firstName | there}},",
        "Paragraph one of the Phase 0 format test for {{companyName}}.",
        unsub_primary,
        "{{accountSignature}}",
    ]
    body_plain = "\n\n".join(plain_paras)  # raw newlines: see what Instantly stores
    body_br = text_to_html(body_plain)  # <br/><br/> form
    body_div = _div_html(*plain_paras)  # editor-style div form
    followup_body_div = _div_html(
        "{{RANDOM | Following up | Bumping this}} {{firstName | there}},",
        "Second step of the Phase 0 format test.",
        unsub_followup,
        "{{accountSignature}}",
    )
    return {
        "sequences": [
            {
                "steps": [
                    {
                        "type": "email",
                        "delay": 0,
                        "delay_unit": "days",
                        "variants": [
                            {
                                "subject": "Phase0 format test A",
                                "body": body_plain,  # raw newlines: see what Instantly stores
                            },
                            {
                                "subject": "Phase0 format test B",
                                "body": body_br,  # <br/><br/> form
                            },
                            {
                                "subject": "Phase0 format test C",
                                "body": body_div,  # editor-style div form
                            },
                        ],
                    },
                    {
                        "type": "email",
                        "delay": 3,
                        "delay_unit": "days",
                        "variants": [
                            {
                                "subject": "",  # empty subject: threaded follow-up?
                                "body": followup_body_div,
                            }
                        ],
                    },
                ]
            }
        ]
    }


def _three_step_sequences() -> dict[str, Any]:
    return {
        "sequences": [
            {
                "steps": [
                    {
                        "type": "email",
                        "delay": 0,
                        "delay_unit": "days",
                        "variants": [{"subject": "Replace test step 1", "body": "One<br/>"}],
                    },
                    {
                        "type": "email",
                        "delay": 2,
                        "delay_unit": "days",
                        "variants": [{"subject": "Replace test step 2", "body": "Two<br/>"}],
                    },
                    {
                        "type": "email",
                        "delay": 2,
                        "delay_unit": "days",
                        "variants": [{"subject": "Replace test step 3", "body": "Three<br/>"}],
                    },
                ]
            }
        ]
    }


def _test_leads() -> list[dict[str, Any]]:
    base = []
    for i in range(1, 6):
        custom = {"list_name": "Phase 0 Test", "phase0_custom": f"alpha{i}"}
        if i == 3:
            # never registered upfront: proves leads/add auto-registers new variables
            custom["phase0_auto"] = "auto-registered"
        base.append(
            {
                "email": f"phase0.lead{i}@example.com",
                "first_name": f"Test{i}",
                "last_name": "PhaseZero",
                "company_name": f"Phase Zero Co {i}",
                "website": f"https://example.com/phase0/{i}",
                "phone": f"+1555000000{i}",
                "personalization": f"Personalization line {i}",
                "custom_variables": custom,
            }
        )
    return base


def _draft_or_pause(client: InstantlyClient, campaign_id: str, *, where: str) -> dict[str, Any]:
    camp = client.get_campaign(campaign_id)
    if camp.get("status") != 0:
        print("!" * 78, file=sys.stderr)
        print(f"!! NOT DRAFT at {where}: campaign {campaign_id} status={camp.get('status')}",
              file=sys.stderr)
        print("!" * 78, file=sys.stderr)
        client.pause_campaign_safety(campaign_id, reason=f"safety:not-draft-after-{where}")
        raise SystemExit(3)
    return camp


def resolve_settings_campaign(client: InstantlyClient, ref: str) -> dict[str, Any]:
    try:
        return client.resolve_campaign(ref)
    except LookupError as exc:
        raise SystemExit(str(exc)) from exc


def run_phase0(
    *,
    date_str: Optional[str] = None,
    env_file: str = ".env",
    blocklist_email: Optional[str] = None,
    keep: bool = False,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_env(env_file)
    key_name, api_key = get_api_key()
    settings_ref = get_test_settings_campaign()
    date_str = date_str or date_cls.today().isoformat()
    name = scratch_name(date_str)
    stamp = datetime.now().strftime("%H%M%S")
    evidence: dict[str, Any] = {
        "date": date_str,
        "key_env_var": key_name,  # name only, never the value
        "settings_ref": settings_ref,
        "scratch_name": name,
        "steps": {},
    }
    artifact = Path(state.state_dir()) / "phase0" / f"phase0_{date_str}_{stamp}.json"
    client: Optional[InstantlyClient] = None
    scratch_id: Optional[str] = None
    exit_code = 0
    try:
        client = InstantlyClient(api_key)
        accounts = client.list_accounts(limit=100)
        evidence["accounts_count"] = len(accounts)
        print(f"Sending accounts visible to this key: {len(accounts)}")

        settings = resolve_settings_campaign(client, settings_ref)
        evidence["settings_campaign"] = settings
        print(f"Settings campaign: {settings['name']} ({settings['id']}) status={settings['status']}")

        unsub = extract_unsubscribe(settings)
        evidence["settings_unsubscribe"] = unsub
        unsub_primary = (unsub.get("primary") or {}).get("html") or UNSUB_PRIMARY_FALLBACK
        unsub_followup = (unsub.get("followup") or {}).get("html") or UNSUB_FOLLOWUP_FALLBACK
        evidence["unsub_primary_used"] = unsub_primary
        evidence["unsub_followup_used"] = unsub_followup
        print(
            "Unsubscribe sentences found in source campaign:",
            len(unsub["found"]),
            "(using",
            "source" if unsub.get("primary") else "fallback",
            "wording)",
        )

        dup = client.duplicate_campaign(settings["id"], name)
        scratch_id = dup["id"]
        state.register_campaign(
            scratch_id, dup.get("name", name), source_id=settings["id"], purpose="phase0-scratch"
        )
        evidence["scratch_id"] = scratch_id
        print(f"Scratch duplicate: {scratch_id}")

        dup_read = _draft_or_pause(client, scratch_id, where="duplicate")
        evidence["steps"]["duplicate_readback"] = dup_read
        evidence["steps"]["inheritance_diff"] = deep_diff(settings, dup_read)
        source_count = client.count_leads(campaign_id=settings["id"], cap=1000)
        evidence["steps"]["leads_count_source"] = source_count
        evidence["steps"]["leads_count_source_capped_at"] = 1000 if source_count >= 1000 else None
        evidence["steps"]["leads_count_scratch_initial"] = client.count_leads(campaign_id=scratch_id)

        # Body-format + sequence patch tests
        sent_sequences = _phase0_sequences(unsub_primary, unsub_followup)
        client.patch_campaign(scratch_id, sent_sequences)
        after_patch = client.get_campaign(scratch_id)
        evidence["steps"]["sequence_sent"] = sent_sequences
        evidence["steps"]["sequence_readback"] = after_patch.get("sequences")

        sent_replace = _three_step_sequences()
        client.patch_campaign(scratch_id, sent_replace)
        after_replace = client.get_campaign(scratch_id)
        evidence["steps"]["sequence_replace_sent_steps"] = len(sent_replace["sequences"][0]["steps"])
        try:
            got_steps = len(after_replace["sequences"][0]["steps"])
        except Exception:
            got_steps = None
        evidence["steps"]["sequence_replace_readback_steps"] = got_steps
        evidence["steps"]["sequence_replace_readback"] = after_replace.get("sequences")

        # Variables
        variables = ["list_name", "phase0_custom"]
        client.add_campaign_variables(scratch_id, variables)
        after_vars = client.get_campaign(scratch_id)
        evidence["steps"]["variables_sent"] = variables
        evidence["steps"]["custom_variables_after_register"] = after_vars.get("custom_variables")
        evidence["steps"]["core_variables_after_register"] = after_vars.get("core_variables")

        # Leads
        leads = _test_leads()
        created_lead_ids: list[str] = []
        r1 = client.add_leads(scratch_id, leads)
        evidence["steps"]["leads_add_batch1"] = r1
        created_lead_ids += [lead["id"] for lead in (r1.get("created_leads") or []) if lead.get("id")]
        r2 = client.add_leads(scratch_id, [leads[0], leads[1]])
        evidence["steps"]["leads_add_duplicate_across_requests"] = r2
        r3 = client.add_leads(scratch_id, [leads[2], {"email": "not-an-email"}])
        evidence["steps"]["leads_add_duplicate_in_payload_and_invalid"] = r3
        r4 = client.add_leads(
            scratch_id,
            [{"email": "phase0.dup@example.com"}, {"email": "phase0.dup@example.com"}],
        )
        evidence["steps"]["leads_add_duplicate_within_payload"] = r4
        created_lead_ids += [lead["id"] for lead in (r4.get("created_leads") or []) if lead.get("id")]
        if blocklist_email:
            r5 = client.add_leads(scratch_id, [{"email": blocklist_email}])
            evidence["steps"]["leads_add_blocklist_probe"] = r5

        back = client.all_leads(campaign_id=scratch_id)
        evidence["steps"]["leads_count_readback"] = len(back)
        evidence["steps"]["leads_readback"] = back
        after_leads = client.get_campaign(scratch_id)
        evidence["steps"]["custom_variables_after_leads"] = after_leads.get("custom_variables")

        # Find what the duplicate inherited for leads vs. the settings campaign
        _draft_or_pause(client, scratch_id, where="final-readback")
        deleted = client.delete_leads(scratch_id, created_lead_ids, require_phase0_pattern=True)
        evidence["steps"]["test_leads_deleted"] = len(created_lead_ids)
        evidence["steps"]["test_leads_delete_response"] = deleted
        print(
            "Batch1 counters:",
            {k: r1.get(k) for k in ("total_sent", "leads_uploaded", "duplicated_leads",
                                    "duplicate_email_count", "invalid_email_count", "in_blocklist")},
        )
    except SystemExit:
        raise
    except Exception as exc:  # keep evidence, still attempt cleanup below
        exit_code = 1
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("Phase 0 aborted")
    finally:
        if client is not None and scratch_id:
            try:
                camp = client.get_campaign(scratch_id)
                if camp.get("status") == 0 and not keep:
                    client.delete_campaign(scratch_id, confirm_name=camp["name"])
                    evidence["cleanup"] = {"deleted": True, "name": camp["name"]}
                    print(f"Scratch campaign deleted: {camp['name']} ({scratch_id})")
                elif not keep:
                    client.pause_campaign_safety(scratch_id, reason="safety:not-draft-at-cleanup")
                    evidence["cleanup"] = {"deleted": False, "paused": True}
                    exit_code = 3
                else:
                    evidence["cleanup"] = {"deleted": False, "kept": True}
            except NotFound:
                evidence["cleanup"] = {"deleted": True, "note": "already absent"}
                state.mark_deleted(scratch_id)
            except Exception as exc:  # noqa: BLE001
                exit_code = 2
                evidence["cleanup"] = {"deleted": False, "error": f"{type(exc).__name__}: {exc}"}
                log.exception("Cleanup failed")
            try:
                orphans = client.all_leads(search="phase0.")
                cleaned = 0
                groups: dict[str, list[dict[str, Any]]] = {}
                for lead in orphans:
                    cid = lead.get("campaign")
                    if cid:
                        groups.setdefault(cid, []).append(lead)
                for cid, group in groups.items():
                    if state.known_campaign(cid):
                        ids = [lead["id"] for lead in group]
                        client.delete_leads(cid, ids, require_phase0_pattern=True)
                        cleaned += len(ids)
                leftovers = client.all_leads(search="phase0.")
                evidence["leftover_test_leads"] = [
                    {"email": l.get("email"), "campaign": l.get("campaign")} for l in leftovers
                ]
                evidence["orphan_test_leads_cleaned"] = cleaned
            except Exception as exc:  # noqa: BLE001
                evidence["leftover_test_leads"] = f"probe failed: {type(exc).__name__}"
            client.close()
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Evidence written to {artifact}")
        print(f"exit_code={exit_code}")
    return exit_code
