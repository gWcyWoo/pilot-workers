"""Tests for runtime.build_environment isolation guarantees."""

from pilot_workers import runtime, providers
from pilot_workers.runners import get_runner


def test_runner_env_cannot_override_safe_env_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    p = providers.PROVIDERS["glm"]
    poisoned = {"PATH": "/evil", "HOME": "/evil", "OPENCODE_CUSTOM": "ok"}
    env = runtime.build_environment(p, poisoned)
    assert env.get("PATH") != "/evil"
    assert env.get("HOME") != "/evil"
    assert env.get("OPENCODE_CUSTOM") == "ok"


def test_runner_env_cannot_override_xdg_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    p = providers.PROVIDERS["glm"]
    poisoned = {"XDG_CONFIG_HOME": "/evil", "XDG_DATA_HOME": "/evil"}
    env = runtime.build_environment(p, poisoned)
    assert env.get("XDG_CONFIG_HOME") != "/evil"
    assert env.get("XDG_DATA_HOME") != "/evil"


def test_runner_env_cannot_override_no_color_ci(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    p = providers.PROVIDERS["glm"]
    poisoned = {"NO_COLOR": "0", "CI": "false"}
    env = runtime.build_environment(p, poisoned)
    assert env.get("NO_COLOR") != "0"
    assert env.get("CI") != "false"


def test_opencode_runner_env_passes_through(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    r = get_runner("opencode")
    p = providers.PROVIDERS["glm"]
    config = r.build_config(p, "code")
    runner_env = r.runner_environment(p, config)
    env = runtime.build_environment(p, runner_env)
    assert env.get("OPENCODE_DISABLE_AUTOUPDATE") == "1"
    assert "OPENCODE_CONFIG_CONTENT" in env


def test_runner_env_cannot_override_xdg_state_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    p = providers.PROVIDERS["glm"]
    poisoned = {"XDG_STATE_HOME": "/evil", "XDG_CACHE_HOME": "/evil"}
    env = runtime.build_environment(p, poisoned)
    assert env.get("XDG_STATE_HOME") != "/evil"
    assert env.get("XDG_CACHE_HOME") != "/evil"


def test_runner_env_cannot_override_user_shell_tmpdir(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    monkeypatch.setenv("USER", "real")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("TMPDIR", "/real/tmp")
    p = providers.PROVIDERS["glm"]
    poisoned = {"USER": "evil", "SHELL": "/evil", "TMPDIR": "/evil"}
    env = runtime.build_environment(p, poisoned)
    assert env.get("USER") == "real"
    assert env.get("SHELL") == "/bin/zsh"
    assert env.get("TMPDIR") == "/real/tmp"
