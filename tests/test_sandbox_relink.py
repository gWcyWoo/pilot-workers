"""A sandbox's credential link must survive the data root moving.

Provisioning used to create the link only when it was absent, which skipped a
link that existed but named the OLD root. `status` reads the canonical
credential file and reported the provider configured, while the engine —
which reads the link — found nothing. That is the worst shape a failure can
take: the diagnostic and the thing being diagnosed disagree.
"""

from __future__ import annotations

import json
import os

from pilot_workers import providers, runtime
from pilot_workers.runners import get_runner


def _provision(provider_key: str, run_id: str):
    provider = providers.PROVIDERS[provider_key]
    runner = get_runner(provider.runner)
    credential = runner.credential_path(provider)
    runtime.atomic_write_text(
        credential,
        json.dumps(runner.credential_payload(provider, "sk-test-key-value")),
        mode=0o600,
    )
    paths = runtime.provision_run_sandbox(provider, run_id, runner)
    runtime.release_run_lock(paths["root"])
    return provider, runner, paths


def test_a_fresh_sandbox_links_the_credential():
    _, runner, paths = _provision("glm", "run-fresh")
    link = runner.sandbox_credential_path(paths)
    assert link.is_symlink()
    assert json.loads(link.read_text())["glm-worker"]["key"] == "sk-test-key-value"


def test_a_link_left_pointing_at_a_moved_root_is_repaired():
    provider, runner, paths = _provision("glm", "run-stale")
    link = runner.sandbox_credential_path(paths)

    # Simulate what relocating the data root leaves behind.
    stale = paths["root"] / "somewhere-that-moved" / "auth.json"
    link.unlink()
    os.symlink(str(stale), str(link))
    assert link.is_symlink() and not link.exists(), "precondition: dangling"

    runtime.release_run_lock(paths["root"])
    runtime.provision_run_sandbox(provider, "run-stale", runner)

    assert os.readlink(link) == str(runner.credential_path(provider))
    assert json.loads(link.read_text())["glm-worker"]["key"] == "sk-test-key-value"


def test_reprovisioning_an_intact_sandbox_changes_nothing():
    provider, runner, paths = _provision("glm", "run-intact")
    link = runner.sandbox_credential_path(paths)
    before = os.readlink(link)

    runtime.provision_run_sandbox(provider, "run-intact", runner)
    runtime.release_run_lock(paths["root"])

    assert os.readlink(link) == before
    # No temp file left behind by the swap path, which must not have run.
    assert not list(link.parent.glob("*.relink.tmp"))


def test_the_shared_cache_link_is_repaired_the_same_way():
    provider, runner, paths = _provision("glm", "run-cache")
    cache = paths["cache"]
    cache.unlink()
    os.symlink(str(paths["root"] / "gone"), str(cache))

    runtime.release_run_lock(paths["root"])
    runtime.provision_run_sandbox(provider, "run-cache", runner)

    assert os.readlink(cache) == str(providers.profile_paths(provider)["cache"])
    assert cache.is_dir()
