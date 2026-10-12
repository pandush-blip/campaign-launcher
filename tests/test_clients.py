import pytest

from tri_launch.clients import (
    KNOWN_CLIENTS,
    client_key,
    client_summaries,
    configured_clients,
    resolve_client,
)
from tri_launch.envload import ConfigError


def _clear(monkeypatch):
    for client in KNOWN_CLIENTS:
        for env in (client.env_var, *client.fallback_env):
            monkeypatch.delenv(env, raising=False)


def test_default_client_uses_legacy_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("INSTANTLY_API_KEY", "legacy-key")
    client, key = client_key(None)
    assert client.name == "The Revenue Inbox"
    assert key == "legacy-key"


def test_resolve_by_env_var_and_name(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("INSTANTLY_KEY_FABRIKAM", "fabrikam-key")
    assert resolve_client("INSTANTLY_KEY_FABRIKAM").name == "Fabrikam"
    assert resolve_client("fabrikam").name == "Fabrikam"
    assert resolve_client("Fabrikam").key == "fabrikam-key"


def test_missing_key_raises(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(ConfigError):
        client_key("Initech")


def test_unknown_client_raises(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(ConfigError):
        resolve_client("Nope Inc")


def test_summaries_never_contain_keys(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("INSTANTLY_KEY_GLOBEX", "super-secret")
    monkeypatch.setenv("INSTANTLY_KEY_ACME_2", "extra-secret")
    summaries = client_summaries()
    blob = repr(summaries)
    assert "super-secret" not in blob and "extra-secret" not in blob
    names = [s["name"] for s in summaries]
    assert "Globex" in names
    assert "Acme 2" in names  # extra INSTANTLY_KEY_* vars show up automatically
    assert [c.name for c in configured_clients()] == ["Globex", "Acme 2"]


def test_all_expected_clients_are_registered():
    names = [c.name for c in KNOWN_CLIENTS]
    assert names == [
        "The Revenue Inbox",
        "Acme",
        "Northwind",
        "Contoso",
        "Fabrikam",
        "Globex",
        "Initech",
    ]
