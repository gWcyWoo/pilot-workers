"""Offline tests for generating the per-host worker region of a deployed skill.

A host's deployed SKILL.md carries a marker-delimited region listing exactly the
providers configured for that host, and which modes each one is the default for.
A host must never see a provider that was never installed for it.
"""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest

from pilot_workers import providers
from pilot_workers.cli import install as install_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Isolate the pilot home and give a fake host target directory."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return {"home": tmp_path / "home", "target": tmp_path / "target"}



def _flat(text: str) -> str:
    """Collapse whitespace: skill doctrine is hard-wrapped, so a plain substring
    test silently misses any phrase that spans a line break."""
    return " ".join(text.split())

def _claude_skill(isolated) -> Path:
    return isolated["target"] / "skills" / "pilot-workers" / "SKILL.md"


def _manifest(isolated) -> dict:
    path = isolated["home"] / "install-manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ----------------------------------------------------------------------
# B1: rendering the region
# ----------------------------------------------------------------------


def test_markers_are_html_comments():
    """Markers must be invisible in rendered markdown."""
    assert install_mod.GENERATED_BEGIN.startswith("<!--")
    assert install_mod.GENERATED_BEGIN.endswith("-->")
    assert install_mod.GENERATED_END.startswith("<!--")
    assert install_mod.GENERATED_END.endswith("-->")


def test_region_is_wrapped_in_markers():
    region = install_mod.render_worker_region(["glm"], {})
    assert region.startswith(install_mod.GENERATED_BEGIN)
    assert region.rstrip().endswith(install_mod.GENERATED_END)


def test_region_lists_the_configured_provider():
    region = install_mod.render_worker_region(["kimi-k3"], {})
    assert "kimi-k3" in region


def test_region_never_names_an_unconfigured_provider():
    """The core invariant: claude must not learn glm exists."""
    region = install_mod.render_worker_region(["kimi-k3"], {"code": "kimi-k3"})
    assert "glm" not in region
    assert "ds" not in region


def test_region_pairs_a_mode_with_its_provider():
    region = install_mod.render_worker_region(
        ["kimi-k3", "ds"], {"code": "kimi-k3", "explore": "ds"})
    kimi_row = next(l for l in region.splitlines() if "kimi-k3" in l)
    ds_row = next(l for l in region.splitlines() if l.startswith("| ds"))
    assert "code" in kimi_row
    assert "explore" in ds_row
    assert "explore" not in kimi_row


def test_region_groups_multiple_modes_on_one_provider():
    region = install_mod.render_worker_region(
        ["ds"], {"explore": "ds", "test": "ds"})
    ds_row = next(l for l in region.splitlines() if l.startswith("| ds"))
    assert "explore" in ds_row and "test" in ds_row


def test_region_marks_a_provider_with_no_default():
    region = install_mod.render_worker_region(["glm"], {})
    glm_row = next(l for l in region.splitlines() if l.startswith("| glm"))
    assert "code" not in glm_row
    assert glm_row.count("|") >= 3  # provider column + modes column


def test_region_preserves_configured_order():
    """Install order is the user's own ordering; do not re-sort it."""
    region = install_mod.render_worker_region(["ds", "glm", "kimi-k3"], {})
    rows = [l for l in region.splitlines() if l.startswith("| ")]
    body = [r for r in rows if "---" not in r][1:]  # drop the header row
    assert [r.split("|")[1].strip() for r in body] == ["ds", "glm", "kimi-k3"]


def test_region_is_a_markdown_table():
    region = install_mod.render_worker_region(["glm"], {"code": "glm"})
    lines = [l for l in region.splitlines() if l.startswith("|")]
    assert len(lines) >= 3          # header, separator, one data row
    assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-", ":"}


def test_region_for_no_providers_is_empty_string():
    """No providers means no skill at all, so there is nothing to render."""
    assert install_mod.render_worker_region([], {}) == ""


def test_region_ignores_a_default_naming_an_unlisted_provider():
    """Defensive: a stale default must not smuggle a name into the region."""
    region = install_mod.render_worker_region(["ds"], {"code": "glm"})
    assert "glm" not in region


def test_region_is_deterministic():
    a = install_mod.render_worker_region(["ds", "glm"], {"explore": "ds"})
    b = install_mod.render_worker_region(["ds", "glm"], {"explore": "ds"})
    assert a == b


# ----------------------------------------------------------------------
# B2: splicing the region into skill text
# ----------------------------------------------------------------------


SKILL_WITH_MARKERS = f"""---
name: pilot-workers
description: Some description.
---

# Playbook

Hand-written doctrine above.

{install_mod.GENERATED_BEGIN}
stale content
{install_mod.GENERATED_END}

Hand-written doctrine below.
"""


def _spliced(providers_list, defaults, text=SKILL_WITH_MARKERS):
    region = install_mod.render_worker_region(providers_list, defaults)
    return install_mod.apply_generated_region(text, region)


def test_apply_replaces_the_stale_region():
    out = _spliced(["glm"], {"code": "glm"})
    assert "stale content" not in out
    assert "glm" in out


def test_apply_preserves_prose_above_and_below():
    """The whole point of markers: hand-edited doctrine must survive."""
    out = _spliced(["glm"], {})
    assert "Hand-written doctrine above." in out
    assert "Hand-written doctrine below." in out
    assert "# Playbook" in out


def test_apply_preserves_frontmatter():
    out = _spliced(["glm"], {})
    assert out.startswith("---\nname: pilot-workers\n")


def test_apply_keeps_exactly_one_marker_pair():
    out = _spliced(["glm"], {})
    assert out.count(install_mod.GENERATED_BEGIN) == 1
    assert out.count(install_mod.GENERATED_END) == 1


def test_apply_is_idempotent():
    once = _spliced(["ds", "glm"], {"explore": "ds"})
    twice = install_mod.apply_generated_region(
        once, install_mod.render_worker_region(["ds", "glm"], {"explore": "ds"}))
    assert once == twice


def test_apply_replaces_rather_than_accumulates():
    """Regenerating with different config must not leave the old table behind."""
    first = _spliced(["glm"], {"code": "glm"})
    second = install_mod.apply_generated_region(
        first, install_mod.render_worker_region(["ds"], {"explore": "ds"}))
    assert "glm" not in second
    assert "ds" in second


def test_apply_with_empty_region_leaves_empty_markers():
    """No providers renders nothing, but the file must stay well-formed."""
    out = install_mod.apply_generated_region(SKILL_WITH_MARKERS, "")
    assert "stale content" not in out
    assert "Hand-written doctrine above." in out
    assert "Hand-written doctrine below." in out


def test_apply_raises_when_markers_are_missing():
    """A packaged skill without markers is a packaging bug — fail loudly."""
    with pytest.raises(RuntimeError, match="marker"):
        install_mod.apply_generated_region("# no markers here\n", "x")


def test_apply_raises_when_end_marker_precedes_begin():
    broken = f"{install_mod.GENERATED_END}\nx\n{install_mod.GENERATED_BEGIN}\n"
    with pytest.raises(RuntimeError, match="marker"):
        install_mod.apply_generated_region(broken, "x")


def test_apply_raises_on_duplicate_begin_marker():
    doubled = (
        f"{install_mod.GENERATED_BEGIN}\na\n{install_mod.GENERATED_END}\n"
        f"{install_mod.GENERATED_BEGIN}\nb\n{install_mod.GENERATED_END}\n"
    )
    with pytest.raises(RuntimeError, match="marker"):
        install_mod.apply_generated_region(doubled, "x")


def test_apply_does_not_reindent_or_reflow_surrounding_text():
    text = SKILL_WITH_MARKERS.replace(
        "Hand-written doctrine below.",
        "    indented line\n\n\ttabbed line")
    out = install_mod.apply_generated_region(
        text, install_mod.render_worker_region(["glm"], {}))
    assert "    indented line" in out
    assert "\ttabbed line" in out


# ----------------------------------------------------------------------
# B3: lifecycle — the deployed skill exists iff the host has a provider
# ----------------------------------------------------------------------


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_packaged_skill_carries_the_markers(host):
    """apply_generated_region raises without them, so packaging must include them."""
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    assert text.count(install_mod.GENERATED_BEGIN) == 1
    assert text.count(install_mod.GENERATED_END) == 1
    assert text.index(install_mod.GENERATED_BEGIN) < text.index(
        install_mod.GENERATED_END)


def test_first_provider_creates_the_skill(isolated):
    assert not _claude_skill(isolated).exists()
    rc = install_mod.main(
        ["glm", "on", "claude", "--target", str(isolated["target"])])
    assert rc == 0
    assert _claude_skill(isolated).is_file()
    assert "glm" in _claude_skill(isolated).read_text(encoding="utf-8")


def test_second_provider_regenerates_with_both(isolated):
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    install_mod.main(["ds", "on", "claude", "--target", t])
    text = _claude_skill(isolated).read_text(encoding="utf-8")
    assert "glm" in text and "ds" in text


def test_regenerating_replaces_the_skill_instead_of_truncating_it(isolated):
    """Every skill write must be a replace, including the routine splice path.

    A reader that already opened the deployed skill is the planner reading its
    own doctrine. A truncating write rewrites that same inode, so the reader's
    handle starts returning half a file; an atomic replace leaves it whole.
    """
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    install_mod.main(["ds", "on", "claude", "--target", t])
    skill = _claude_skill(isolated)
    before = skill.read_text(encoding="utf-8")

    # No --target: the splice-into-deployed path, the one routine
    # provider install/uninstall takes.
    with skill.open("r", encoding="utf-8") as reader:
        assert install_mod.uninstall_main(["glm", "on", "claude"]) == 0
        assert reader.read() == before, "an in-flight reader saw the rewrite"

    after = skill.read_text(encoding="utf-8")
    assert "ds" in after and "glm" not in after


def test_the_deployed_skill_has_the_same_mode_as_its_siblings(isolated):
    """The skill is now written, not copied. A NamedTemporaryFile is 0600, which
    would leave one file in the deployed directory unlike all the others for no
    reason — it carries no secret."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    skill = _claude_skill(isolated)
    assert stat.S_IMODE(skill.stat().st_mode) == 0o644
    assert not list(skill.parent.glob(".skill.*.tmp"))


def test_a_stranded_temp_file_does_not_block_uninstall(isolated, capsys):
    """A crash mid-write can leave a `.skill.*.tmp` nothing records. Its mere
    presence made the uninstall's rmdir fail silently, leaving the directory."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    skill_dir = _claude_skill(isolated).parent
    (skill_dir / ".skill.leftover.tmp").write_text("half a doctrine", encoding="utf-8")
    capsys.readouterr()

    assert install_mod.uninstall_main(["claude"]) == 0

    assert not skill_dir.exists(), "the skill directory outlived its uninstall"


def test_the_template_placeholder_is_never_the_text_on_disk(isolated):
    """Deploy used to copy the packaged template, then render over it.

    Between those two steps the planner's own doctrine file said
    ``{{PILOT_PROVIDER_TRIGGERS}}`` and carried an empty worker table. Rendering
    happens first now, so no reader can observe the template.
    """
    t = str(isolated["target"])
    copied: list[str] = []
    real_copy = install_mod.shutil.copy2

    def watch(src, dst, *a, **k):
        copied.append(Path(dst).name)
        return real_copy(src, dst, *a, **k)

    install_mod.shutil.copy2 = watch
    try:
        assert install_mod.main(["glm", "on", "claude", "--target", t]) == 0
    finally:
        install_mod.shutil.copy2 = real_copy

    assert "SKILL.md" not in copied, (
        "the packaged template was copied to disk before being rendered over")
    text = _claude_skill(isolated).read_text(encoding="utf-8")
    assert install_mod.TRIGGER_PLACEHOLDER not in text
    assert "glm" in text


def test_deployed_skill_never_names_an_unconfigured_provider(isolated):
    """The end-to-end form of the core invariant."""
    install_mod.main(
        ["kimi-k3", "on", "claude", "for", "code",
         "--target", str(isolated["target"])])
    text = _claude_skill(isolated).read_text(encoding="utf-8")
    assert "glm" not in text


def test_uninstalling_one_of_two_regenerates_without_it(isolated):
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    install_mod.main(["ds", "on", "claude", "--target", t])
    assert install_mod.uninstall_main(["glm", "on", "claude"]) == 0
    text = _claude_skill(isolated).read_text(encoding="utf-8")
    assert _claude_skill(isolated).is_file()
    assert "glm" not in text
    assert "ds" in text


def test_uninstalling_the_last_provider_deletes_the_skill(isolated):
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    assert _claude_skill(isolated).is_file()

    assert install_mod.uninstall_main(["glm", "on", "claude"]) == 0

    assert not _claude_skill(isolated).exists()
    assert "claude" not in _manifest(isolated).get("installs", {})


def test_bare_host_install_without_providers_deploys_nothing(isolated, capsys):
    """No providers means no skill; the user is told what to do instead."""
    rc = install_mod.main(["claude", "--target", str(isolated["target"])])
    assert rc == 0
    assert not _claude_skill(isolated).exists()
    out = capsys.readouterr().out
    assert "install" in out and "on claude" in out


def test_bare_host_install_never_deletes_a_deployed_skill(isolated):
    """The v0.5.1 upgrade path must not silently remove working delegation."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    assert _claude_skill(isolated).is_file()

    assert install_mod.main(["claude", "--target", t]) == 0

    assert _claude_skill(isolated).is_file()
    assert "glm" in _claude_skill(isolated).read_text(encoding="utf-8")


def test_default_mode_appears_in_the_deployed_skill(isolated):
    install_mod.main(
        ["ds", "on", "claude", "for", "explore",
         "--target", str(isolated["target"])])
    text = _claude_skill(isolated).read_text(encoding="utf-8")
    region = text.split(install_mod.GENERATED_BEGIN)[1].split(
        install_mod.GENERATED_END)[0]
    assert "explore" in region
    assert "ds" in region


def test_clearing_a_default_regenerates_without_the_mode(isolated):
    t = str(isolated["target"])
    install_mod.main(["ds", "on", "claude", "for", "explore",
                      "--target", t])
    assert install_mod.uninstall_main(["for", "explore", "on", "claude"]) == 0
    text = _claude_skill(isolated).read_text(encoding="utf-8")
    region = text.split(install_mod.GENERATED_BEGIN)[1].split(
        install_mod.GENERATED_END)[0]
    assert "ds" in region          # still visible
    assert "explore" not in region  # but no longer the default


def test_hand_edited_doctrine_survives_regeneration(isolated):
    """Marker regions exist so a user's own edits are not destroyed."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    path = _claude_skill(isolated)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## My own note\nkeep me\n",
        encoding="utf-8")

    install_mod.main(["ds", "on", "claude", "--target", t])

    text = path.read_text(encoding="utf-8")
    assert "## My own note" in text
    assert "keep me" in text
    assert "ds" in text


# ----------------------------------------------------------------------
# B4: no provider name survives in static skill text
# ----------------------------------------------------------------------


PROVIDER_KEYS = ("glm", "kimi-k3", "ds")


def _names_provider(text: str, key: str) -> bool:
    """Whole-token match. A bare substring test is wrong: ``ds`` occurs inside
    ordinary words like "needs", so it reports prose as a hardcoded provider."""
    return re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", text) is not None


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_packaged_skill_hardcodes_no_provider_key(host):
    """Static text must name no provider, or an uninstalled one leaks through."""
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    for key in PROVIDER_KEYS:
        assert not _names_provider(text, key), \
            f"{host} skill still hardcodes {key!r}"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_packaged_skill_names_no_vendor_brand_either(host):
    """A key's brand stem is as much a leak as the key.

    An example reading "use kimi for this" told every host about a provider it
    may not have — the key is ``kimi-k3``, so the whole-token check above saw
    nothing wrong.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    stems = {re.match(r"[a-z]+", key).group(0) for key in providers.PROVIDERS}
    for stem in stems:
        assert not _names_provider(text, stem), \
            f"{host} skill names the vendor {stem!r}"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_packaged_skill_has_the_trigger_placeholder(host):
    """The frontmatter is YAML and cannot hold markers, so it uses a token."""
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    assert text.count(install_mod.TRIGGER_PLACEHOLDER) == 1


def test_deployed_frontmatter_names_the_configured_provider(isolated):
    """The trigger list is what makes "use kimi" reach this skill at all."""
    install_mod.main(["kimi-k3", "on", "claude", "--target",
                      str(isolated["target"])])
    frontmatter = _claude_skill(isolated).read_text(
        encoding="utf-8").split("---")[1]
    assert "kimi-k3" in frontmatter
    assert install_mod.TRIGGER_PLACEHOLDER not in frontmatter


def test_deployed_frontmatter_omits_unconfigured_providers(isolated):
    install_mod.main(["kimi-k3", "on", "claude", "--target",
                      str(isolated["target"])])
    frontmatter = _claude_skill(isolated).read_text(
        encoding="utf-8").split("---")[1]
    assert "glm" not in frontmatter
    assert "ds" not in frontmatter


def test_no_provider_name_anywhere_in_a_single_provider_deployment(isolated):
    """The whole-file form of the invariant, frontmatter and body together."""
    install_mod.main(["kimi-k3", "on", "claude", "for", "code",
                      "--target", str(isolated["target"])])
    text = _claude_skill(isolated).read_text(encoding="utf-8")
    assert _names_provider(text, "kimi-k3")
    assert not _names_provider(text, "glm")
    assert not _names_provider(text, "ds")


def test_placeholder_is_replaced_for_every_configured_provider(isolated):
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    install_mod.main(["ds", "on", "claude", "--target", t])
    text = _claude_skill(isolated).read_text(encoding="utf-8")
    assert install_mod.TRIGGER_PLACEHOLDER not in text
    assert "glm" in text and "ds" in text
    assert "kimi-k3" not in text


# ----------------------------------------------------------------------
# cross-model review follow-ups
# ----------------------------------------------------------------------


def test_bare_host_install_preserves_hand_edits(isolated):
    """The bare-host path purges and re-copies, so it must save the old text too.

    The provider-install path already did this; this covers the OTHER path,
    which read the freshly-copied package template as its base and so silently
    destroyed anything the user had added outside the markers.
    """
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    path = _claude_skill(isolated)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## My own note\nkeep me\n",
        encoding="utf-8")

    assert install_mod.main(["claude", "--target", t]) == 0

    text = path.read_text(encoding="utf-8")
    assert "## My own note" in text
    assert "keep me" in text


def test_flat_v3_entry_with_no_providers_is_left_alone(isolated):
    """The real v0.5.1 upgrade path: a deployment exists, config is empty.

    The previous test for this pre-configured a provider and so took the
    reinstall branch, never exercising the guard it was named for.
    """
    t = isolated["target"]
    install_mod.main(["glm", "on", "claude", "--target", str(t)])
    path = _claude_skill(isolated)
    original = path.read_text(encoding="utf-8")

    # Strip the config, keeping the deployment: exactly a v0.5.1 install.
    manifest_path = isolated["home"] / "install-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["installs"]["claude"]["providers"] = []
    data["installs"]["claude"]["modes"] = {}
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    assert install_mod.main(["claude", "--target", str(t)]) == 0

    assert path.is_file()
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_lifecycle_works_for_every_host(isolated, monkeypatch, host):
    """codex deploys to a different path than claude; both must work."""
    monkeypatch.setenv("CODEX_HOME", str(isolated["home"] / "codex-home"))
    t = isolated["target"]
    assert install_mod.main(["glm", "on", host, "--target", str(t)]) == 0

    deployed = ((t / "skills" / "pilot-workers" / "SKILL.md") if host == "claude"
                else (t / "pilot-workers" / "SKILL.md"))
    assert deployed.is_file()
    assert "glm" in deployed.read_text(encoding="utf-8")

    assert install_mod.uninstall_main(["glm", "on", host]) == 0
    assert not deployed.exists()


def test_provider_install_deploys_to_the_default_location(tmp_path, monkeypatch):
    """--target is a test-isolation flag; the real command must work without it.

    Without this, `pilot-workers install glm on claude` records the routing
    decision but never creates the skill, so the planner never sees it and the
    whole feature is inert in default usage.
    """
    fake_home = tmp_path / "fakehome"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(fake_home / ".codex"))

    assert install_mod.main(["glm", "on", "claude"]) == 0

    deployed = fake_home / ".claude" / "skills" / "pilot-workers" / "SKILL.md"
    assert deployed.is_file()
    assert "glm" in deployed.read_text(encoding="utf-8")


def test_default_location_deploy_is_recorded_in_the_manifest(tmp_path, monkeypatch):
    fake_home = tmp_path / "fakehome"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(fake_home / ".codex"))

    install_mod.main(["glm", "on", "claude"])

    manifest = json.loads(
        (fake_home / ".codex" / "install-manifest.json").read_text(encoding="utf-8"))
    entry = manifest["installs"]["claude"]
    assert entry["files"], "a deploy must record its files so uninstall can purge"
    assert entry["providers"] == ["glm"]


def test_uninstalling_last_provider_purges_a_default_location_deploy(
        tmp_path, monkeypatch):
    fake_home = tmp_path / "fakehome"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(fake_home / ".codex"))
    install_mod.main(["glm", "on", "claude"])
    deployed = fake_home / ".claude" / "skills" / "pilot-workers" / "SKILL.md"
    assert deployed.is_file()

    assert install_mod.uninstall_main(["glm", "on", "claude"]) == 0

    assert not deployed.exists()


def test_target_accepts_the_equals_form(isolated):
    """LOW-4: --target=dir is parsed but was never exercised."""
    t = isolated["target"]
    assert install_mod.main(["glm", "on", "claude", f"--target={t}"]) == 0
    assert (t / "skills" / "pilot-workers" / "SKILL.md").is_file()


def test_install_all_with_no_providers_deploys_nothing(isolated, capsys):
    """LOW-8: only the single-host empty case was covered."""
    assert install_mod.main(["all", "--target", str(isolated["target"])]) == 0
    out = capsys.readouterr().out
    for host in providers.HOSTS:
        assert f"on {host}" in out, f"'all' said nothing about {host}"
    assert not (isolated["target"] / "skills" / "pilot-workers").exists()


def test_deploy_failure_returns_one_not_a_traceback(isolated, monkeypatch, capsys):
    """LOW-7: the except (OSError, RuntimeError) path had zero coverage."""
    def _boom(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(install_mod, "install_host", _boom)
    rc = install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    assert rc == 1
    assert "disk on fire" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_skill_tells_the_planner_how_to_fix_a_missing_credential(host):
    """A recorded provider with no key fails every dispatch; the planner must
    know the fix. codex-host carried this guidance and claude-host did not, so
    a Claude planner hit "credential missing" with nothing to act on.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    assert "--global-key" in text, f"{host} skill gives no credential fix"


def test_manifest_records_the_files_a_deploy_created(isolated):
    """Ordering guarantee: the deploy runs BEFORE the manifest write.

    A review suggested swapping them so a failed manifest write cannot leave a
    stale skill file. That would be worse: `files`/`created_dirs` are filled in
    BY the deploy, so persisting first records an entry with no file list and
    uninstall could never purge the deployment. The accepted trade-off is that a
    failed manifest write leaves deployed-but-unrecorded files, which `status`
    flags and a re-run repairs — see the next test.
    """
    t = isolated["target"]
    assert install_mod.main(["glm", "on", "claude", "--target", str(t)]) == 0
    entry = _manifest(isolated)["installs"]["claude"]
    assert entry["files"], "uninstall needs this to purge"
    for name in entry["files"]:
        assert Path(name).is_file()


def test_a_failed_manifest_write_is_repaired_by_rerunning(isolated, monkeypatch):
    """The failure mode left by deploy-then-write must be recoverable."""
    t = str(isolated["target"])

    real_write = install_mod._write_manifest
    calls = []

    def _fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("no space left on device")
        return real_write(*args, **kwargs)

    # NOT monkeypatch.undo(): that would also unwind conftest's home isolation,
    # which shares this test's monkeypatch instance.
    monkeypatch.setattr(install_mod, "_write_manifest", _fail_once)
    assert install_mod.main(["glm", "on", "claude", "--target", t]) == 1

    assert install_mod.main(["glm", "on", "claude", "--target", t]) == 0
    entry = _manifest(isolated)["installs"]["claude"]
    assert entry["providers"] == ["glm"]
    assert entry["files"]


# ----------------------------------------------------------------------
# the point of the whole feature: the planner must USE the generated table
# ----------------------------------------------------------------------


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_skill_tells_the_planner_to_read_the_worker_table(host):
    """A table nobody is told to consult changes nothing.

    The generated region records "explore -> ds". Unless the doctrine sends the
    planner there when the user did not name a provider, it still guesses, and
    the entire per-host configuration feature is inert.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")

    # Must appear in the header doctrine, BEFORE the per-mode sections: a
    # mention buried in "Mode: code" passed while the planner's first decision
    # went unaddressed.
    header = _flat(text[:text.index("Mode:")])
    assert "Workers table" in header, (
        "the provider-choice doctrine never points at the generated table")


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_skill_states_the_users_word_wins(host):
    """Explicit beats configured: a named provider must override the table."""
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    doctrine = _flat(packaged.read_text(encoding="utf-8")).lower()
    # The rule is the OVERRIDE, not the trigger: an explicit provider beats the
    # configured table. Asserting only "names a provider" passed on the trigger
    # clause while saying nothing about precedence.
    assert "that always wins" in doctrine, "no statement that explicit wins"


def test_deployed_skill_pairs_the_doctrine_with_the_live_table(isolated):
    """End-to-end: a planner reading the deployed file can resolve explore->ds."""
    t = str(isolated["target"])
    install_mod.main(["ds", "on", "claude", "for", "explore", "--target", t])
    text = _claude_skill(isolated).read_text(encoding="utf-8")

    marker = text.index(install_mod.GENERATED_BEGIN)
    assert "Workers table" in text[:marker], (
        "no doctrine sends the planner to the table")

    region = text[marker:text.index(install_mod.GENERATED_END)]
    ds_row = next(l for l in region.splitlines() if l.startswith("| ds"))
    assert "explore" in ds_row


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_code_verification_defers_to_an_assigned_tester(host):
    """Assigning a provider to `test` must actually route the test run there.

    The code-mode doctrine said "run tests/lint yourself", which silently voids
    the `for test` assignment: the main session would run the suite it had just
    delegated. Judgment stays with the planner; executing the run does not.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")

    marker = text.index(install_mod.GENERATED_BEGIN)
    doctrine = text[:marker]
    code_section = doctrine[doctrine.index("Mode: code"):]

    assert "run tests/lint yourself" not in code_section
    assert "assigned to `test`" in code_section, (
        "code-mode verification never mentions the test assignment")


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_explore_doctrine_has_no_ritual_spot_check(host):
    """Sampling 2 of 20 conclusions licenses trusting the other 18 — worse than
    no check, because it manufactures confidence. Exploration output is
    information the planner is about to USE, so planning verifies it: a large
    gap goes back to the explorer, a few lines are read here.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    explore = _flat(text[text.index("Mode: explore"):text.index("Mode: code")])

    assert "trust the whole report" not in explore
    # "Spot-check" was deleted long ago, so asserting its absence caught nothing.
    # Assert the replacement instead: sampling is named and rejected, and the
    # two proportionate responses to a gap are both spelled out.
    assert "do not sample" in explore.lower(), "sampling is not explicitly rejected"
    assert "read them here" in explore, "no small-gap path that avoids a round-trip"
    assert "explore dispatch" in explore, "no large-gap path back to the worker"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_review_findings_are_still_verified_before_acting(host):
    """Unlike exploration, a review finding is a CLAIM acted on by editing code:
    a false positive causes a wrong edit, so it must be checked first."""
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    review = _flat(text[text.index("Mode: review"):]).lower()
    # The instruction, not just the motivation.
    assert "verify every finding you intend to act on" in review


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_code_verification_checks_the_file_set_not_diff_samples(host):
    """Same principle as explore: complete cheap checks yes, sampling no.

    The changed-FILE-SET is one property checkable in full for ~100 tokens, and
    it catches out-of-scope writes. Reading a few hunks of a large diff licenses
    nothing about the rest — correctness is established by the test run, the
    end-to-end check, and cross-model review for large diffs.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    code = _flat(text[text.index("Mode: code"):text.index("Mode: test")])

    # Assert on the CONCEPT, not one phrasing: earlier versions of this test
    # pinned an exact wording and passed while the sampling step was still there.
    assert "spot-check" not in code.lower(), "diff sampling is still prescribed"
    assert "diff --stat" in code, "the file-set check must survive"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_unassigned_mode_falls_to_the_main_session(host):
    """A mode with no assignment is not an invitation to guess a provider.

    Delegation happens for exactly two reasons: the user configured it, or the
    user asked for it in the moment. Everything else the planner does itself —
    that is what "default" means here, and it is what makes the system
    predictable rather than surprising.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    text = packaged.read_text(encoding="utf-8")
    doctrine = _flat(text[:text.index(install_mod.GENERATED_BEGIN)])

    assert "do it yourself" in doctrine.lower(), (
        "no instruction to handle an unassigned mode in the main session")
    # The old rule told the planner to pick a provider by strengths instead.
    assert "pick from the" not in doctrine.lower(), (
        "still tells the planner to guess a provider for an unassigned mode")


# ----------------------------------------------------------------------
# upgrading from a release whose skill had no markers
# ----------------------------------------------------------------------


def _legacy_deployment(isolated) -> Path:
    """A v0.5.1 deployment: manifest v3, and a skill with NO marker pair.

    Synthesised from the packaged skill by cutting the marker region out, NOT
    read from git. The previous version ran
    `git show HEAD:...SKILL.md` and asserted the result had no markers, which
    made it depend on this repo's history twice over: it FAILED (rather than
    skipped) outside a checkout, and it broke the moment the marker-bearing skill
    was committed — HEAD then had markers and the fixture's own assertion fired.
    What the test needs is a markerless deployment; cutting the region produces
    exactly that, hermetically.
    """
    skill = _claude_skill(isolated)
    skill.parent.mkdir(parents=True, exist_ok=True)
    packaged = install_mod._packaged_skill_text("claude")
    begin = packaged.find(install_mod.GENERATED_BEGIN)
    end = packaged.find(install_mod.GENERATED_END)
    if begin != -1 and end != -1:
        released = packaged[:begin] + packaged[end + len(install_mod.GENERATED_END):]
    else:
        released = packaged
    assert install_mod.GENERATED_BEGIN not in released
    assert install_mod.GENERATED_END not in released
    skill.write_text(released, encoding="utf-8")

    manifest = isolated["home"] / "install-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "schema_version": 3,
        "installs": {"claude": {
            "installed_at": "2026-01-01T00:00:00+00:00",
            "package_version": "0.5.1",
            "files": [str(skill)], "created_dirs": [],
        }},
    }), encoding="utf-8")
    return skill


def test_upgrade_from_a_markerless_deployment_succeeds(isolated, capsys):
    """The released skill has no markers, so splicing into it must not fail.

    Left unhandled the command printed an error, still exited 0, and left a
    skill with no Workers table — the planner would never learn who to dispatch
    to, and the user would believe it was configured.
    """
    _legacy_deployment(isolated)

    rc = install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])

    assert rc == 0
    err = capsys.readouterr().err
    assert "marker" not in err, f"upgrade still reports a marker problem: {err}"

    text = _claude_skill(isolated).read_text(encoding="utf-8")
    assert install_mod.GENERATED_BEGIN in text
    region = text[text.index(install_mod.GENERATED_BEGIN):
                  text.index(install_mod.GENERATED_END)]
    assert "glm" in region, "the generated table never made it into the skill"


def test_a_failure_never_leaves_the_host_without_a_skill(isolated, monkeypatch):
    """Render before destroy: purging and re-copying ahead of a render that can
    raise leaves the host with no working skill at all."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    before = _claude_skill(isolated).read_text(encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(install_mod, "_render_deployed_skill", _boom)
    install_mod.main(["ds", "on", "claude", "--target", t])

    assert _claude_skill(isolated).is_file(), "skill was destroyed by a failed render"
    assert _claude_skill(isolated).read_text(encoding="utf-8") == before


def test_bare_host_install_also_renders_before_destroying(isolated, monkeypatch):
    """The plain `install <host>` path purges and re-copies too, so it needs the
    same ordering as the provider path."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    before = _claude_skill(isolated).read_text(encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(install_mod, "_render_deployed_skill", _boom)
    install_mod.main(["claude", "--target", t])

    assert _claude_skill(isolated).is_file(), "skill destroyed by a failed render"
    assert _claude_skill(isolated).read_text(encoding="utf-8") == before


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_review_verifies_every_acted_on_finding_not_a_sample(host):
    """Review keeps verification — a finding is a claim acted on by editing code,
    so a false positive causes a wrong edit. But "spot-check 1-2" is sampling:
    the principled form is complete verification over the set you will act on.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    review = _flat(packaged.read_text(encoding="utf-8"))
    review = review[review.index("Mode: review"):]

    # Not "Spot-check 1-2": that phrasing is long gone, so its absence proves
    # nothing. What must hold is the positive rule and its reason.
    assert "every finding you intend to act on" in review.lower(), (
        "no instruction to verify the whole acted-on set")
    assert "false positive" in review.lower(), "the reason to verify is missing"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_code_doctrine_reconciles_the_workers_own_file_list(host):
    """A complete cheap check that was missing: the worker reports
    FILES_CHANGED, and `git diff --stat` says what actually changed. Comparing
    them costs nothing and catches a worker that misreports its own work."""
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    code = _flat(packaged.read_text(encoding="utf-8"))
    code = code[code.index("Mode: code"):code.index("Mode: test")]
    assert "FILES_CHANGED" in code, "the worker's own file list is never reconciled"


def test_provider_install_purges_legacy_entries_too(isolated, capsys):
    """A legacy v1/v2 host entry must be cleaned by whichever install touches it.

    Only the host form purged, so `install <provider> on <host>` over a legacy
    manifest left the v0.4.0 files on disk AND the sub-entry in the manifest —
    where a later host install or last-provider uninstall would no longer find
    them, stranding both forever.
    """
    target = isolated["target"]
    legacy_file = target / "agents" / "glm-coder.md"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text("legacy", encoding="utf-8")

    manifest_path = isolated["home"] / "install-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        "schema_version": 2,
        "installs": {"claude": {"glm": {
            "installed_at": "2025-01-01T00:00:00+00:00",
            "package_version": "0.4.0",
            "files": [str(legacy_file)], "created_dirs": [],
        }}},
    }), encoding="utf-8")

    assert install_mod.main(["ds", "on", "claude", "--target", str(target)]) == 0

    assert not legacy_file.exists(), "legacy file left on disk"
    entry = _manifest(isolated)["installs"]["claude"]
    leftovers = [k for k in entry
                 if k not in ("installed_at", "package_version", "files",
                              "created_dirs", "providers", "modes")]
    assert leftovers == [], f"legacy sub-entries stranded: {leftovers}"


def test_target_pointing_elsewhere_than_an_existing_deploy_is_refused(
        tmp_path, monkeypatch, capsys):
    """Redirecting the deploy silently orphaned the previous one.

    entry['files'] was replaced with the new target's paths, so the real skill
    the planner still reads became stale and untracked — uninstall could never
    remove it, and status could not see it.
    """
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(fake_home / ".codex"))

    assert install_mod.main(["glm", "on", "claude"]) == 0
    deployed = fake_home / ".claude" / "skills" / "pilot-workers" / "SKILL.md"
    assert deployed.is_file()

    elsewhere = tmp_path / "other"
    rc = install_mod.main(["ds", "on", "claude", "--target", str(elsewhere)])

    assert rc != 0, "silently redirected the deploy"
    assert "already deployed" in capsys.readouterr().err.lower()
    assert deployed.is_file(), "original deployment was orphaned"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_skill_states_the_guard_is_not_a_licence_to_paste(host):
    """The dispatch guard refuses obvious credential shapes, but a hand-written
    list of nine patterns will always be incomplete — dedicated scanners carry
    hundreds and still miss. Saying "never put a credential in a task file"
    without saying "the check cannot be relied on" invites the opposite.
    """
    packaged = (install_mod.INTEGRATIONS_DIR / f"{host}-host"
                / "skills" / "pilot-workers" / "SKILL.md")
    doctrine = _flat(packaged.read_text(encoding="utf-8")).lower()
    assert "cannot catch every" in doctrine, (
        "the skill never says the credential check is best-effort")


# ----------------------------------------------------------------------
# round 5: failure and recovery
# ----------------------------------------------------------------------


def test_a_deleted_skill_is_restored_by_rerunning(tmp_path, monkeypatch):
    """The user's only lever is to re-run the command that made the state.

    With the skill file gone but the provider still recorded, the re-run said
    "already recorded" and did nothing — leaving config that claims a worker the
    planner can never see, and no documented way back.
    """
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(fake_home / ".codex"))

    assert install_mod.main(["glm", "on", "claude"]) == 0
    skill = fake_home / ".claude" / "skills" / "pilot-workers" / "SKILL.md"
    assert skill.is_file()
    skill.unlink()

    assert install_mod.main(["glm", "on", "claude"]) == 0
    assert skill.is_file(), "re-running the same command did not restore the skill"
    assert "glm" in skill.read_text(encoding="utf-8")


def test_uninstall_reports_files_it_could_not_remove(isolated, capsys, monkeypatch):
    """Dropping the manifest entry after a failed unlink orphans the files with
    no record that they exist. Better to fail loudly and keep the record."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])

    real_unlink = install_mod.os.unlink

    def _refuse(path, *a, **k):
        if str(path).endswith("SKILL.md"):
            raise OSError(13, "Permission denied")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(install_mod.os, "unlink", _refuse)
    rc = install_mod.uninstall_main(["glm", "on", "claude"])

    err = capsys.readouterr().err
    assert rc != 0, "reported success while leaving files behind"
    assert "SKILL.md" in err, "never says which file could not be removed"


def test_the_deployed_skill_is_written_atomically(isolated, monkeypatch):
    """It was the only non-atomic write in the tool: a crash mid-write truncates
    the file the planner reads, and a truncation that keeps the markers would be
    silently regenerated over forever."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    skill = _claude_skill(isolated)
    before = skill.read_text(encoding="utf-8")

    # Fail the commit step only. The tree copy that precedes it is a separate
    # concern; what must hold is that the RENDER never lands half-written.
    real_replace = install_mod.os.replace
    calls = []

    def _fail_replace(src, dst, *a, **k):
        if ".skill." in str(src):
            calls.append(str(dst))
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(install_mod.os, "replace", _fail_replace)
    install_mod.main(["ds", "on", "claude", "--target", t])

    assert calls, "the render is not committed through a temp file at all"
    text = skill.read_text(encoding="utf-8")
    assert text.count(install_mod.GENERATED_BEGIN) == 1, (
        "a failed commit left the skill without exactly one marker pair")
    assert text.count(install_mod.GENERATED_END) == 1
    assert "# pilot-workers Playbook" in text, "the skill was left truncated"
    leftovers = list(skill.parent.glob(".skill.*"))
    assert not leftovers, f"temp files left behind: {leftovers}"


def test_a_file_dropped_from_the_packaged_tree_stops_being_deployed(isolated, capsys):
    """The refresh replaced the recorded file list without removing what fell
    out of it, so a file this package no longer ships stayed on disk AND left the
    record — after which nothing could ever remove it, and the planner went on
    reading doctrine from an abandoned version.
    """
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    skill_dir = _claude_skill(isolated).parent
    stale = skill_dir / "OLD-DOCTRINE.md"
    stale.write_text("doctrine from a previous version\n", encoding="utf-8")

    # Record it as deployed, the state a previous version's install would leave.
    manifest_path = isolated["home"] / "install-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["installs"]["claude"]["files"].append(str(stale))
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()

    install_mod.main(["ds", "on", "claude", "--target", t])

    assert not stale.exists(), "a file the package no longer ships stayed deployed"
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert str(stale) not in recorded["installs"]["claude"]["files"]
    assert "removed stale" in capsys.readouterr().out
