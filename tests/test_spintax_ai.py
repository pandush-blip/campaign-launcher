import json

import httpx
import pytest

from tri_launch.copy_parser import parse_copy, serialize_copy
from tri_launch.spintax_ai import spin_copy_file, validate_rewrite

ORIGINAL = """# Step 1
delay: 0

## Variant A
Subject: quick question about {{companyName}}
Body:
{{firstName | there}},

We book qualified meetings for teams like {{companyName | your company}}.

Worth a look?

Not interested? Just reply "no thanks."
"""

SPUN_BODY = (
    "{{firstName | there}},\n"
    "\n"
    "{{RANDOM | We book qualified meetings for teams like "
    "{{companyName | your company}}. | We fill the calendars of companies such as "
    "{{companyName | your company}}. | Our team books calls for firms like "
    "{{companyName | your company}}.}}\n"
    "\n"
    "{{RANDOM | Worth a look? | Open to a quick look?}}\n"
    "\n"
    "[[KEEP_UNSUB_1]]"
)


def _copy():
    return parse_copy(ORIGINAL)


def _handler(payload_for, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        system = body["messages"][0]["content"]
        if "meticulous cold-email copy editor" in system:
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps({"issues": []})}}]}
            )
        user = json.loads(body["messages"][1]["content"].split("\n\nPrevious")[0])
        calls.append(user)
        payload = payload_for(user)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    return handler


def test_skips_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = spin_copy_file(_copy())
    assert result.status == "skipped"


def test_applies_valid_spintax_and_keeps_unsub_verbatim():
    calls = []

    def payload_for(user):
        assert "[[KEEP_UNSUB_1]]" in user["variants"][0]["body"]
        return {
            "variants": [
                {
                    "step": 1,
                    "label": "Variant A",
                    "subject": "{{RANDOM | quick question about {{companyName}} | "
                               "one question about {{companyName}}}}",
                    "body": SPUN_BODY,
                }
            ]
        }

    copy_file = _copy()
    result = spin_copy_file(
        copy_file,
        api_key="dummy",
        transport=httpx.MockTransport(_handler(payload_for, calls)),
    )
    assert result.status == "ok", result.error
    body = copy_file.steps[0].variants[0].body
    assert "{{RANDOM |" in body
    assert 'Not interested? Just reply "no thanks."' in body
    assert "[[KEEP_UNSUB_1]]" not in body
    assert "{{companyName | your company}}" in body
    assert len(calls) == 1


def test_broken_spintax_is_retried():
    calls = []

    def payload_for(user):
        if len(calls) == 1:
            return {
                "variants": [
                    {"step": 1, "label": "Variant A", "subject": "quick question",
                     "body": "{{RANDOM | broken | options}"}
                ]
            }
        return {
            "variants": [
                {"step": 1, "label": "Variant A", "subject": "quick question",
                 "body": SPUN_BODY}
            ]
        }

    result = spin_copy_file(
        _copy(),
        api_key="dummy",
        transport=httpx.MockTransport(_handler(payload_for, calls)),
    )
    assert result.status == "ok", result.error
    assert result.attempts == 2
    assert len(calls) == 2


def test_duplicate_options_are_repaired_not_failed():
    calls = []
    padded = (
        "{{firstName | there}},\n"
        "\n"
        "{{RANDOM | We book meetings for teams like {{companyName | your company}}. | "
        "We book meetings for teams like {{companyName | your company}}. | "
        "We fill calendars for firms like {{companyName | your company}}.}}\n"
        "\n"
        "{{RANDOM | Worth a look? | Worth a look? | Open to a quick look?}}\n"
        "\n"
        "[[KEEP_UNSUB_1]]"
    )

    def payload_for(user):
        return {
            "variants": [
                {"step": 1, "label": "Variant A", "subject": "quick question",
                 "body": padded}
            ]
        }

    copy_file = _copy()
    result = spin_copy_file(
        copy_file,
        api_key="dummy",
        transport=httpx.MockTransport(_handler(payload_for, calls)),
    )
    assert result.status == "ok", result.error
    body = copy_file.steps[0].variants[0].body
    assert body.count("We book meetings for teams like") == 1
    assert "We fill calendars for firms like" in body
    assert any("repeated spin option" in note for note in result.notes)


def test_single_option_blocks_are_unwrapped_not_failed():
    calls = []
    single = (
        "{{firstName | there}},\n"
        "\n"
        "{{RANDOM | Only one phrasing here for {{companyName | your company}}.}}\n"
        "\n"
        "{{RANDOM | Worth a look? | Open to a quick look?}}\n"
        "\n"
        "[[KEEP_UNSUB_1]]"
    )

    def payload_for(user):
        return {
            "variants": [
                {"step": 1, "label": "Variant A", "subject": "quick question",
                 "body": single}
            ]
        }

    copy_file = _copy()
    result = spin_copy_file(
        copy_file,
        api_key="dummy",
        transport=httpx.MockTransport(_handler(payload_for, calls)),
    )
    assert result.status == "ok", result.error
    body = copy_file.steps[0].variants[0].body
    assert "{{RANDOM | Only one phrasing here" not in body
    assert "Only one phrasing here for {{companyName | your company}}." in body


def test_changed_variable_is_rejected():
    calls = []

    def payload_for(user):
        return {
            "variants": [
                {
                    "step": 1,
                    "label": "Variant A",
                    "subject": "quick question",
                    "body": SPUN_BODY.replace("{{companyName | your company}}", "{{companyName}}"),
                }
            ]
        }

    copy_file = _copy()
    original = copy_file.steps[0].variants[0].body
    result = spin_copy_file(
        copy_file,
        api_key="dummy",
        transport=httpx.MockTransport(_handler(payload_for, calls)),
    )
    assert result.status == "error"
    assert "variable" in result.error
    assert copy_file.steps[0].variants[0].body == original  # untouched on failure


def test_paragraph_structure_change_is_rejected():
    problems = validate_rewrite(
        "One.\n\nTwo.\n",
        "{{RANDOM | One. Two. | Two. One.}}",
        {},
        max_words=120,
    )
    assert any("paragraph" in p for p in problems)


def test_serialize_copy_round_trips():
    copy_file = _copy()
    copy_file.steps[0].variants[0].body = SPUN_BODY
    text = serialize_copy(copy_file)
    again = parse_copy(text)
    assert again.steps[0].variants[0].body == SPUN_BODY
    assert again.steps[0].variants[0].subject == "quick question about {{companyName}}"
    assert again.steps[0].delay_days == 0
