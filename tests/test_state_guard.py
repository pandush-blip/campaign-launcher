import json

import httpx
import pytest

from tri_launch import state
from tri_launch.allowlist import GuardError
from tri_launch.instantly import InstantlyClient

UUID = "01a0fa07-03b1-73ee-8daa-106db74692ac"


def _client(handler, key="dummy"):
    return InstantlyClient(key, transport=httpx.MockTransport(handler))


def test_patch_untracked_rejected_without_request(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    calls = []

    def handler(request):
        calls.append(request.url)  # pragma: no cover
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(GuardError):
        client.patch_campaign(UUID, {"sequences": []})
    assert calls == []
    client.close()


def test_patch_tracked_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    state.register_campaign(UUID, "Scratch")

    def handler(request):
        return httpx.Response(200, json={"id": UUID, "status": 0})

    client = _client(handler)
    out = client.patch_campaign(UUID, {"sequences": []})
    assert out["status"] == 0
    client.close()


def test_delete_requires_exact_name(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    state.register_campaign(UUID, "Scratch")
    calls = []

    def handler(request):
        calls.append(str(request.method))
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(GuardError):
        client.delete_campaign(UUID, confirm_name="Somebody else's campaign")
    assert calls == []
    client.close()


LEAD_ID = "01a0fa44-5b43-7963-9da8-951169b6da31"


def test_delete_leads_rejects_untracked_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    calls = []

    def handler(request):
        calls.append(request.url)  # pragma: no cover
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(GuardError):
        client.delete_leads(UUID, [LEAD_ID])
    assert calls == []
    client.close()


def test_delete_leads_rejects_unrecorded_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    state.register_campaign(UUID, "Scratch")
    calls = []

    def handler(request):
        calls.append(str(request.method))
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(GuardError):
        client.delete_leads(UUID, [LEAD_ID])
    assert calls == []
    client.close()


def test_delete_leads_phase0_pattern_verifies_before_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    state.register_campaign(UUID, "Scratch", purpose="phase0-scratch")
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST" and request.url.path == "/api/v2/leads/list":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": LEAD_ID, "campaign": UUID, "email": "real-client@acme.com"}
                    ]
                },
            )
        return httpx.Response(200, json={"deleted": 1})

    client = _client(handler)
    with pytest.raises(GuardError):
        client.delete_leads(UUID, [LEAD_ID], require_phase0_pattern=True)
    assert methods == ["POST"]  # readback only, delete never sent
    client.close()


def test_delete_leads_phase0_pattern_allows_verified_test_lead(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    state.register_campaign(UUID, "Scratch", purpose="phase0-scratch")
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST" and request.url.path == "/api/v2/leads/list":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": LEAD_ID,
                            "campaign": UUID,
                            "email": "phase0.lead1@example.com",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"deleted": 1})

    client = _client(handler)
    assert client.delete_leads(UUID, [LEAD_ID], require_phase0_pattern=True) == {"deleted": 1}
    assert methods == ["POST", "DELETE"]
    client.close()


def test_delete_leads_phase0_pattern_skips_already_gone_leads(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    state.register_campaign(UUID, "Scratch", purpose="phase0-scratch")
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"items": []})

    client = _client(handler)
    out = client.delete_leads(UUID, [LEAD_ID], require_phase0_pattern=True)
    assert out == {"count": 0, "note": "already deleted"}
    assert methods == ["POST"]  # readback only, no delete sent
    client.close()


def test_delete_leads_chunks_large_batches(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path))
    state.register_campaign(UUID, "Scratch")
    ids = [f"01a0fa44-0000-4000-8000-{i:012d}" for i in range(1500)]
    state.add_lead_ids(UUID, ids)
    deletes = []

    def handler(request):
        if request.method == "DELETE":
            body = json.loads(request.content.decode())
            deletes.append(len(body["ids"]))
            return httpx.Response(200, json={"count": len(body["ids"])})
        return httpx.Response(200, json={"items": []})

    client = _client(handler)
    out = client.delete_leads(UUID, ids)
    assert out == {"count": 1500}
    assert deletes == [100] * 15
    client.close()
