"""A second runner must be addable without editing the neutral layer.

CLAUDE.md claims `runtime.py` is runner-neutral and that engine specifics live
behind the `Runner` ABC. Three places quietly broke that: the per-run sandbox
spelled OpenCode's credential path itself, and `install runner <name>` /
`uninstall runner <name>` ignored their own `name` argument and operated on
OpenCode's directory whatever it said — so a second runner would have installed
and deleted the first one's runtime.

These tests define a fake runner and drive the real code paths with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pilot_workers import providers, runtime
from pilot_workers.cli import install as install_mod
from pilot_workers.cli import status as status_mod
from pilot_workers.runners import RUNNERS, get_runner
from pilot_workers.runners.base import Runner, UnifiedEvent


class FakeRunner(Runner):
    """The smallest runner that is not OpenCode."""

    name = "fake"

    def __init__(self, home: Path) -> None:
        self.home = home

    # --- credential seam -------------------------------------------------
    def credential_path(self, provider):
        return self.home / "canonical" / "fake-credentials.json"

    def credential_payload(self, provider, key):
        return {"key": key}

    def parse_credential(self, provider, payload):
        return payload["key"]

    def sandbox_credential_path(self, paths):
        # Deliberately unlike OpenCode's data/opencode/auth.json.
        return paths["data"] / "fake-engine" / "creds" / "token.json"

    # --- runtime seam ----------------------------------------------------
    def runtime_root(self):
        return self.home / "fake-runtime"

    @property
    def pinned_version(self):
        return "9.9.9"

    def install_script(self):
        return None  # nothing to install

    # --- unused-by-these-tests abstract methods --------------------------
    def build_config(self, provider, mode, *, permission_profile=None):
        return {}

    def build_command(self, binary, provider, mode, workdir, run_id, session):
        return [str(binary)]

    def runner_environment(self, provider, config, *, paths=None):
        return {}

    def format_task_input(self, task, mode):
        return task

    def parse_events(self, raw):
        return [] if not raw else [UnifiedEvent(kind="text", text="")]

    def resolve_binary(self):
        raise RuntimeError("fake runner has no binary")


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    runner = FakeRunner(tmp_path / "home")
    monkeypatch.setitem(RUNNERS, "fake", runner)
    return runner


def test_the_sandbox_credential_lands_where_the_runner_says(fake_runner, tmp_path):
    """provision_run_sandbox spelled `data/opencode/auth.json` itself, so a
    runner that reads its credential anywhere else got a useless symlink."""
    canonical = fake_runner.credential_path(None)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text('{"key": "fake"}', encoding="utf-8")
    provider = providers.PROVIDERS["glm"]

    paths = runtime.provision_run_sandbox(provider, "run-fake", fake_runner)
    try:
        link = fake_runner.sandbox_credential_path(paths)
        assert link.is_symlink(), "the runner's chosen path holds no symlink"
        assert Path(link.readlink()) == canonical
        assert not (paths["data"] / "opencode").exists(), (
            "the neutral layer still created OpenCode's directory")
    finally:
        runtime.release_run_lock(paths["root"])


def test_uninstalling_a_runner_does_not_touch_another_runners_runtime(
        fake_runner, tmp_path, capsys):
    """`uninstall runner fake` hardcoded OpenCode's directory: it would have
    deleted the wrong runtime and reported success."""
    opencode_root = get_runner("opencode").runtime_root()
    opencode_root.mkdir(parents=True, exist_ok=True)
    (opencode_root / "1.18.4").mkdir()
    fake_root = fake_runner.runtime_root()
    fake_root.mkdir(parents=True, exist_ok=True)
    (fake_root / "9.9.9").mkdir()

    assert install_mod._uninstall_runner("fake") == 0

    assert not fake_root.exists(), "the named runner's runtime survived"
    assert opencode_root.is_dir(), "another runner's runtime was deleted"


def test_installing_a_runner_with_no_script_says_so(fake_runner, capsys):
    """The install path ran OpenCode's shell script unconditionally."""
    assert install_mod._install_runner("fake") == 0
    assert "needs no runtime" in capsys.readouterr().out


def test_status_reports_each_runners_own_pin(fake_runner, capsys):
    """The pinned column was `PINNED_OPENCODE_VERSION if name == 'opencode'`,
    so any other runner's pin was reported as None."""
    data = status_mod._collect()
    assert data["runners"]["fake"]["pinned"] == "9.9.9"
    assert data["runners"]["opencode"]["pinned"] == get_runner(
        "opencode").pinned_version


def test_the_post_uninstall_report_names_the_installed_runner(
        fake_runner, tmp_path, capsys):
    """It said `uninstall runner opencode` whatever was installed."""
    fake_root = fake_runner.runtime_root()
    fake_root.mkdir(parents=True, exist_ok=True)

    install_mod._report_remaining_artifacts({})

    out = capsys.readouterr().out
    assert "uninstall runner fake" in out


def test_the_isolation_layer_builds_no_opencode_specific_path():
    """The seam only holds if the neutral layer never spells engine paths.

    Checks string LITERALS, not comments: a docstring may name OpenCode as the
    example it is describing, but a path built from the word is a dependency.
    `provision_run_sandbox` used to construct `data/opencode/auth.json` here.
    """
    import ast

    source = Path(runtime.__file__).read_text(encoding="utf-8")
    assert "opencode_runner" not in source, (
        "the isolation layer imports the OpenCode runner directly")

    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    offenders = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "opencode" in node.value.lower()
        and node.value not in docstrings
        # The provider tree's own directory name predates the runner layer and
        # is not an engine path.
        and "opencode-workers" not in node.value
    ]
    assert not offenders, f"engine-specific literals in runtime.py: {offenders}"


def test_the_runner_flag_guard_tests_explicitness_not_a_name(capsys):
    """`--runner` is reparse-only. The guard compared against the literal
    "opencode", so passing the default explicitly was accepted in dispatch mode
    and the check would silently invert if the default ever changed."""
    from pilot_workers.cli import dispatch as dispatch_mod

    rc = dispatch_mod.main([
        "--provider", "glm", "--mode", "review", "--workdir", ".",
        "--task", "x", "--runner", dispatch_mod.DEFAULT_RUNNER,
    ])
    assert rc == dispatch_mod.DISPATCH_ERROR_EXIT
    assert "--runner is only valid with --reparse" in capsys.readouterr().err


def test_reparse_still_defaults_the_runner(tmp_path, capsys):
    """Removing the argparse default must not make reparse runner-less."""
    import json

    from pilot_workers.cli import dispatch as dispatch_mod

    jsonl = tmp_path / "20260101T000000Z-abcd1234.jsonl"
    jsonl.write_text(json.dumps({"type": "text", "part": {"text": "hi"}}) + "\n",
                     encoding="utf-8")

    assert dispatch_mod.main(["--reparse", str(jsonl), "--mode", "review"]) == 0
    verdict = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert verdict["runner"] == dispatch_mod.DEFAULT_RUNNER
