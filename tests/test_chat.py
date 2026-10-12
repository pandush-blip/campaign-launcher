import json

import httpx

from tri_launch.chat import build_context, chat

PREVIEW = {
    "rows": 120,
    "columns": ["email", "first_name", "company_name", "List Name", "industry"],
    "candidates": [
        {"column": "List Name", "unique": 2, "top": [["Alpha", 80], ["Beta", 40]]}
    ],
}


def test_skips_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = chat("write me copy", PREVIEW)
    assert result.status == "skipped"
    assert "DEEPSEEK_API_KEY" in (result.error or "")


def test_context_mentions_columns_and_values():
    ctx = build_context(PREVIEW, client="Acme", naming="{client}")
    assert "List Name" in ctx and "Alpha (80)" in ctx and "Acme" in ctx


def test_ok_parses_reply_and_fields():
    payload = {
        "reply": "Drafted two steps for both lists.",
        "shared_copy": "# Step 1\ndelay: 0\n\n## Variant A\nSubject: hi\nBody:\nHi {{firstName | there}},\n",
        "exclude": ["DND*"],
        "min_leads": 2,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    result = chat(
        "write copy", PREVIEW, api_key="dummy", transport=httpx.MockTransport(handler)
    )
    assert result.status == "ok"
    assert result.reply.startswith("Drafted")
    assert result.fields["exclude"] == ["DND*"]
    assert result.fields["min_leads"] == 2
    assert result.fields["shared_copy"].startswith("# Step 1")


def test_errors_are_captured():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    result = chat("x", PREVIEW, api_key="dummy", transport=httpx.MockTransport(handler))
    assert result.status == "error"
