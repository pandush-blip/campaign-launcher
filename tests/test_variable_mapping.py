from pathlib import Path

from tri_launch.config import SegmentConfig
from tri_launch.leads import build_leads


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_explicit_variable_map_wins_over_auto_aliases(tmp_path):
    _write(
        tmp_path / "leads" / "one.csv",
        "email,first_name,Preferred Name,Company,category\n"
        "a@x.com,Auto,Manual,Acme Corp,fintech\n",
    )
    segments = [SegmentConfig(slug="one", name="One", leads="leads/one.csv",
                              copy="copy/one.md")]
    build = build_leads(
        segments=segments,
        lead_mode="per_file",
        base_dir=tmp_path,
        variable_map={"firstName": "Preferred Name", "companyName": "Company",
                      "category": "category"},
    )
    lead = build.segments["one"].leads[0]
    assert lead["first_name"] == "Manual"  # explicit mapping beats the alias column
    assert lead["company_name"] == "Acme Corp"
    assert lead["custom_variables"]["category"] == "fintech"
    assert lead["custom_variables"]["list_name"] == "One"
    values = build.segments["one"].values[0]
    assert values["firstName"] == "Manual"
    assert values["category"] == "fintech"


def test_custom_variable_can_come_from_a_differently_named_column(tmp_path):
    _write(
        tmp_path / "leads" / "one.csv",
        "email,Company Short Name\nb@x.com,BetaPay\n",
    )
    segments = [SegmentConfig(slug="one", name="One", leads="leads/one.csv",
                              copy="copy/one.md")]
    build = build_leads(
        segments=segments,
        lead_mode="per_file",
        base_dir=tmp_path,
        variable_map={"companyShort": "Company Short Name"},
    )
    lead = build.segments["one"].leads[0]
    assert lead["custom_variables"]["companyShort"] == "BetaPay"
    assert build.segments["one"].values[0]["companyShort"] == "BetaPay"
