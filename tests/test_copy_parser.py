import pytest

from tri_launch.copy_parser import CopyParseError, parse_copy

SPEC_EXAMPLE = """# Step 1
delay: 0

## Variant A
Subject: {{RANDOM | quick question | idea for {{companyName}}}}
Body:
{{RANDOM | Hi | Hey}} {{firstName | there}},

First paragraph...

{{sendingAccountFirstName}}

## Variant B
Subject: second subject
Body:
Hello B

# Step 2
delay: 3

## Variant A
Subject:
Body:
Follow up.
"""


def test_parses_spec_example():
    copy = parse_copy(SPEC_EXAMPLE)
    assert copy.issues == []
    assert [s.number for s in copy.steps] == [1, 2]
    assert copy.steps[0].delay_days == 0
    assert copy.steps[1].delay_days == 3
    assert len(copy.steps[0].variants) == 2
    a, b = copy.steps[0].variants
    assert a.label == "Variant A"
    assert a.subject == "{{RANDOM | quick question | idea for {{companyName}}}}"
    assert a.body.startswith("{{RANDOM | Hi | Hey}} {{firstName | there}},")
    assert "{{sendingAccountFirstName}}" in a.body
    assert b.subject == "second subject"
    follow = copy.steps[1].variants[0]
    assert follow.subject == ""
    assert follow.body == "Follow up."


def test_variant_before_step_is_an_error():
    with pytest.raises(CopyParseError):
        parse_copy("## Variant A\nSubject: x\nBody:\ny\n")


def test_empty_body_is_reported():
    copy = parse_copy("# Step 1\n## Variant A\nSubject: x\nBody:\n")
    assert any("empty body" in issue for issue in copy.issues)


def test_delay_defaults_to_zero():
    copy = parse_copy("# Step 1\n## Variant A\nSubject: x\nBody:\ny\n")
    assert copy.steps[0].delay_days == 0
