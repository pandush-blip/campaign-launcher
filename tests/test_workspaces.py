import pytest

from tri_launch.envload import ConfigError
from tri_launch.workspaces import (
    list_workspaces,
    normalize_sequencer,
    workspace_key,
)


def test_normalize_sequencer():
    assert normalize_sequencer("outfalcon") == "outfalcon"
    assert normalize_sequencer("Falcon") == "outfalcon"
    assert normalize_sequencer("") == "instantly"
    with pytest.raises(ConfigError):
        normalize_sequencer("smartlead")


def test_outfalcon_workspaces_from_env(monkeypatch):
    monkeypatch.setenv("OUTFALCON_KEY_HOOLI", "mk_hooli")
    monkeypatch.setenv("OUTFALCON_KEY_VANDELAY", "mk_vandelay")
    monkeypatch.setenv("OUTFALCON_KEY_ACME_2", "mk_acme")
    workspaces = list_workspaces("outfalcon")
    names = [w["name"] for w in workspaces]
    assert "Hooli" in names and "Vandelay" in names and "Acme 2" in names
    blob = repr(workspaces)
    assert "mk_hooli" not in blob and "mk_vandelay" not in blob

    name, env_var, key = workspace_key("outfalcon", "Vandelay")
    assert (name, env_var, key) == ("Vandelay", "OUTFALCON_KEY_VANDELAY", "mk_vandelay")


def test_outfalcon_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OUTFALCON_KEY_HOOLI", raising=False)
    with pytest.raises(ConfigError):
        workspace_key("outfalcon", "Hooli")
