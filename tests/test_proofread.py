import json

import httpx

from tri_launch.proofread import ProofIssue, _parse, proofread

SAMPLE = [{"segment": "s", "step": 1, "variant": "Variant A", "sample": "spin 1", "text": "Hi Ada,"}]


def test_skipped_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = proofread(SAMPLE)
    assert result.status == "skipped"
    assert "DEEPSEEK_API_KEY" in (result.error or "")


def test_ok_with_mocked_response():
    payload = {
        "issues": [
            {
                "step": 1,
                "variant": "Variant A",
                "sample": "spin 1",
                "quote": "Hi , there",
                "problem": "empty greeting",
                "suggestion": "use the firstName fallback",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    result = proofread(SAMPLE, api_key="dummy", transport=httpx.MockTransport(handler))
    assert result.status == "ok"
    assert result.issues == [
        ProofIssue(
            step=1,
            variant="Variant A",
            sample="spin 1",
            quote="Hi , there",
            problem="empty greeting",
            suggestion="use the firstName fallback",
        )
    ]


def test_error_is_captured_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    result = proofread(SAMPLE, api_key="dummy", transport=httpx.MockTransport(handler))
    assert result.status == "error"
    assert "HTTPStatusError" in (result.error or "")


def test_parse_handles_fenced_json():
    issues = _parse('```json\n{"issues": [{"variant": "A", "quote": "x"}]}\n```')
    assert issues and issues[0].quote == "x"
