from pathlib import Path

from tri_launch.config import load_launch
from tri_launch.qa import BLOCK, PASS, analyze_launch

GOOD_COPY = """# Step 1
delay: 0

## Variant A
Subject: Quick question
Body:
Hi {{firstName | there}},

Quick question about {{companyName | your company}}. Worth a 15 minute chat?

Not interested? Just reply "no thanks."
"""


def make_launch(tmp_path: Path, copy_text: str = GOOD_COPY, unsub_mode: str = "append") -> Path:
    d = tmp_path / "launch"
    (d / "copy").mkdir(parents=True)
    (d / "leads").mkdir(parents=True)
    (d / "copy" / "seg.md").write_text(copy_text, encoding="utf-8")
    rows = ["email,first_name,company_name"]
    for i in range(10):
        rows.append(f"lead{i}@example.com,First{i},Company {i}")
    (d / "leads" / "seg.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (d / "launch.yaml").write_text(
        f"""client: Test
settings_campaign: "Test | SETTINGS"
naming: "{{client}} | {{segment}} | {{date}}"
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
  ban_exclamation: false
  max_body_words: 120
  max_links: 1
unsubscribe:
  mode: {unsub_mode}
""",
        encoding="utf-8",
    )
    return d


def test_good_launch_passes(tmp_path):
    cfg = load_launch(make_launch(tmp_path))
    qa = analyze_launch(cfg)
    assert qa.status == PASS
    assert qa.segments[0].leads_count == 10
    assert qa.segments[0].variants[0].unsubscribe_present


def test_unknown_variable_blocks(tmp_path):
    copy = GOOD_COPY.replace("{{companyName | your company}}", "{{totallyUnknown}}")
    cfg = load_launch(make_launch(tmp_path, copy))
    qa = analyze_launch(cfg)
    assert qa.status == BLOCK
    assert any(i.kind == "unknown-variable" for i in qa.segments[0].variants[0].issues)


def test_broken_spintax_blocks(tmp_path):
    copy = GOOD_COPY.replace("{{RANDOM | Hi | Hey}}", "")
    copy = copy.replace("Hi {{firstName | there}},", "Hi {{firstName | there,")
    cfg = load_launch(make_launch(tmp_path, copy))
    qa = analyze_launch(cfg)
    assert qa.status == BLOCK
    assert any(i.kind == "spintax" for i in qa.segments[0].variants[0].issues)


def test_require_mode_blocks_missing_unsubscribe(tmp_path):
    copy = GOOD_COPY.replace('Not interested? Just reply "no thanks."', "Talk soon.")
    cfg = load_launch(make_launch(tmp_path, copy, unsub_mode="require"))
    qa = analyze_launch(cfg)
    assert qa.status == BLOCK
    assert any(i.kind == "unsubscribe" for i in qa.segments[0].variants[0].issues)


def test_append_mode_flags_missing_unsubscribe_without_warning(tmp_path):
    copy = GOOD_COPY.replace('Not interested? Just reply "no thanks."', "Talk soon.")
    cfg = load_launch(make_launch(tmp_path, copy, unsub_mode="append"))
    qa = analyze_launch(cfg)
    # The default block is appended at create time; QA surfaces it in the report's
    # "Unsub" column instead of downgrading the whole segment to WARN.
    assert qa.status == PASS
    assert qa.segments[0].variants[0].unsubscribe_present is False
    assert not [i for i in qa.segments[0].variants[0].issues if i.kind == "unsubscribe"]


def test_empty_copy_file_blocks(tmp_path):
    cfg = load_launch(make_launch(tmp_path, ""))
    qa = analyze_launch(cfg)
    assert qa.status == BLOCK
    assert any("no steps" in issue or "no email variants" in issue
               for issue in qa.segments[0].copy_issues)


def test_step_without_variants_blocks(tmp_path):
    copy = GOOD_COPY + "\n# Step 2\ndelay: 3\n"
    cfg = load_launch(make_launch(tmp_path, copy))
    qa = analyze_launch(cfg)
    assert qa.status == BLOCK
    assert any("no variants" in issue for issue in qa.segments[0].copy_issues)
