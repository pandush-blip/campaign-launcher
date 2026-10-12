import json
import re

import httpx

from tri_launch.draft import build_context, draft, draft_all

LISTS = [{"value": "Alpha List", "leads": 80}, {"value": "Beta List", "leads": 40}]


def test_skips_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = draft("write copy", LISTS)
    assert result.status == "skipped"
    assert "DEEPSEEK_API_KEY" in (result.error or "")


def test_context_lists_every_campaign_and_mapping():
    ctx = build_context(
        LISTS,
        columns=["email", "first_name", "company_name", "industry"],
        client="Acme",
        follow_ups=True,
        follow_up_steps=2,
        variants=2,
    )
    assert "Alpha List" in ctx and "80 leads" in ctx
    assert "first_name -> {{firstName}}" in ctx
    assert "Requested follow-up steps: 2" in ctx


def test_context_lets_the_instruction_decide_variants():
    ctx = build_context(LISTS, columns=["email"], client="Acme", variants=0)
    assert "decide per list" in ctx


def test_ok_parses_structured_campaigns():
    payload = {
        "reply": "Drafted both lists.",
        "campaigns": {
            "Alpha List": {
                "subject": "quick question",
                "step1_variants": ["Hi {{firstName | there}}, one.", "Hi {{firstName | there}}, two."],
                "followups": [{"delay_days": 3, "variants": ["Bumping this."]}],
            },
            "beta list": {"subject": "hi", "step1_variants": ["Only one."]},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    result = draft(
        "write copy", LISTS, api_key="dummy", transport=httpx.MockTransport(handler)
    )
    assert result.status == "ok"
    assert set(result.campaigns) == {"Alpha List", "Beta List"}  # case-corrected
    alpha = result.campaigns["Alpha List"]
    assert len(alpha["step1_variants"]) == 2
    assert alpha["followups"][0]["delay_days"] == 3


def test_errors_are_captured():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    result = draft("x", LISTS, api_key="dummy", transport=httpx.MockTransport(handler))
    assert result.status == "error"


def test_empty_instruction_rejected():
    result = draft("  ", LISTS, api_key="dummy")
    assert result.status == "error"


def test_max_tokens_is_set_and_truncated_json_is_salvaged():
    truncated = (
        '{"reply": "working", "campaigns": {'
        '"Alpha List": {"subject": "s", "step1_variants": ["first body"]}, '
        '"Beta List": {"subject": "s", "step1_variants": ["second bo'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["max_tokens"] == 8192
        return httpx.Response(
            200, json={"choices": [{"message": {"content": truncated}}]}
        )

    result = draft("write copy", LISTS, api_key="dummy", transport=httpx.MockTransport(handler))
    assert result.status == "ok"
    assert list(result.campaigns) == ["Alpha List"]  # the complete one was salvaged
    assert "cut off" in result.reply


def test_draft_all_retries_missing_lists_from_a_batch():
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        context = body["messages"][1]["content"]
        values = re.findall(r"- '([^']+)' -", context)
        calls.append(values)
        if len(values) > 1:
            content = (
                '{"reply": "cut", "campaigns": {'
                f'"{values[0]}": {{"subject": "s", "step1_variants": ["body one"]}}, '
                '"trunc'
            )
        else:
            content = json.dumps(
                {
                    "reply": "ok",
                    "campaigns": {
                        values[0]: {"subject": "s", "step1_variants": ["body two"]}
                    },
                }
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    # ~4k chars -> batch size 2, and the first batch reply is cut off.
    instruction = "describe the copy. " * 200
    result = draft_all(
        instruction, LISTS, api_key="dummy", transport=httpx.MockTransport(handler)
    )
    assert result.status == "ok", result.error
    assert set(result.campaigns) == {"Alpha List", "Beta List"}
    assert any(len(call) == 2 for call in calls)  # batched first
    assert any(len(call) == 1 for call in calls)  # missing list retried alone
