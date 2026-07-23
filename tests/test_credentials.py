"""Credential path and payload via runner."""

from pilot_workers.providers import PROVIDERS
from pilot_workers.runners import get_runner


def test_credential_path_under_provider_root():
    runner = get_runner("opencode")
    for p in PROVIDERS.values():
        path = runner.credential_path(p)
        assert "opencode" in str(path) and "auth.json" in str(path)


def test_credential_payload_shape():
    runner = get_runner("opencode")
    p = PROVIDERS["glm"]
    payload = runner.credential_payload(p, "sk-test")
    assert payload == {p.provider_id: {"type": "api", "key": "sk-test"}}


def test_parse_credential_valid():
    runner = get_runner("opencode")
    p = PROVIDERS["glm"]
    payload = {p.provider_id: {"type": "api", "key": "sk-real"}}
    assert runner.parse_credential(p, payload) == "sk-real"


def test_parse_credential_missing_provider():
    import pytest
    runner = get_runner("opencode")
    p = PROVIDERS["glm"]
    with pytest.raises(RuntimeError):
        runner.parse_credential(p, {"wrong-id": {"type": "api", "key": "x"}})
