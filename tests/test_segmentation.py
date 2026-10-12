from pathlib import Path

import pytest

from tri_launch.config import LaunchConfigError, load_launch, validate_launch
from tri_launch.leads import build_leads

SHARED_COPY = """# Step 1
delay: 0

## Variant A
Subject: quick question
Body:
Hi {{firstName | there}},

Quick question about {{companyName}} in {{industry}}.

Not interested? Just reply "no thanks."
"""


def make_auto_launch(tmp_path: Path, column: str = "List Name") -> Path:
    d = tmp_path / "launch"
    (d / "leads").mkdir(parents=True)
    (d / "copy").mkdir(parents=True)
    (d / "leads" / "all_leads.csv").write_text(
        "email,first_name,company_name,List Name,industry\n"
        "a1@x.com,A1,Acme One,Enterprise SaaS,SaaS\n"
        "a2@x.com,A2,Acme Two,Enterprise SaaS,SaaS\n"
        "a3@x.com,A3,Acme Three,Enterprise SaaS,SaaS\n"
        "b1@x.com,B1,Beta One,Fintech Founders,Fintech\n"
        "b2@x.com,B2,Beta Two,Fintech Founders,Fintech\n"
        "d1@x.com,D1,Dnd Co,DND LIST - DO NOT DISTURB,SaaS\n"
        "t1@x.com,T1,Tiny Co,Tiny List,SaaS\n",
        encoding="utf-8",
    )
    (d / "copy" / "default.md").write_text(SHARED_COPY, encoding="utf-8")
    (d / "copy" / "enterprise.md").write_text(SHARED_COPY, encoding="utf-8")
    (d / "launch.yaml").write_text(
        f"""client: Auto Demo
settings_campaign: "Clay Audiences, US, CA, UK, 30.09.2026"
naming: "{{client}} | {{segment}} | {{date}}"
segmentation:
  mode: list_name
  column: "{column}"
  source: leads/all_leads.csv
  shared_copy: copy/default.md
  copy_map:
    "Enterprise SaaS": copy/enterprise.md
  exclude:
    - "DND*"
  min_leads: 2
unsubscribe:
  mode: append
""",
        encoding="utf-8",
    )
    return d


def test_derives_one_segment_per_list_name(tmp_path):
    cfg = load_launch(make_auto_launch(tmp_path))
    assert [s.slug for s in cfg.segments] == ["enterprise-saas", "fintech-founders"]
    assert [s.name for s in cfg.segments] == ["Enterprise SaaS", "Fintech Founders"]
    assert cfg.segments[0].copy == "copy/enterprise.md"  # copy_map override
    assert cfg.segments[1].copy == "copy/default.md"  # shared copy
    assert [s.priority for s in cfg.segments] == [1, 2]  # biggest list first
    assert cfg.lead_mode == "split_column"
    assert cfg.split_column == "List Name"
    rep = cfg.segmentation_report
    assert rep["values"] == 4
    assert rep["excluded"] == ["DND LIST - DO NOT DISTURB"]
    assert rep["too_small"] == ["Tiny List"]
    assert validate_launch(cfg) == []


def test_excluded_and_small_lists_never_upload(tmp_path):
    cfg = load_launch(make_auto_launch(tmp_path))
    build = build_leads(
        segments=cfg.segments,
        lead_mode=cfg.lead_mode,
        split_column=cfg.split_column,
        dedupe_across_segments=cfg.dedupe_across_segments,
        base_dir=cfg.path,
    )
    assert build.segments["enterprise-saas"].report.rows_out == 3
    assert build.segments["fintech-founders"].report.rows_out == 2
    assert {r["email"] for r in build.unassigned} == {"d1@x.com", "t1@x.com"}


def test_column_match_is_case_insensitive(tmp_path):
    cfg = load_launch(make_auto_launch(tmp_path, column="list name"))
    assert cfg.split_column == "List Name"
    assert len(cfg.segments) == 2


def test_missing_column_raises(tmp_path):
    d = make_auto_launch(tmp_path)
    text = (d / "launch.yaml").read_text(encoding="utf-8").replace('column: "List Name"', 'column: "No Such Column"')
    (d / "launch.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(LaunchConfigError):
        load_launch(d)


def test_explicit_segments_win_over_segmentation_mode(tmp_path):
    d = make_auto_launch(tmp_path)
    text = (d / "launch.yaml").read_text(encoding="utf-8")
    text = text.replace(
        "unsubscribe:",
        """segments:
  - slug: manual
    name: Manual Segment
    leads: leads/all_leads.csv
    copy: copy/default.md
    priority: 1
unsubscribe:""",
    )
    (d / "launch.yaml").write_text(text, encoding="utf-8")
    cfg = load_launch(d)
    assert [s.slug for s in cfg.segments] == ["manual"]
