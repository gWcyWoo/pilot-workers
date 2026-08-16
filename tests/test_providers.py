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
# The data root moved out of the Codex CLI's home. $CODEX_HOME used to select
# it, which put every credential, log and run sandbox inside a directory
# belonging to a different tool.
# ----------------------------------------------------------------------


def test_the_default_root_is_this_tools_own_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("PILOT_WORKERS_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert pilot_home() == (tmp_path / ".pilot-workers").resolve()


def test_codex_home_no_longer_selects_the_root(monkeypatch, tmp_path):
    """It named the Codex CLI's home, not ours; honouring it is the bug."""
    monkeypatch.delenv("PILOT_WORKERS_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "somewhere-else"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert pilot_home() == (tmp_path / ".pilot-workers").resolve()


def test_an_install_still_under_the_old_root_is_told_how_to_move(
    monkeypatch, tmp_path
):
    """Answering with a fresh empty root would report every credential as
    missing and give no reason. Name the relocation instead."""
    monkeypatch.delenv("PILOT_WORKERS_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".codex" / "opencode-workers").mkdir(parents=True)
    (tmp_path / ".codex" / "worker-runtime").mkdir()

    message = providers.legacy_home_notice()
    assert message is not None
    assert ".pilot-workers" in message
    assert "mv opencode-workers worker-runtime" in message
    # The rest of ~/.codex is Codex's; the instruction must not sweep it up.
    assert "mv .codex" not in message
    assert "PILOT_WORKERS_HOME" in message
    # pilot_home itself must stay a pure path helper: it runs at import time,
    # so raising there would abort `import pilot_workers.cli.status` and bury
    # this message in a traceback. The built wheel failed exactly that way.
    assert pilot_home() == (tmp_path / ".pilot-workers").resolve()


def test_the_cli_refuses_before_running_a_subcommand(monkeypatch, tmp_path, capsys):
    from pilot_workers.cli import main as main_mod

    monkeypatch.delenv("PILOT_WORKERS_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".codex" / "opencode-workers").mkdir(parents=True)

    assert main_mod.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "mv opencode-workers" in err


def test_a_bare_codex_home_without_our_data_is_not_an_error(monkeypatch, tmp_path):
    """Plenty of machines have a Codex install and never ran pw9 against it."""
    monkeypatch.delenv("PILOT_WORKERS_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".codex" / "sessions").mkdir(parents=True)
    assert providers.legacy_home_notice() is None


def test_a_completed_move_stops_the_refusal(monkeypatch, tmp_path):
    monkeypatch.delenv("PILOT_WORKERS_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".codex" / "opencode-workers").mkdir(parents=True)
    (tmp_path / ".pilot-workers").mkdir()
    assert providers.legacy_home_notice() is None


def test_an_explicit_home_override_is_never_second_guessed(monkeypatch, tmp_path):
    """Someone who set the variable has already made the decision."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".codex" / "opencode-workers").mkdir(parents=True)
    assert providers.legacy_home_notice() is None


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


# ----------------------------------------------------------------------
# The no-pyyaml fallback must agree with pyyaml on the file shape this
# package tells an author to write.
#
# `data/providers/README.md` documents the seven required fields WITH a
# trailing `# comment` on every line. The fallback used to keep the comment
# inside the value, so `key:` became a provider key with a comment in it —
# and `providers.profile_root` uses that key verbatim as a directory name.
# Nothing raised. `pyyaml` is optional by design, so the two parsers reading
# the same file differently is the defect, not the fallback existing.
# ----------------------------------------------------------------------

DOCUMENTED_TEMPLATE = """\
key: myprov                        # used as --provider argument and directory name
provider_id: myprov-worker          # OpenCode provider ID (arbitrary, must be unique)
model_id: my-model-1                # model ID sent to the API
base_url: https://api.example.invalid/v1   # official API endpoint (HTTPS only, no relay)
display_name: My Model              # shown in logs and config
context_tokens: 128000              # max context window
output_tokens: 32000                # max output tokens
"""


@pytest.fixture
def no_pyyaml(monkeypatch):
    """Force the stdlib fallback path regardless of what is installed."""
    monkeypatch.setattr(providers, "yaml", None)


def test_the_fallback_reads_the_documented_template_like_pyyaml(tmp_path, no_pyyaml):
    (tmp_path / "myprov.yaml").write_text(DOCUMENTED_TEMPLATE, encoding="utf-8")
    p = load_providers(tmp_path)["myprov"]
    assert p.key == "myprov", "the inline comment landed in the provider key"
    assert p.provider_id == "myprov-worker"
    assert p.model_id == "my-model-1"
    assert p.base_url == "https://api.example.invalid/v1"
    assert p.display_name == "My Model"
    assert p.context_tokens == 128000
    assert p.output_tokens == 32000


def test_the_fallback_agrees_with_pyyaml_field_for_field(tmp_path, monkeypatch):
    """Parity, not a hand-written expectation: whichever parser runs, the
    provider a user gets from one file must be the same provider."""
    pytest.importorskip("yaml")
    (tmp_path / "myprov.yaml").write_text(DOCUMENTED_TEMPLATE, encoding="utf-8")
    with_yaml = load_providers(tmp_path)["myprov"]
    monkeypatch.setattr(providers, "yaml", None)
    without_yaml = load_providers(tmp_path)["myprov"]
    assert without_yaml == with_yaml


@pytest.mark.parametrize("line,expected", [
    # Ground truth taken from pyyaml itself, not from reading its docs.
    ("bare   # trailing", "bare"),
    ('"quoted" # trailing', "quoted"),
    ("'single' # trailing", "single"),
    ("model #1 for speed", "model"),      # whitespace before # => comment
    ("glm#5", "glm#5"),                   # no whitespace before # => literal
    ('a "b" c', 'a "b" c'),               # quote not at the start => literal
    ("it's fine", "it's fine"),
    ("plain", "plain"),
])
def test_fallback_scalars_match_pyyaml(tmp_path, no_pyyaml, line, expected):
    text = VALID_PROVIDER_YAML + f"notes: {line}\n"
    (tmp_path / "testp.yaml").write_text(text, encoding="utf-8")
    assert load_providers(tmp_path)["testp"].notes == expected


def test_a_quoted_number_with_a_comment_is_still_an_integer(tmp_path, no_pyyaml):
    """`context_tokens: 128000   # max context window` is an int to pyyaml. The
    comment has to come off BEFORE the numeric check, or the field is rejected."""
    text = VALID_PROVIDER_YAML.replace(
        "context_tokens: 100000", "context_tokens: 100000   # max context window")
    (tmp_path / "testp.yaml").write_text(text, encoding="utf-8")
    assert load_providers(tmp_path)["testp"].context_tokens == 100000


def test_the_fallback_refuses_an_unterminated_quote(tmp_path, no_pyyaml):
    """pyyaml raises a ScannerError here. Silently keeping `"myprov` as the key
    would be the same class of defect this whole block exists to prevent."""
    text = VALID_PROVIDER_YAML.replace("key: testp", 'key: "testp')
    (tmp_path / "testp.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(RuntimeError, match="unterminated quote"):
        load_providers(tmp_path)


def test_the_documented_template_is_the_shape_this_test_pins(tmp_path):
    """If README stops showing inline comments the tests above still pass, but
    they would no longer be pinning the documented path. Read the real file."""
    readme = (providers.PROVIDERS_DIR / "README.md").read_text(encoding="utf-8")
    documented = [line for line in readme.splitlines()
                  if line.startswith(("key:", "context_tokens:"))]
    assert documented, "README no longer documents the required fields"
    assert all("#" in line for line in documented), (
        "README example no longer uses inline comments; "
        "re-check what shape the fallback must tolerate")


def test_a_bom_saved_provider_file_loads_with_either_parser(tmp_path, monkeypatch):
    """pyyaml strips a leading BOM; the fallback did not, so the first key became
    "\ufeffkey" and the file failed as "missing fields: key"."""
    path = tmp_path / "bomprov.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + VALID_PROVIDER_YAML.encode("utf-8"))
    assert "testp" in load_providers(tmp_path)
    monkeypatch.setattr(providers, "yaml", None)
    assert "testp" in load_providers(tmp_path)


# ----------------------------------------------------------------------
# reasoning effort validation
# ----------------------------------------------------------------------

# An oauth shape: no base_url is required there, which keeps these cases
# about `effort` and nothing else.
_MINIMAL_OAUTH = {
    "key": "sub",
    "provider_id": "openai",
    "model_id": "gpt-5.6-sol",
    "display_name": "Subscription Worker",
    "context_tokens": 1050000,
    "output_tokens": 128000,
    "auth": "oauth",
    "auth_method": "ChatGPT Pro/Plus (headless)",
}


def test_effort_accepts_every_level_the_engine_knows(tmp_path):
    for level in providers.EFFORT_LEVELS:
        data = dict(_MINIMAL_OAUTH, effort=level)
        assert providers.provider_from_data(
            data, tmp_path / "p.yaml").effort == level


def test_effort_is_normalised_and_optional(tmp_path):
    assert providers.provider_from_data(
        dict(_MINIMAL_OAUTH, effort=" HIGH "), tmp_path / "p.yaml").effort == "high"
    assert providers.provider_from_data(
        dict(_MINIMAL_OAUTH), tmp_path / "p.yaml").effort == ""


def test_a_misspelled_effort_is_refused_not_forwarded(tmp_path):
    """A typo silently forwarded would be rejected by the model mid-run, one
    dispatch later — the load is where it is cheap to see."""
    with pytest.raises(RuntimeError, match="effort must be one of"):
        providers.provider_from_data(
            dict(_MINIMAL_OAUTH, effort="highest"), tmp_path / "p.yaml")
