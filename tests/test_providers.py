"""Offline unit tests for pilot_workers.providers."""

import json
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


# ----------------------------------------------------------------------
# v0.5.0 (design D1): optional flat metadata fields strengths /
# suitable_modes / notes, surfaced by `pilot-workers status`.
# ----------------------------------------------------------------------


def test_provider_metadata_fields_default_to_empty():
    p = Provider(
        key="x",
        provider_id="acme",
        model_id="m1",
        base_url="https://example.invalid",
        display_name="X",
        context_tokens=1,
        output_tokens=1,
    )
    assert p.strengths == ""
    assert p.suitable_modes == ""
    assert p.notes == ""


METADATA_YAML = (
    "strengths: long context, strong rewrite-scale diffs\n"
    "suitable_modes: code, review\n"
    "notes: subscription tier required\n"
)


def test_load_providers_metadata_fields(tmp_path):
    text = VALID_PROVIDER_YAML + METADATA_YAML
    (tmp_path / "testp.yaml").write_text(text, encoding="utf-8")
    p = load_providers(tmp_path)["testp"]
    assert p.strengths == "long context, strong rewrite-scale diffs"
    assert p.suitable_modes == "code, review"
    assert p.notes == "subscription tier required"


def test_load_providers_metadata_absent_defaults_to_empty(tmp_path):
    (tmp_path / "testp.yaml").write_text(VALID_PROVIDER_YAML, encoding="utf-8")
    p = load_providers(tmp_path)["testp"]
    assert p.strengths == ""
    assert p.suitable_modes == ""
    assert p.notes == ""


def test_load_providers_metadata_flat_fallback(tmp_path, monkeypatch):
    # The stdlib fallback parser must keep the new fields flat key:value.
    monkeypatch.setattr(providers, "yaml", None)
    text = VALID_PROVIDER_YAML + METADATA_YAML
    (tmp_path / "testp.yaml").write_text(text, encoding="utf-8")
    p = load_providers(tmp_path)["testp"]
    assert p.strengths == "long context, strong rewrite-scale diffs"
    assert p.suitable_modes == "code, review"
    assert p.notes == "subscription tier required"


def test_bundled_providers_have_strengths_and_suitable_modes():
    # Enumerate every bundled YAML at test time.
    yaml_files = sorted(providers.PROVIDERS_DIR.glob("*.yaml"))
    assert yaml_files, "no bundled provider YAML files found"
    for path in yaml_files:
        data = _parse_yaml(path)
        assert str(data.get("strengths", "")).strip(), (
            f"{path.name} missing non-empty strengths"
        )
        assert str(data.get("suitable_modes", "")).strip(), (
            f"{path.name} missing non-empty suitable_modes"
        )
    for key, p in load_providers().items():
        assert p.strengths, f"provider {key} has empty strengths"
        assert p.suitable_modes, f"provider {key} has empty suitable_modes"


def test_status_json_provider_entries_include_metadata(tmp_path, monkeypatch, capsys):
    from pilot_workers.cli import status as status_mod

    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["providers"], "status --json reported no providers"
    for key, entry in data["providers"].items():
        for field in ("strengths", "suitable_modes", "notes"):
            assert field in entry, (
                f"status --json provider {key!r} missing field {field!r}"
            )
            assert isinstance(entry[field], str)
