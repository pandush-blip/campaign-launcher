from pathlib import Path

from tri_launch.config import SegmentConfig
from tri_launch.leads import build_leads, is_valid_email, normalize_email


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_email_normalization_and_validation():
    assert normalize_email("  Foo@Example.COM ") == "foo@example.com"
    assert is_valid_email("foo@example.com")
    assert not is_valid_email("not-an-email")
    assert not is_valid_email("a@b")


def test_per_file_mode_dedupe_and_mapping(tmp_path):
    _write(
        tmp_path / "leads" / "one.csv",
        "email,first_name,company_name,Industry\na@x.com,Ada,Acme,SaaS\n"
        "A@X.com,Ada,Acme,SaaS\nbad-email,Zed,Zeta,SaaS\nb@x.com,Bob,Beta,AI\n",
    )
    _write(
        tmp_path / "leads" / "two.csv",
        "email,first_name\nb@x.com,Bob\nc@x.com,Cleo\n",
    )
    segs = [
        SegmentConfig(slug="one", name="One", leads="leads/one.csv", copy="copy/one.md", priority=1),
        SegmentConfig(slug="two", name="Two", leads="leads/two.csv", copy="copy/two.md", priority=2),
    ]
    build = build_leads(segments=segs, lead_mode="per_file", base_dir=tmp_path)
    one = build.segments["one"]
    assert one.report.rows_in == 4
    assert one.report.invalid == 1
    assert one.report.duplicates == 1
    assert one.report.rows_out == 2
    lead = one.leads[0]
    assert lead["first_name"] == "Ada"
    assert lead["company_name"] == "Acme"
    assert lead["custom_variables"]["Industry"] == "SaaS"
    assert lead["custom_variables"]["list_name"] == "One"
    assert one.values[0]["firstName"] == "Ada"
    # b@x.com lives in both segments; priority 1 (segment one) keeps it
    two = build.segments["two"]
    assert two.report.rows_out == 1
    assert two.leads[0]["email"] == "c@x.com"
    assert any(r.reason == "cross-segment-dedupe" and r.email == "b@x.com" for r in build.removals)


def test_split_column_mode_and_unassigned(tmp_path):
    _write(
        tmp_path / "leads" / "all_leads.csv",
        "email,segment\nx@a.com,alpha\ny@a.com,beta\nz@a.com,gamma\nw@a.com,\n",
    )
    _write(tmp_path / "copy" / "alpha.md", "# Step 1\n## Variant A\nSubject: s\nBody: b\n")
    segs = [
        SegmentConfig(slug="alpha", name="Alpha", copy="copy/alpha.md", priority=1),
        SegmentConfig(slug="beta", name="Beta", copy="copy/beta.md", priority=2),
    ]
    build = build_leads(
        segments=segs, lead_mode="split_column", split_column="segment", base_dir=tmp_path
    )
    assert build.segments["alpha"].report.rows_out == 1
    assert build.segments["beta"].report.rows_out == 1
    assert [r["email"] for r in build.unassigned] == ["z@a.com", "w@a.com"]


def test_human_csv_headers_map_to_instantly_fields(tmp_path):
    """Real client files use 'First Name' / 'Company' / 'Job Title', not snake_case."""
    _write(
        tmp_path / "leads" / "one.csv",
        "Email Address,First Name,Last Name,Company,Job Title,Company Domain,Industry\n"
        "ada@x.com,Ada,Lovelace,Acme Corp,CTO,acme.com,SaaS\n",
    )
    segs = [
        SegmentConfig(slug="one", name="One", leads="leads/one.csv", copy="copy/one.md")
    ]
    build = build_leads(segments=segs, lead_mode="per_file", base_dir=tmp_path)
    lead = build.segments["one"].leads[0]
    assert lead["email"] == "ada@x.com"
    assert lead["first_name"] == "Ada"
    assert lead["last_name"] == "Lovelace"
    assert lead["company_name"] == "Acme Corp"
    assert lead["job_title"] == "CTO"
    assert lead["website"] == "acme.com"
    assert lead["custom_variables"]["Industry"] == "SaaS"
    # human headers stay usable in copy as well ({{First Name}})
    assert lead["custom_variables"]["First Name"] == "Ada"
    assert "first_name" not in lead["custom_variables"]
    values = build.segments["one"].values[0]
    assert values["firstName"] == "Ada"
    assert values["companyName"] == "Acme Corp"
    assert values["jobTitle"] == "CTO"
    assert values["website"] == "acme.com"
    assert values["First Name"] == "Ada"


def test_full_name_derives_first_and_last(tmp_path):
    _write(
        tmp_path / "leads" / "one.csv",
        "email,Full Name,Company\nmary@x.com,Mary Jane Smith,Acme\n",
    )
    segs = [
        SegmentConfig(slug="one", name="One", leads="leads/one.csv", copy="copy/one.md")
    ]
    build = build_leads(segments=segs, lead_mode="per_file", base_dir=tmp_path)
    lead = build.segments["one"].leads[0]
    assert lead["first_name"] == "Mary"
    assert lead["last_name"] == "Smith"
    assert build.segments["one"].values[0]["firstName"] == "Mary"
