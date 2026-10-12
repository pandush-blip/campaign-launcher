from pathlib import Path

from tri_launch import state
from tri_launch.config import load_launch
from tri_launch.outfalcon_create import create_launch_outfalcon

COPY = """# Step 1
delay: 0

## Variant A
Subject: {{RANDOM | quick question | one question}} about {{companyName}}
Body:
Hi {{firstName | there}},

Quick question about {{companyName}} for a {{category}} company.

# Step 2
delay: 3

## Variant A
Subject:
Body:
{{firstName | there}}, bumping this.
"""


class FakeOutFalcon:
    def __init__(self):
        self.campaigns = {
            "settings": {"id": "settings", "name": "Settings Campaign", "status": "draft",
                         "daily_limit": 500, "lead_count": 0},
        }
        self.sequences = {}
        self.lists = {}
        self.enrolled = {}
        self.imported = {}
        self._n = 0
        self.count_campaign_leads_calls = 0
        self.find_lead_list_calls = 0

    # -- campaigns
    def resolve_campaign(self, ref):
        return self.campaigns["settings"]

    def find_campaign_by_name(self, name):
        return [c for c in self.campaigns.values() if c["name"] == name]

    def duplicate_campaign(self, source_id, name, include_accounts=True):
        self._n += 1
        cid = f"camp-{self._n}"
        self.campaigns[cid] = {"id": cid, "name": name, "status": "draft",
                               "daily_limit": 500, "lead_count": 0}
        self.sequences[cid] = [
            {"id": f"old-step-{cid}", "variants": [{"id": f"old-var-{cid}"}]}
        ]
        return dict(self.campaigns[cid])

    def get_campaign(self, cid):
        return dict(self.campaigns[cid])

    def get_sequences(self, cid):
        return {"steps": [dict(step) for step in self.sequences[cid]]}

    def delete_step(self, step_id):
        for cid, steps in self.sequences.items():
            self.sequences[cid] = [s for s in steps if s["id"] != step_id]

    def add_step(self, cid, *, delay_days=1, delay_hours=0):
        self._n += 1
        step = {
            "id": f"step-{self._n}",
            "delay_days": delay_days,
            "variants": [{"id": f"var-{self._n}-a", "label": "A", "subject": "",
                          "body_html": "", "weight": 100}],
        }
        self.sequences[cid].append(step)
        return dict(step)

    def add_variant(self, step_id, label):
        self._n += 1
        variant = {"id": f"var-{self._n}", "label": label, "subject": "",
                   "body_html": "", "weight": 100}
        for steps in self.sequences.values():
            for step in steps:
                if step["id"] == step_id:
                    step["variants"].append(variant)
        return dict(variant)

    def patch_variant(self, variant_id, *, subject=None, body_html=None, weight=None):
        for steps in self.sequences.values():
            for step in steps:
                for variant in step["variants"]:
                    if variant["id"] == variant_id:
                        if subject is not None:
                            variant["subject"] = subject
                        if body_html is not None:
                            variant["body_html"] = body_html
                        if weight is not None:
                            variant["weight"] = weight
        return {}

    def delete_variant(self, variant_id):
        for steps in self.sequences.values():
            for step in steps:
                step["variants"] = [v for v in step["variants"] if v["id"] != variant_id]

    # -- leads
    def create_lead_list(self, name, description=""):
        self._n += 1
        list_id = f"list-{self._n}"
        self.lists[list_id] = {"id": list_id, "name": name}
        return dict(self.lists[list_id])

    def find_lead_list(self, name):
        self.find_lead_list_calls += 1
        for item in self.lists.values():
            if item["name"] == name:
                return dict(item)
        return None

    def import_csv(self, list_id, csv_text, mapping, async_job=False):
        rows = max(0, len(csv_text.strip().splitlines()) - 1)
        self.imported[list_id] = rows
        assert mapping.get("email") == "email"
        return {"imported": rows, "skipped": 0, "errors": []}

    def enroll_list(self, cid, list_id):
        count = self.imported.get(list_id, 0)
        self.enrolled[cid] = count
        self.campaigns[cid]["lead_count"] = count
        return {"added": count, "skipped": 0, "skippedOtherCampaigns": 0}

    def count_campaign_leads(self, cid):
        self.count_campaign_leads_calls += 1
        return self.campaigns[cid]["lead_count"]


def make_launch(tmp_path: Path) -> Path:
    d = tmp_path / "launch"
    (d / "copy").mkdir(parents=True)
    (d / "leads").mkdir(parents=True)
    (d / "copy" / "seg.md").write_text(COPY, encoding="utf-8")
    rows = ["email,First Name,Company,category"]
    for i in range(5):
        rows.append(f"lead{i}@example.com,First{i},Company {i},fintech")
    (d / "leads" / "all_leads.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (d / "launch.yaml").write_text(
        """
client: OutFalcon Demo
provider: outfalcon
settings_campaign: "Settings Campaign"
variable_map:
  firstName: "First Name"
  companyName: "Company"
  category: category
naming: "{client} | {segment} | {date}"
lead_mode: per_file
segments:
  - slug: seg
    name: Segment One
    leads: leads/all_leads.csv
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


def test_create_launch_outfalcon_duplicates_replaces_and_enrolls(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    cfg = load_launch(make_launch(tmp_path))
    assert cfg.provider == "outfalcon"
    client = FakeOutFalcon()

    run = create_launch_outfalcon(cfg, client)
    assert run.status == "PASS", [s.issues for s in run.segments]
    segment = run.segments[0]
    assert segment.leads_uploaded == 5
    assert segment.leads_in_campaign == 5

    cid = segment.campaign_id
    steps = client.sequences[cid]
    assert len(steps) == 2  # the inherited settings step was replaced
    first_variant = steps[0]["variants"][0]
    assert "{quick question|one question}" in first_variant["subject"]
    assert "{{company}}" in first_variant["subject"]
    assert "{{first_name}}" in first_variant["body_html"]
    assert "{{company}}" in first_variant["body_html"]
    assert steps[1]["variants"][0]["subject"] == ""

    record = state.load()["created"][cid]
    assert record["provider"] == "outfalcon"
    assert record["expected_lead_count"] == 5
    # Readback must use the campaign's lead_count, not paginate every lead.
    assert client.count_campaign_leads_calls == 0
    assert client.find_lead_list_calls == 1


def test_rerun_reuses_the_lead_list_and_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    cfg = load_launch(make_launch(tmp_path))
    client = FakeOutFalcon()
    first = create_launch_outfalcon(cfg, client)
    assert first.status == "PASS"
    lists_before = len(client.lists)

    again = create_launch_outfalcon(cfg, client)
    assert again.status == "PASS"
    assert again.segments[0].campaign_id == first.segments[0].campaign_id  # resumed
    assert len(client.lists) == lists_before  # no duplicate lead list
    # Leads are already enrolled, so the re-run only re-patches the sequence.
    assert client.find_lead_list_calls == 1
    assert again.segments[0].leads_uploaded == 0
    assert again.segments[0].leads_in_campaign == 5
