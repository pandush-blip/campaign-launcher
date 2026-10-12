from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tri_launch.web import app

CSV = "email,first_name,company_name,List Name\n" + "".join(
    f"w{i}@example.com,First{i},Company {i},{'Alpha List' if i < 3 else 'Beta List'}\n"
    for i in range(5)
)

COPY = """# Step 1
delay: 0

## Variant A
Subject: quick question
Body:
Hi {{firstName | there}},

Quick question about {{companyName}}.

Not interested? Just reply "no thanks."
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_HOME", str(tmp_path))
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/healthz").json()["ok"] is True


def test_upload_detects_list_column_and_rows(client):
    r = client.post(
        "/api/upload",
        files={"file": ("leads.csv", CSV.encode(), "text/csv")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 5
    assert body["default_column"] == "List Name"
    assert any(c["column"] == "List Name" and c["unique"] == 2 for c in body["candidates"])


def test_plan_creates_one_campaign_per_list(client):
    up = client.post(
        "/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}
    ).json()
    r = client.post(
        "/api/plan",
        json={
            "session_id": up["session_id"],
            "client": "Web Test",
            "settings_campaign": "Some Settings Campaign",
            "list_column": "List Name",
            "naming": "{client} | {segment} | {date}",
            "exclude": [],
            "min_leads": 1,
            "shared_copy": COPY,
            "copy_map": {},
            "unsubscribe_mode": "append",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"] in {"PASS", "WARN"}
    names = sorted(c["name"] for c in body["campaigns"])
    assert names[0].startswith("Web Test | Alpha List |")
    assert names[1].startswith("Web Test | Beta List |")
    assert sorted(c["leads"] for c in body["campaigns"]) == [2, 3]
    assert body["samples"]


def test_create_requires_confirmation(client):
    up = client.post(
        "/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}
    ).json()
    r = client.post("/api/create", json={"session_id": up["session_id"], "confirm": ""})
    assert r.status_code == 400


def test_upload_rejects_non_csv(client):
    r = client.post("/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_clients_endpoint_never_leaks_keys(client, monkeypatch):
    monkeypatch.setenv("INSTANTLY_KEY_FABRIKAM", "fabrikam-secret")
    r = client.get("/api/clients")
    assert r.status_code == 200
    body = r.json()
    assert "fabrikam-secret" not in r.text
    fabrikam = next(c for c in body if c["name"] == "Fabrikam")
    assert fabrikam["configured"] is True
    assert fabrikam["env_var"] == "INSTANTLY_KEY_FABRIKAM"


def test_sequencers_and_outfalcon_workspaces(client, monkeypatch):
    monkeypatch.setenv("OUTFALCON_KEY_HOOLI", "mk_hooli_secret")
    r = client.get("/api/sequencers")
    assert r.status_code == 200
    assert {s["id"] for s in r.json()} == {"instantly", "outfalcon"}

    r = client.get("/api/workspaces?sequencer=outfalcon")
    assert r.status_code == 200
    body = r.json()
    names = [w["name"] for w in body["workspaces"]]
    assert "Hooli" in names
    assert "mk_hooli_secret" not in r.text


def test_variables_endpoint_reports_column_stats(client):
    csv_text = (
        "email,First Name,category\n"
        "a@x.com,Ada,fintech\n"
        "b@x.com,,\n"
        "c@x.com,Cara,fintech\n"
    )
    up = client.post("/api/upload", files={"file": ("leads.csv", csv_text.encode(), "text/csv")}).json()
    r = client.get(f"/api/variables?session_id={up['session_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 3
    first_name = next(c for c in body["columns"] if c["column"] == "First Name")
    assert first_name["variable"] == "firstName"
    assert first_name["empty"] == 1 and first_name["empty_pct"] == 33.3
    category = next(c for c in body["columns"] if c["column"] == "category")
    assert category["unique"] == 1 and category["samples"] == ["fintech"]


def test_plan_stores_sequencer_and_variable_map(client, tmp_path):
    csv_text = "email,First Name,Company,List Name\n" + "".join(
        f"v{i}@x.com,Name{i},Company {i},Alpha List\n" for i in range(2)
    )
    up = client.post("/api/upload", files={"file": ("leads.csv", csv_text.encode(), "text/csv")}).json()
    r = client.post(
        "/api/plan",
        json={
            "session_id": up["session_id"],
            "client": "OutFalcon Demo",
            "sequencer": "outfalcon",
            "api_key_env": "OUTFALCON_KEY_HOOLI",
            "variable_map": {"firstName": "First Name", "companyName": "Company",
                             "category": "Company"},
            "settings_campaign": "Settings Campaign",
            "list_column": "List Name",
            "exclude": [],
            "min_leads": 1,
            "shared_copy": COPY,
            "copy_map": {},
            "unsubscribe_mode": "append",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sequencer"] == "outfalcon"
    assert body["variable_map"]["firstName"] == "First Name"

    from tri_launch.web import web_root

    launch_dir = web_root() / "state" / "web" / up["session_id"] / "launch"
    yaml_text = (launch_dir / "launch.yaml").read_text(encoding="utf-8")
    assert "provider: outfalcon" in yaml_text
    assert "firstName: First Name" in yaml_text


def test_lists_endpoint_returns_every_value_with_counts(client):
    up = client.post("/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}).json()
    r = client.get(f"/api/lists?session_id={up['session_id']}&column=List Name")
    assert r.status_code == 200
    body = r.json()
    assert body["unique"] == 2
    by_value = {v["value"]: v["leads"] for v in body["values"]}
    assert by_value == {"Beta List": 2, "Alpha List": 3}


STRUCTURED = {
    "session_id": "",
    "client": "Web Test",
    "api_key_env": "INSTANTLY_KEY_FABRIKAM",
    "settings_campaign": "Some Settings Campaign",
    "list_column": "List Name",
    "naming": "{client} | {segment} | {date}",
    "exclude": [],
    "min_leads": 1,
    "unsubscribe_mode": "append",
    "shared": {
        "value": "__shared__",
        "subject": "shared subject",
        "steps": [{"delay_days": 0, "variants": ["Hi {{firstName | there}},\n\nShared body."]}],
    },
    "campaigns": [
        {
            "value": "Alpha List",
            "include": True,
            "use_shared": False,
            "subject": "quick question",
            "steps": [
                {
                    "delay_days": 0,
                    "variants": [
                        "Hi {{firstName}},\n\nFirst variant about {{companyName}}.",
                        "Hi {{firstName}},\n\nSecond variant.",
                    ],
                },
                {"delay_days": 3, "variants": ["{{firstName}}, bumping this."]},
            ],
        },
        {"value": "Beta List", "include": True, "use_shared": True, "steps": []},
        {"value": "Gamma List", "include": True, "use_shared": True, "steps": []},
    ],
}


def test_plan_structured_campaigns_subject_and_followups(client):
    up = client.post("/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}).json()
    payload = dict(STRUCTURED)
    payload["session_id"] = up["session_id"]
    r = client.post("/api/plan", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client_env"] == "INSTANTLY_KEY_FABRIKAM"
    names = sorted(c["name"] for c in body["campaigns"])
    # Gamma List does not exist in the CSV, so it creates no segment.
    assert names[0].startswith("Web Test | Alpha List |")
    assert names[1].startswith("Web Test | Beta List |")
    alpha_fixes = " ".join(body["autofixes"].get("Alpha List", []))
    assert "firstName" in alpha_fixes and "fallback" in alpha_fixes
    assert "spintax" in alpha_fixes or "greeting" in alpha_fixes

    # Read the generated copy file straight from the session directory.
    from tri_launch.web import web_root
    alpha_md = (web_root() / "state" / "web" / up["session_id"] / "launch" / "copy" / "alpha-list.md")
    assert alpha_md.is_file()
    text = alpha_md.read_text(encoding="utf-8")
    # one subject per step-1 variant; follow-ups stay in the same thread (empty subject)
    assert text.count("Subject: quick question") == 2
    step2 = text.split("# Step 2")[1]
    assert "Subject: \n" in step2
    assert "{{firstName | there}}" in text


HUMAN_CSV = (
    "Email Address,First Name,Last Name,Company,Job Title,List Name,category,companyShort\n"
    "a1@example.com,Ada,One,AlphaFin,CTO,Fintech Founders 2-10,spatial software company,AlphaFin\n"
    "a2@example.com,Ben,Two,AlphaPay,COO,Fintech Founders 2-10,payments platform,AlphaPay\n"
)

HUMAN_COPY = (
    "# Step 1\n"
    "delay: 0\n"
    "\n"
    "## Variant A\n"
    "Subject: {{RANDOM | RE: | Re:}} {{firstName}}, quick question\n"
    "Body:\n"
    "{{firstName}},\n"
    "\n"
    "Curious how this works at {{category}} like {{companyShort}}.\n"
)


def test_human_headers_are_not_blocked(client):
    """Regression: 'First Name' must feed {{firstName}} so coverage does not block."""
    up = client.post(
        "/api/upload",
        files={"file": ("leads.csv", HUMAN_CSV.encode(), "text/csv")},
    ).json()
    r = client.post(
        "/api/plan",
        json={
            "session_id": up["session_id"],
            "client": "Contoso",
            "api_key_env": "INSTANTLY_KEY_CONTOSO",
            "settings_campaign": "Some Settings Campaign",
            "list_column": "List Name",
            "naming": "{client} | {segment} | {date}",
            "exclude": [],
            "min_leads": 1,
            "shared_copy": HUMAN_COPY,
            "copy_map": {},
            "unsubscribe_mode": "append",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"] == "PASS", body
    assert body["campaigns"][0]["qa"] == "PASS"
    assert body["campaigns"][0]["blocked_reasons"] == []


def test_plan_reports_block_reasons(client):
    up = client.post(
        "/api/upload",
        files={"file": ("leads.csv", HUMAN_CSV.encode(), "text/csv")},
    ).json()
    r = client.post(
        "/api/plan",
        json={
            "session_id": up["session_id"],
            "client": "Contoso",
            "settings_campaign": "Some Settings Campaign",
            "list_column": "List Name",
            "exclude": [],
            "min_leads": 1,
            "shared_copy": HUMAN_COPY.replace("{{category}}", "{{missingColumn}}"),
            "copy_map": {},
            "unsubscribe_mode": "append",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"] == "BLOCK"
    campaign = body["campaigns"][0]
    assert campaign["blocked"] == "QA BLOCK"
    assert any("missingColumn" in reason for reason in campaign["blocked_reasons"])


def test_refresh_requires_and_reuses_a_plan(client):
    up = client.post("/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}).json()
    session_id = up["session_id"]
    r = client.post("/api/refresh", json={"session_id": session_id})
    assert r.status_code == 400

    plan = client.post(
        "/api/plan",
        json={
            "session_id": session_id,
            "client": "Web Test",
            "settings_campaign": "Some Settings Campaign",
            "list_column": "List Name",
            "exclude": [],
            "min_leads": 1,
            "shared_copy": COPY,
            "copy_map": {},
            "unsubscribe_mode": "append",
        },
    ).json()
    refreshed = client.post("/api/refresh", json={"session_id": session_id})
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["overall"] == plan["overall"]
    assert [c["name"] for c in body["campaigns"]] == [c["name"] for c in plan["campaigns"]]
    assert body["campaigns"][0]["spintax"] == {"with_spin": 0, "variants": 1}


def test_spintax_requires_ai_key(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    up = client.post("/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}).json()
    r = client.post("/api/spintax", json={"session_id": up["session_id"]})
    assert r.status_code == 400
    assert "DEEPSEEK" in r.text


def test_spintax_job_rewrites_copy_and_refreshes_plan(client, monkeypatch):
    import time

    from tri_launch.spintax_ai import SpinOutcome

    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy")

    def fake_spin(copy_file, **kwargs):
        for step in copy_file.steps:
            for variant in step.variants:
                variant.body = "{{RANDOM | Hi | Hello}} " + variant.body.lstrip()
        return SpinOutcome(status="ok", notes=["spintax added"])

    monkeypatch.setattr("tri_launch.spintax_ai.spin_copy_file", fake_spin)

    up = client.post("/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}).json()
    session_id = up["session_id"]
    client.post(
        "/api/plan",
        json={
            "session_id": session_id,
            "client": "Web Test",
            "api_key_env": "INSTANTLY_KEY_FABRIKAM",
            "settings_campaign": "Some Settings Campaign",
            "list_column": "List Name",
            "exclude": [],
            "min_leads": 1,
            "shared_copy": COPY,
            "copy_map": {},
            "unsubscribe_mode": "append",
        },
    )
    r = client.post("/api/spintax", json={"session_id": session_id})
    assert r.status_code == 200
    state = {"status": "running"}
    for _ in range(100):
        state = client.get(f"/api/spintax/{session_id}").json()
        if state["status"] != "running":
            break
        time.sleep(0.05)
    assert state["status"] == "done", state
    assert state["done"][0]["status"] == "ok"

    body = client.post("/api/refresh", json={"session_id": session_id}).json()
    for campaign in body["campaigns"]:
        assert campaign["spintax"]["with_spin"] == campaign["spintax"]["variants"]
    assert body["samples"]  # rendered samples come from the spun copy


def test_launch_check_requires_created_drafts(client):
    up = client.post("/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}).json()
    r = client.post("/api/launch/check", json={"session_id": up["session_id"]})
    assert r.status_code == 400
    assert "create the drafts first" in r.text


def test_launch_flow_activates_tracked_drafts(client, monkeypatch, tmp_path):
    import time

    from tri_launch import state

    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("INSTANTLY_KEY_FABRIKAM", "dummy-key")

    calls = {"activated": [], "get": []}

    class FakeInstantly:
        def __init__(self, key, **kwargs):
            self.key = key

        def get_campaign(self, cid):
            calls["get"].append(cid)
            status = 1 if cid in calls["activated"] else 0
            return {"id": cid, "name": "Fabrikam | Smoke | 2026-10-06", "status": status,
                    "daily_limit": 120}

        def count_leads(self, *, campaign_id=None, **kwargs):
            return 7

        def list_accounts(self, limit=100):
            return [{"id": "acct-1"}, {"id": "acct-2"}]

        def activate_campaign(self, cid):
            calls["activated"].append(cid)

        def close(self):
            pass

    monkeypatch.setattr("tri_launch.instantly.InstantlyClient", FakeInstantly)

    up = client.post(
        "/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}
    ).json()
    session_id = up["session_id"]
    client.post(
        "/api/plan",
        json={
            "session_id": session_id,
            "client": "Fabrikam",
            "api_key_env": "INSTANTLY_KEY_FABRIKAM",
            "settings_campaign": "Some Settings Campaign",
            "list_column": "List Name",
            "exclude": [],
            "min_leads": 1,
            "shared_copy": COPY,
            "copy_map": {},
            "unsubscribe_mode": "append",
        },
    )
    launch_dir = tmp_path / "state" / "web" / session_id / "launch"
    cid = "01a0fa44-0000-4000-8000-000000000001"
    state.register_campaign(
        cid,
        "Fabrikam | Smoke | 2026-10-06",
        extra={"launch_path": str(launch_dir), "segment": "alpha-list", "client": "Fabrikam"},
    )

    check = client.post("/api/launch/check", json={"session_id": session_id})
    assert check.status_code == 200, check.text
    readiness = check.json()
    assert readiness["ready"] == 1 and readiness["leads"] == 7
    assert readiness["accounts"] == 2
    assert readiness["confirm_phrase"] == "LAUNCH 1"

    wrong = client.post(
        "/api/launch", json={"session_id": session_id, "confirm": "launch"}
    )
    assert wrong.status_code == 400
    assert "LAUNCH 1" in wrong.text

    started = client.post(
        "/api/launch", json={"session_id": session_id, "confirm": "LAUNCH 1"}
    )
    assert started.status_code == 200
    state_job = {"status": "running"}
    for _ in range(100):
        state_job = client.get(f"/api/launch/jobs/{session_id}").json()
        if state_job["status"] != "running":
            break
        time.sleep(0.05)
    assert state_job["status"] == "done", state_job
    assert calls["activated"] == [cid]
    assert state_job["activated"] == 1
    assert state_job["leads"] == 7


def test_plan_includes_copy_markdown_and_save_copy_endpoint(client):
    up = client.post("/api/upload", files={"file": ("leads.csv", CSV.encode(), "text/csv")}).json()
    session_id = up["session_id"]
    plan = client.post(
        "/api/plan",
        json={
            "session_id": session_id,
            "client": "Web Test",
            "settings_campaign": "Some Settings Campaign",
            "list_column": "List Name",
            "exclude": [],
            "min_leads": 1,
            "shared_copy": COPY,
            "copy_map": {},
            "unsubscribe_mode": "append",
        },
    ).json()
    assert plan["campaigns"][0]["copy_markdown"].startswith("# Step 1")

    edited = plan["campaigns"][0]["copy_markdown"].replace(
        "Quick question about",
        "{{RANDOM | Quick question about | One question about}}",
    )
    r = client.post(
        "/api/copy",
        json={"session_id": session_id, "slug": plan["campaigns"][0]["slug"], "markdown": edited},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    campaign = body["campaigns"][0]
    assert campaign["spintax"]["with_spin"] == campaign["spintax"]["variants"]
    assert "{{RANDOM | Quick question about | One question about}}" in campaign["copy_markdown"]

    broken = client.post(
        "/api/copy",
        json={"session_id": session_id, "slug": campaign["slug"], "markdown": "just some text"},
    )
    assert broken.status_code == 400
