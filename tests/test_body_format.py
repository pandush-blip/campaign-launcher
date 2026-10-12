from tri_launch.body_format import (
    copy_to_sequences,
    div_html_to_plain,
    escape_text,
    text_to_div_html,
)
from tri_launch.copy_parser import parse_copy


def test_paragraphs_and_line_breaks():
    html_out = text_to_div_html("Hi {{firstName | there}},\n\nSecond line\nstill second")
    assert html_out == (
        "<div>Hi {{firstName | there}},</div><div><br /></div>"
        "<div>Second line<br />still second</div>"
    )


def test_escapes_text_but_keeps_entities():
    assert escape_text("a < b & c -> d") == "a &lt; b &amp; c -&gt; d"
    assert escape_text("A &amp; B") == "A &amp; B"


def test_roundtrip_plain():
    assert div_html_to_plain("<div>a</div><div><br /></div><div>b<br />c</div>") == "a\n\nb\nc"


def test_copy_to_sequences_uses_div_format_and_appends_unsubscribe():
    copy = parse_copy(
        "# Step 1\ndelay: 0\n\n## Variant A\nSubject: hi\nBody:\nHello {{firstName}}.\n"
    )
    payload, notices = copy_to_sequences(copy, unsubscribe_mode="append")
    steps = payload["sequences"][0]["steps"]
    assert steps[0]["delay_unit"] == "days"
    body = steps[0]["variants"][0]["body"]
    assert body.startswith("<div>Hello {{firstName}}.</div>")
    assert "no thanks" in body
    assert notices == [
        {"kind": "unsubscribe-appended", "step": 1, "variant": "Variant A"}
    ]


def test_copy_to_sequences_keeps_existing_unsubscribe():
    copy = parse_copy(
        "# Step 1\ndelay: 0\n\n## Variant A\nSubject: hi\nBody:\n"
        "Not interested? Reply \u201cno thanks.\u201d\n"
    )
    payload, notices = copy_to_sequences(copy, unsubscribe_mode="append")
    assert notices == []
    assert "Reply \u201cno thanks.\u201d" in payload["sequences"][0]["steps"][0]["variants"][0]["body"]
