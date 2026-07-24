"""Offline unit tests for pilot_workers.providers."""

from pathlib import Path

import pytest

from pilot_workers import providers
from pilot_workers.providers import PROVIDERS, Provider, _parse_yaml, load_providers, pilot_home


VALID_PROVIDER_YAML = """\
key: testp
provider_id: testp-worker
model_id: testp-1
base_url: https://example.invalid/v1
display_name: Test Provider
context_tokens: 100000
output_tokens: 8192
"""


def test_module_providers_contains_glm_kimi_k3_and_ds():
    assert "glm" in PROVIDERS
    assert "kimi-k3" in PROVIDERS
    assert "ds" in PROVIDERS


def test_provider_model_property_is_provider_id_slash_model_id():
    p = Provider(
        key="x",
        provider_id="acme",
        model_id="m1",
        base_url="https://example.invalid",
        display_name="X",
        context_tokens=1,
        output_tokens=1,
    )
    assert p.model == "acme/m1"


def test_provider_permissions_defaults_to_none():
    p = Provider(
        key="x",
        provider_id="acme",
        model_id="m1",
        base_url="https://example.invalid",
        display_name="X",
        context_tokens=1,
        output_tokens=1,
    )
    assert p.permissions is None


def test_load_providers_valid_yaml(tmp_path):
    (tmp_path / "testp.yaml").write_text(VALID_PROVIDER_YAML, encoding="utf-8")
    loaded = load_providers(tmp_path)
    assert "testp" in loaded
    p = loaded["testp"]
    assert isinstance(p.context_tokens, int)
    assert p.context_tokens == 100000
    assert isinstance(p.output_tokens, int)
    assert p.base_url == "https://example.invalid/v1"


def test_load_providers_missing_required_field_raises(tmp_path):
    text = VALID_PROVIDER_YAML.replace(
        "base_url: https://example.invalid/v1\n", ""
    )
    (tmp_path / "bad.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(RuntimeError, match="base_url"):
        load_providers(tmp_path)


def test_load_providers_duplicate_key_raises(tmp_path):
    (tmp_path / "a.yaml").write_text(VALID_PROVIDER_YAML, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(VALID_PROVIDER_YAML, encoding="utf-8")
    with pytest.raises(RuntimeError, match="duplicate"):
        load_providers(tmp_path)


def test_load_providers_reserved_key_raises(tmp_path):
    text = VALID_PROVIDER_YAML.replace("key: testp", "key: runner")
    (tmp_path / "bad.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(RuntimeError, match="reserved"):
        load_providers(tmp_path)


def test_asset_prefix_defaults_to_key(tmp_path):
    (tmp_path / "testp.yaml").write_text(VALID_PROVIDER_YAML, encoding="utf-8")
    assert load_providers(tmp_path)["testp"].asset_prefix == "testp"


def test_kimi_k3_asset_prefix_is_kimi():
    assert PROVIDERS["kimi-k3"].asset_prefix == "kimi"


def test_load_providers_empty_directory_raises(tmp_path):
    with pytest.raises(RuntimeError, match="no provider"):
        load_providers(tmp_path)


def test_load_providers_missing_directory_raises(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        load_providers(tmp_path / "nonexistent")


def test_load_providers_permissions_field(tmp_path):
    text = VALID_PROVIDER_YAML + "permissions: relaxed\n"
    (tmp_path / "testp.yaml").write_text(text, encoding="utf-8")
    loaded = load_providers(tmp_path)
    assert loaded["testp"].permissions == "relaxed"


def test_parse_yaml_flat_fallback_without_pyyaml(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "yaml", None)
    path = tmp_path / "flat.yaml"
    path.write_text(
        "# a comment\n"
        "\n"
        "key: flatp\n"
        "context_tokens: 123456\n"
        "base_url: https://example.invalid/v1\n",
        encoding="utf-8",
    )
    data = _parse_yaml(path)
    assert data["key"] == "flatp"
    assert data["context_tokens"] == 123456
    assert isinstance(data["context_tokens"], int)
    assert data["base_url"] == "https://example.invalid/v1"
    assert len(data) == 3


def test_pilot_home_respects_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert pilot_home() == tmp_path.resolve()
