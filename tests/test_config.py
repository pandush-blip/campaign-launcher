from datetime import date

import pytest

from tri_launch.config import (
    LaunchConfigError,
    SegmentConfig,
    load_launch,
    render_campaign_name,
    validate_launch,
)


def test_loads_template_launch_yaml():
    cfg = load_launch("templates")
    assert cfg.client == "Acme"
    assert cfg.segments[0].slug == "ops-leaders-11-50"
    assert cfg.lint.max_body_words == 120
    assert cfg.unsubscribe.mode == "append"
    # template references files that do not exist; schema itself must be fine
    assert validate_launch(cfg, check_files=False) == []


def test_missing_launch_yaml(tmp_path):
    with pytest.raises(LaunchConfigError):
        load_launch(tmp_path)


def test_naming_tokens_and_blank_removal():
    cfg = load_launch("templates")
    seg = SegmentConfig(
        slug="s",
        name="Ops Leaders",
        angle="",
        industry="SaaS",
        keyword="operations",
        employee_size="11-50",
    )
    cfg.naming = "{client} | {industry} | {keyword} | {employee_size} | {angle} | {date}"
    name = render_campaign_name(cfg, seg, on=date(2026, 10, 2))
    assert name == "Acme | SaaS | operations | 11-50 | 2026-10-02"


def test_unknown_naming_token_is_blocking():
    cfg = load_launch("templates")
    cfg.naming = "{client} | {nonsense}"
    issues = validate_launch(cfg, check_files=False)
    assert any("unknown tokens" in i for i in issues)
