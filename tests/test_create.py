from datetime import date
from pathlib import Path

from tri_launch import state
from tri_launch.config import load_launch
import pytest

from tri_launch.create import Interrupted, create_launch, launch_totals, verify_launch

GOOD_COPY = """# Step 1
delay: 0

## Variant A
Subject: Quick question
Body:
Hi {{firstName | there}},

Quick question about {{companyName | your company}} and {{industry}}.

Not interested? Just reply "no thanks."
"""

SOURCE = {
    "id": "01e70d3a-64fd-4192-91fb-0346f8443155",
    "name": "Clay Audiences, US, CA, UK, 30.09.2026",
    "status": 1,
    "campaign_schedule": {"schedules": [{"name": "U.S Time"}]},
    "email_list": [],
    "email_tag_list": ["tag-1"],
    "daily_limit": 15000,
    "text_only": True,
    "first_email_text_only": True,
    "stop_on_reply": True,
    "stop_on_auto_reply": False,
    "link_tracking": False,
    "open_tracking": False,
    "insert_unsubscribe_header": True,
}


class FakeClient:
    def __init__(self, *, untracked_existing: bool = False):
        self.campaigns: dict[str, dict] = {}
        self.lead_counts: dict[str, int] = {}
        self.lead_sets: dict[str, set] = {}
        self.variables: dict[str, list[str]] = {}
        self.upload_calls: list[dict] = []
        self.untracked_existing = untracked_existing
        self._n = 0
        self._lead_n = 0

    def resolve_campaign(self, ref):
        return dict(SOURCE)

    def find_campaign_by_name(self, name):
        if self.untracked_existing and name.endswith(
            f"Segment One | Guarantee | {date.today().isoformat()}"
        ):
            return [{"id": "untracked", "name": name}]
        return []

    def duplicate_campaign(self, source_id, name):
        self._n += 1
        cid = f"01a0fa44-0000-4000-8000-{self._n:012d}"
        camp = dict(SOURCE)
        camp.update({"id": cid, "name": name, "status": 0})
        self.campaigns[cid] = camp
        self.lead_counts[cid] = 0
        self.lead_sets[cid] = set()
        return dict(camp)

    def get_campaign(self, cid):
        return dict(self.campaigns[cid])

    def patch_campaign(self, cid, payload):
        self.campaigns[cid]["sequences"] = payload["sequences"]
        return dict(self.campaigns[cid])

    def add_campaign_variables(self, cid, names):
        self.variables[cid] = list(names)

    def add_leads(self, cid, leads, *, skip_if_in_workspace=None, **kwargs):
        self.upload_calls.append({"skip": skip_if_in_workspace, "count": len(leads)})
        mailed = {l["email"] for l in leads}
        known = self.lead_sets.setdefault(cid, set())
        if skip_if_in_workspace:
            workspace = set().union(*self.lead_sets.values()) if self.lead_sets else set()
            skipped = len(mailed & workspace)
            new = mailed - workspace
        else:
            skipped = 0
            new = mailed - known
        dupes = len(mailed & known) - skipped if skipped else len(mailed & known)
        new_ids = []
        for _ in new:
            self._lead_n += 1
            new_ids.append(f"01a0fa44-1111-4000-8000-{self._lead_n:012d}")
        state.add_lead_ids(cid, new_ids)
        known |= new
        self.lead_counts[cid] = len(known)
        return {
            "status": "success",
            "total_sent": len(leads),
            "leads_uploaded": len(new),
            "duplicated_leads": max(0, dupes),
            "skipped_count": skipped,
            "invalid_email_count": 0,
            "in_blocklist": 0,
        }

    def count_leads(self, *, campaign_id=None, search=None):
        return self.lead_counts.get(campaign_id, 0)

    def delete_leads(self, cid, ids, require_phase0_pattern=False):
        self.lead_counts[cid] = max(0, self.lead_counts.get(cid, 0) - len(ids))
        return {"count": len(ids)}

    def pause_campaign_safety(self, cid, reason):
        self.campaigns[cid]["status"] = 2


def make_launch(tmp_path: Path) -> Path:
    d = tmp_path / "launch"
    (d / "copy").mkdir(parents=True)
    (d / "leads").mkdir(parents=True)
    (d / "copy" / "seg.md").write_text(GOOD_COPY, encoding="utf-8")
    rows = ["email,first_name,company_name,industry"]
    for i in range(7):
        rows.append(f"lead{i}@example.com,First{i},Company {i},SaaS")
    (d / "leads" / "seg.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (d / "launch.yaml").write_text(
        """client: Demo Client
settings_campaign: "Clay Audiences, US, CA, UK, 30.09.2026"
api_key_env: INSTANTLY_API_KEY
naming: "{client} | {segment} | {angle} | {date}"
lead_mode: per_file
segments:
  - slug: seg
    name: Segment One
    angle: Guarantee
    leads: leads/seg.csv
    copy: copy/seg.md
    priority: 1
lint:
  ban_em_dash: true
  max_body_words: 120
  max_links: 1
unsubscribe:
  mode: append
""",
        encoding="utf-8",
    )
    return d


def test_create_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    cfg = load_launch(make_launch(tmp_path))
    run = create_launch(cfg, FakeClient())
    assert run.status == "PASS"
    seg = run.segments[0]
    assert seg.status == "PASS"
    assert seg.leads_uploaded == 7
    assert seg.leads_in_campaign == 7
    assert launch_totals(run) == {
        "campaigns": 1,
        "added": 7,
        "uploaded": 7,
        "duplicated": 0,
        "skipped": 0,
        "rejected": 0,
    }
    assert seg.campaign_name == (
        f"Demo Client | Segment One | Guarantee | {date.today().isoformat()}"
    )
    # readback + state
    rec = state.load()["created"][seg.campaign_id]
    assert rec["expected_lead_count"] == 7
    assert rec["sequences"]["sequences"][0]["steps"][0]["variants"][0]["body"].startswith("<div>")


def test_create_registers_custom_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    cfg = load_launch(make_launch(tmp_path))
    client = FakeClient()
    run = create_launch(cfg, client)
    cid = run.segments[0].campaign_id
    assert "industry" in client.variables[cid]
    assert "list_name" in client.variables[cid]
    assert client.upload_calls[0]["skip"] is True


def test_create_refuses_untracked_existing_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    cfg = load_launch(make_launch(tmp_path))
    run = create_launch(cfg, FakeClient(untracked_existing=True))
    assert run.status == "FAILED"
    assert "not tracked" in run.segments[0].issues[0]


def test_verify_reports_created_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    cfg = load_launch(make_launch(tmp_path))
    client = FakeClient()
    run = create_launch(cfg, client)
    results = verify_launch(cfg, client)
    assert len(results) == 1
    assert results[0]["status"] == "OK"
    assert results[0]["leads"] == 7
    # break the campaign: verify pauses and reports
    client.campaigns[run.segments[0].campaign_id]["status"] = 1
    results = verify_launch(cfg, client)
    assert results[0]["status"] in {"FAILED", "UNKNOWN"}
    assert client.campaigns[run.segments[0].campaign_id]["status"] == 2


def test_split_mode_dedupes_across_segments_before_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    d = tmp_path / "launch"
    (d / "copy").mkdir(parents=True)
    (d / "leads").mkdir(parents=True)
    (d / "leads" / "all_leads.csv").write_text(
        "email,first_name,segment,industry\n"
        "a@x.com,A,alpha,SaaS\n"
        "shared@x.com,S,alpha,SaaS\n"
        "shared@x.com,S,beta,SaaS\n"
        "b@x.com,B,beta,SaaS\n"
        "c@x.com,C,gamma,SaaS\n",
        encoding="utf-8",
    )
    for slug in ("alpha", "beta", "gamma"):
        (d / "copy" / f"{slug}.md").write_text(GOOD_COPY, encoding="utf-8")
    (d / "launch.yaml").write_text(
        """client: Split Demo
settings_campaign: "Clay Audiences, US, CA, UK, 30.09.2026"
naming: "{client} | {segment} | {date}"
lead_mode: split_column
split_column: segment
dedupe_across_segments: true
segments:
  - slug: alpha
    name: Alpha
    leads: leads/all_leads.csv
    copy: copy/alpha.md
    priority: 1
  - slug: beta
    name: Beta
    leads: leads/all_leads.csv
    copy: copy/beta.md
    priority: 2
  - slug: gamma
    name: Gamma
    leads: leads/all_leads.csv
    copy: copy/gamma.md
    priority: 3
unsubscribe:
  mode: append
""",
        encoding="utf-8",
    )
    cfg = load_launch(d)
    run = create_launch(cfg, FakeClient())
    assert run.status == "PASS"
    by_slug = {s.slug: s for s in run.segments}
    assert by_slug["alpha"].leads_uploaded == 2  # a + shared
    assert by_slug["beta"].leads_uploaded == 1  # shared moved to alpha, only b
    assert by_slug["gamma"].leads_uploaded == 1
    assert launch_totals(run)["added"] == 4


def test_interrupt_then_resume_no_duplicate_campaign_or_leads(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    d = make_launch(tmp_path)
    rows = ["email,first_name,company_name,industry"]
    for i in range(1200):
        rows.append(f"bulk{i}@example.com,First{i},Company {i},SaaS")
    (d / "leads" / "seg.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    cfg = load_launch(d)
    client = FakeClient()
    with pytest.raises(Interrupted):
        create_launch(cfg, client, interrupt_after_batches=1)
    assert len(state.load()["created"]) == 1
    cid = next(iter(state.load()["created"]))
    assert client.count_leads(campaign_id=cid) == 1000

    run = create_launch(cfg, client)
    assert run.status == "PASS"
    seg = run.segments[0]
    assert seg.campaign_id == cid  # resumed, not duplicated
    assert seg.resumed is True
    assert seg.leads_uploaded == 200
    assert seg.leads_skipped == 1000
    assert client.count_leads(campaign_id=cid) == 1200
    assert launch_totals(run)["added"] == 1200
