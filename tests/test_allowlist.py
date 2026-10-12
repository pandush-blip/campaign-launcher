import httpx
import pytest

from tri_launch.allowlist import AllowlistError, check_allowed
from tri_launch.instantly import InstantlyClient

UUID = "01a0fa07-03b1-73ee-8daa-106db74692ac"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v2/campaigns"),
        ("GET", f"/api/v2/campaigns/{UUID}"),
        ("POST", f"/api/v2/campaigns/{UUID}/duplicate"),
        ("PATCH", f"/api/v2/campaigns/{UUID}"),
        ("POST", f"/api/v2/campaigns/{UUID}/variables"),
        ("GET", "/api/v2/accounts"),
        ("GET", "/api/v2/block-lists-entries"),
        ("POST", "/api/v2/leads/add"),
        ("POST", "/api/v2/leads/list"),
        ("DELETE", "/api/v2/leads"),
        ("POST", f"/api/v2/campaigns/{UUID}/pause"),
        ("POST", f"/api/v2/campaigns/{UUID}/activate"),
        ("DELETE", f"/api/v2/campaigns/{UUID}"),
    ],
)
def test_allowed(method, path):
    assert check_allowed(method, path)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", f"/api/v2/campaigns/{UUID}/activate"),
        ("POST", f"/api/v2/campaigns/{UUID}/activate/now"),
        ("POST", "/api/v2/emails/test"),
        ("POST", "/api/v2/emails/reply"),
        ("POST", "/api/v2/accounts/warmup/enable"),
        ("POST", "/api/v2/leads"),
        ("GET", "/api/v2/campaigns/abc"),  # non-UUID id
        ("PATCH", "/api/v2/campaigns/abc"),
        ("GET", f"/api/v2/campaigns/{UUID}?with_sequences=true"),
    ],
)
def test_rejected(method, path):
    with pytest.raises(AllowlistError):
        check_allowed(method, path)


def test_forbidden_request_never_reaches_transport():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    client = InstantlyClient("dummy", transport=httpx.MockTransport(handler))
    with pytest.raises(AllowlistError):
        client.request("POST", "/api/v2/emails/test")
    assert calls == []
    client.close()


def test_activate_requires_a_tracked_campaign(tmp_path, monkeypatch):
    from tri_launch.allowlist import GuardError

    monkeypatch.setenv("TRI_LAUNCH_STATE_DIR", str(tmp_path / "state"))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    client = InstantlyClient("dummy", transport=httpx.MockTransport(handler))
    with pytest.raises(GuardError):
        client.activate_campaign(UUID)
    assert calls == []
    client.close()
