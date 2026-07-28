#!/usr/bin/env python3
"""Install integration files (host playbook skill) to host config directories.

Grammar:

    pilot-workers install <host|all> [--target <dir>]
    pilot-workers install runner <name>
    pilot-workers install <provider> on <host> [--global-key]
    pilot-workers install <provider> on <host> for <mode> [--global-key]
    pilot-workers uninstall <host|all>
    pilot-workers uninstall runner <name>
    pilot-workers uninstall <provider> on <host>
    pilot-workers uninstall for <mode> on <host>
    pilot-workers uninstall key <provider>

``install_host(host, target)`` copies
``INTEGRATIONS_DIR/<host>-host/skills/pilot-workers/`` recursively into
the host's skill directory (claude: ``~/.claude/skills``; codex:
``$CODEX_HOME/skills``; either overridden by ``--target``). The
``<provider> on <host>`` forms record per-host worker configuration
(provider visibility and mode assignments) in the manifest and keep the
deployed skill's marker-delimited worker region in sync with it: with
``--target`` the skill tree is deployed/refreshed, and removing the last
provider of a host purges the deployed skill.

The install manifest (schema v4) lives at <pilot_home>/install-manifest.json:

    {"schema_version": 4,
     "installs": {"<host>": {"installed_at": ..., "package_version": ...,
                              "files": [...], "created_dirs": [...],
                              "providers": [...], "modes": {...}}}}

``providers`` is a visibility list (which providers the host may use);
``modes`` maps mode -> provider. Installing over a v1/v2 manifest purges
every legacy entry for the host (v1 ``__all__`` first, then each v2 provider
entry) — one printed line per removed file — and the on-disk file is
rewritten as a clean v4 via ``os.replace``. A plain host reinstall carries
``providers``/``modes`` forward from the previous entry.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pilot_workers import policy, providers


INTEGRATIONS_DIR = Path(__file__).resolve().parent.parent / "integrations"

MANIFEST_SCHEMA_VERSION = 4

# One list, in providers.py beside the reserved keys it feeds. Re-exported here
# because this module is where the host grammar lives and callers reach for it.
HOSTS = providers.HOSTS

INSTALL_USAGE = (
    "usage: pilot-workers install <host|all> [--target <dir>]\n"
    "       pilot-workers install runner <name>\n"
    "       pilot-workers install <provider> on <host> [--global-key]\n"
    "       pilot-workers install <provider> on <host> for <mode> [--global-key]\n"
    "\n"
    "'on <host>' takes a single host; '<provider> on <host|all>' with 'all' "
    "is a usage error."
)

UNINSTALL_USAGE = (
    "usage: pilot-workers uninstall <host|all>\n"
    "       pilot-workers uninstall runner <name>\n"
    "       pilot-workers uninstall <provider> on <host>\n"
    "       pilot-workers uninstall for <mode> on <host>\n"
    "       pilot-workers uninstall key <provider>\n"
    "\n"
    "'on <host>' takes a single host; '<provider> on <host|all>' with 'all' "
    "is a usage error."
)


class _UsageError(Exception):
    pass


# ----------------------------------------------------------------------
# manifest
# ----------------------------------------------------------------------


def _manifest_path() -> Path:
    return providers.pilot_home() / "install-manifest.json"


def _package_version() -> str:
    try:
        return importlib.metadata.version("pilot-workers")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _validate_installs(data: dict, path: Path) -> None:
    """Reject a manifest whose shape would corrupt what reads it.

    ``_load_manifest`` already names two corruption shapes; these got past it
    and reached the accessors, where the outcome was worse than an error:
    ``providers: "glm"`` became the provider list ``['g','l','m']`` with no
    complaint, and ``modes: "glm"`` raised ValueError out of ``status`` as a
    traceback. A non-dict host ENTRY stays tolerated on purpose — that is how
    a legacy v1/v2 nesting reads as "not installed" and gets migrated.
    """
    installs = data.get("installs", {})
    if not isinstance(installs, dict):
        raise RuntimeError(
            f"corrupt install manifest {path}: 'installs' must be an object, "
            f"got {type(installs).__name__}"
        )
    for host, entry in installs.items():
        if not isinstance(entry, dict):
            continue
        if "providers" in entry:
            value = entry["providers"]
            if (not isinstance(value, list)
                    or not all(isinstance(item, str) for item in value)):
                raise RuntimeError(
                    f"corrupt install manifest {path}: {host}.providers must "
                    f"be a list of strings, got {value!r}"
                )
        if "modes" in entry:
            value = entry["modes"]
            if (not isinstance(value, dict)
                    or not all(isinstance(k, str) and isinstance(v, str)
                               for k, v in value.items())):
                raise RuntimeError(
                    f"corrupt install manifest {path}: {host}.modes must be an "
                    f"object of mode -> provider strings, got {value!r}"
                )


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "installs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"corrupt install manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"corrupt install manifest {path}: expected JSON object")
    if "installs" not in data and "hosts" in data:
        # In-memory v1 → v4 migration: a v1 host-level entry becomes a legacy
        # "__all__" sub-entry so the install path can purge it uniformly. The
        # file itself is rewritten as clean v4 on the next install.
        legacy_hosts = data.get("hosts", {})
        if not isinstance(legacy_hosts, dict):
            # Checked HERE rather than in _validate_installs: this migration
            # runs first, so a non-object `hosts` reached `.items()` and left
            # AttributeError as the error the user saw.
            raise RuntimeError(
                f"corrupt install manifest {path}: 'hosts' must be an object, "
                f"got {type(legacy_hosts).__name__}"
            )
        data = {
            "installs": {
                host: {"__all__": entry}
                for host, entry in legacy_hosts.items()
            },
        }
    _validate_installs(data, path)
    for host_entry in data.get("installs", {}).values():
        # In-memory v3 → v4 migration: a flat v3 host entry reads as having
        # empty provider config. The file itself is not rewritten here; the
        # next manifest write persists the keys.
        if isinstance(host_entry, dict) and isinstance(host_entry.get("files"), list):
            host_entry.setdefault("providers", [])
            host_entry.setdefault("modes", {})
    data["schema_version"] = MANIFEST_SCHEMA_VERSION
    data.setdefault("installs", {})
    return data


def _write_manifest(path: Path, data: dict) -> None:
    from pilot_workers import runtime

    runtime.atomic_write_text(
        path, json.dumps(data, indent=2) + "\n", mode=0o600,
        prefix=".install-manifest.")


@contextlib.contextmanager
def manifest_transaction(*, delete_when_empty: bool = False):
    """Read-modify-write the manifest under an exclusive lock.

    An atomic write guarantees the file is never torn; it does NOT guarantee the
    second writer saw the first's change. Two concurrent installs each loaded,
    modified and replaced the file, so last-writer-wins silently discarded a
    whole host's configuration.

    ``fcntl.flock`` rather than a pid file: the kernel releases it when the
    holder dies, so a crash cannot leave the manifest permanently unwritable.
    Yields the ``installs`` mapping; the manifest is written once, on clean exit
    only, so a failed operation leaves the previous state intact.
    """
    import fcntl

    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        manifest = _load_manifest(path)
        installs = manifest.setdefault("installs", {})
        yield installs
        if installs or not delete_when_empty:
            _write_manifest(path, manifest)
        elif path.exists():
            # Only an UNINSTALL deletes an emptied manifest. An install that
            # merely purged legacy entries must still leave a v4 file behind, or
            # the next run would re-migrate what was already migrated.
            path.unlink()
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)



def _write_skill_atomically(path: Path, text: str) -> None:
    """Replace a deployed skill in one step.

    This was the only truncating write in the tool: a crash or a full disk
    mid-write leaves the planner reading a half file, and a truncation that
    happens to keep both markers would be regenerated over forever without
    anyone noticing the doctrine had gone.

    The mode is set explicitly: a NamedTemporaryFile is 0600, and the skill is
    an ordinary readable document sitting among 0644 siblings copied from the
    package. It holds no secret, so matching them keeps the deployed directory
    uniform however each file got there.

    The temp name keeps the ``.skill.`` prefix the uninstall sweep looks for.
    """
    from pilot_workers import runtime

    runtime.atomic_write_text(path, text, mode=0o644, prefix=".skill.")



# Directory names this tool deploys into: the v4 skill directory and the two
# v0.4.0 locations. A recorded path outside all of them was never ours.
_DEPLOY_DIR_NAMES = frozenset({"pilot-workers", "agents", "commands"})


def _looks_deployed_by_us(path: Path,
                          allowed_roots: set[Path] | None = None) -> bool:
    """Whether a recorded path sits somewhere this tool deploys.

    With ``allowed_roots`` this is true containment against directories the
    caller knows (the orphan sweep knows its own destination). Without them it
    falls back to the name check below, which is all the purge path can do —
    it is reached from six call sites and knows neither host nor target.
    ONE helper on purpose: two containment mechanisms under two names is how
    this property drifted across three sites in the first place.

    The round-11 fix gave ``_deploy_skill_tree``'s orphan sweep a containment
    check and left this sibling — the uninstall/purge path — deleting whatever
    the manifest listed. Stated honestly: this is a name check, not true
    containment (``/anywhere/agents/x`` passes), because ``_purge_entry`` is
    called from six places and does not know the host or target. It blocks every
    realistic shape — a hand-edited entry, a manifest copied between machines,
    and anything else in the user's own ``~/.claude`` such as CLAUDE.md or
    settings.json.

    Normalised FIRST. Both variants were purely lexical, so
    ``<skilldir>/../../CLAUDE.md`` passed the name check (it still contains
    "pilot-workers") AND the root check (``Path.parents`` does not resolve
    ``..``), while ``os.unlink`` resolved it and removed the file two levels up.
    The line above claiming "true containment" was simply false. Normalised
    rather than resolved, and the residual gap is named rather than claimed
    away: a symlinked parent COMPONENT is not followed, so a recorded path
    under a ``skills`` symlink pointing elsewhere passes this check while
    living somewhere else. Resolving would close that and open another - it
    follows the final component too, and ``os.unlink`` removes a symlink as a
    link, so resolving would compare the wrong thing for a symlinked
    artifact. ``..`` is what a hand edit or a copied manifest produces.
    """
    path = Path(os.path.normpath(str(path)))
    if allowed_roots:
        return any(root == path.parent or root in path.parents
                   for root in allowed_roots)
    return any(part in _DEPLOY_DIR_NAMES for part in path.parts)


def _purge_entry(entry: dict) -> None:
    """Remove every file/dir recorded by a previous install entry.

    Prints one ``removed:`` line per file (and per emptied created dir) so a
    reinstall or migration leaves an auditable trail. Missing files are skipped
    silently. Directories are removed deepest-first so nested dirs go before
    their parents; only ``created_dirs`` entries are touched (never file parents
    the user may own).
    """
    failures: list[str] = []
    for name in entry.get("files", []):
        if not _looks_deployed_by_us(Path(name)):
            print(f"  note: refusing to remove {name}: not a path this tool "
                  f"deploys to", file=sys.stderr)
            continue
        try:
            os.unlink(name)
        except FileNotFoundError:
            # Already gone: the desired end state, not a failure.
            continue
        except OSError as exc:
            # Dropping the manifest entry after this would orphan the file with
            # nothing left recording that it exists.
            failures.append(f"{name}: {exc.strerror or exc}")
            continue
        print(f"  removed: {name}")
    if failures:
        raise RuntimeError(
            "could not remove "
            + "; ".join(failures)
            + " — the manifest entry is kept so these stay accounted for"
        )
    # created_dirs is manifest data too, and it drives an rmdir plus a tmp-file
    # glob. The containment check went to the recorded FILES and to
    # _deploy_skill_tree's orphan sweep and skipped this third site — found by
    # mechanically enumerating every manifest-driven deletion in this module,
    # not by reading it again.
    candidates = set()
    for recorded in entry.get("created_dirs", []):
        directory = Path(recorded)
        if not _looks_deployed_by_us(directory):
            # Said out loud, like the files loop above. Dropping it silently
            # left the operator with a directory nothing would ever remove and
            # no reason given.
            print(f"  note: refusing to remove {recorded}: not a path this "
                  f"tool deploys to", file=sys.stderr)
            continue
        candidates.add(directory)
    for directory in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        # A crash inside _write_skill_atomically can strand a `.skill.*.tmp`
        # here. Nothing records it, and its mere presence made the rmdir below
        # fail silently — leaving the skill directory behind with no message.
        for stale in directory.glob(".skill.*.tmp"):
            try:
                stale.unlink()
            except OSError:
                continue
            print(f"  removed: {stale}")
        try:
            directory.rmdir()
        except OSError:
            continue
        print(f"  removed: {directory}")


def _host_purge_entries(host_entry) -> list[dict]:
    """Ordered list of legacy/previous entry dicts to purge for one host.

    A v3 flat entry (top-level ``files`` list, i.e. a reinstall) is returned as
    a single entry. A legacy v1/v2 nesting is returned as: the ``__all__``
    entry first (v1 host-level bundle), then every remaining provider sub-entry
    (v2 per-provider installs), so D2's purge ordering is preserved.
    """
    if not isinstance(host_entry, dict) or not host_entry:
        return []
    if isinstance(host_entry.get("files"), list):
        return [host_entry]
    ordered: list[dict] = []
    all_entry = host_entry.get("__all__")
    if isinstance(all_entry, dict):
        ordered.append(all_entry)
    for key in sorted(host_entry):
        # ``modes`` is v4 worker configuration, not a legacy install entry —
        # purging walks files/created_dirs and must never be handed config.
        if key in ("__all__", "modes", "providers"):
            continue
        value = host_entry[key]
        if isinstance(value, dict):
            ordered.append(value)
    return ordered


# ----------------------------------------------------------------------
# per-host worker configuration (schema v4: providers / modes)
# ----------------------------------------------------------------------


def _assignable_modes() -> list[str]:
    # ``resume`` is never assignable: it must reuse the provider of the run
    # it resumes.
    return sorted(mode for mode in policy.MODE_TO_AGENT if mode != "resume")


def host_providers(installs: dict, host: str) -> list[str]:
    entry = installs.get(host)
    if not isinstance(entry, dict):
        return []
    return list(entry.get("providers", []))


def host_modes(installs: dict, host: str) -> dict[str, str]:
    entry = installs.get(host)
    if not isinstance(entry, dict):
        return {}
    return dict(entry.get("modes", {}))


GENERATED_BEGIN = "<!--PILOT_GENERATED_BEGIN-->"
GENERATED_END = "<!--PILOT_GENERATED_END-->"

# The frontmatter is YAML and cannot carry the generated-region markers, so
# the trigger list there is a substitution token, filled at deploy time with
# the host's configured provider keys.
TRIGGER_PLACEHOLDER = "{{PILOT_PROVIDER_TRIGGERS}}"


def render_worker_region(provider_keys: list[str],
                         modes: dict[str, str]) -> str:
    """Render the marker-delimited worker table for a host's deployed skill.

    Pure function: lists exactly ``provider_keys`` (in the user's install
    order) and which modes each is assigned to. An assignment naming a
    provider not in ``provider_keys`` is ignored, so a host never learns
    about a provider it was never given.
    """
    if not provider_keys:
        return ""
    modes_by_provider: dict[str, list[str]] = {key: [] for key in provider_keys}
    for mode in sorted(modes):
        provider = modes[mode]
        if provider in modes_by_provider:
            modes_by_provider[provider].append(mode)
    lines = [
        GENERATED_BEGIN,
        "",
        "## Workers",
        "",
        "| Provider | Handles modes |",
        "| --- | --- |",
    ]
    for key in provider_keys:
        modes = modes_by_provider[key]
        modes_cell = ", ".join(modes) if modes else "—"
        lines.append(f"| {key} | {modes_cell} |")
    lines += ["", GENERATED_END, ""]
    return "\n".join(lines)


def apply_generated_region(skill_text: str, region: str) -> str:
    """Splice ``region`` into ``skill_text`` between the generated markers.

    Pure string function: everything before ``GENERATED_BEGIN`` and after
    ``GENERATED_END`` is returned byte-identical, and exactly one marker pair
    remains in the output. ``region`` may carry its own marker pair (as
    ``render_worker_region`` produces); only its inner content is used, so no
    second pair is emitted. An empty ``region`` clears the generated content
    while keeping the markers and surrounding prose intact.

    Raises ``RuntimeError`` when the marker pair is missing, duplicated, or
    out of order — a packaged skill without a sane marker pair is a packaging
    bug and must fail loudly rather than be silently appended to.
    """
    begin_count = skill_text.count(GENERATED_BEGIN)
    end_count = skill_text.count(GENERATED_END)
    if begin_count == 0 or end_count == 0:
        raise RuntimeError(
            "skill text is missing the generated-region marker pair")
    if begin_count > 1 or end_count > 1:
        raise RuntimeError(
            "skill text contains a duplicate generated-region marker")
    begin_idx = skill_text.index(GENERATED_BEGIN)
    end_idx = skill_text.index(GENERATED_END)
    if end_idx < begin_idx:
        raise RuntimeError(
            "generated-region end marker appears before the begin marker")

    inner = region
    if inner.startswith(GENERATED_BEGIN):
        inner = inner[len(GENERATED_BEGIN):]
    end_pos = inner.rfind(GENERATED_END)
    if end_pos != -1:
        inner = inner[:end_pos]

    before = skill_text[:begin_idx]
    after = skill_text[end_idx + len(GENERATED_END):]
    return before + GENERATED_BEGIN + inner + GENERATED_END + after


# Example requests per mode, spliced into the generated description so the
# trigger has surface area that RESEMBLES real prompts — policy prose shares
# no tokens with an actual request like asking to trace a flow. Bilingual
# because users prompt in Chinese too; only ROUTED modes get their examples
# advertised (the sibling rule to not advertising an unassigned mode's
# routing). No ": " anywhere — the description must stay a valid plain YAML
# scalar. resume is planner-internal recovery, so it has no user phrasing.
MODE_TRIGGER_EXAMPLES: dict[str, tuple[str, ...]] = {
    "explore": ('"how does X work"', '"trace this flow"',
                '"探索/梳理/读一下这块代码"'),
    "code": ('"implement/fix/refactor X"', '"改一下/实现/修复"'),
    "test": ('"run the tests"', '"跑一下测试"'),
    "review": ('"review this change"', '"review 一下这次改动"'),
}


def render_trigger_sentence(provider_keys: list[str],
                            modes: dict[str, str]) -> str:
    """The frontmatter's trigger clause, generated from the host's ROUTING.

    This is the only part of the skill a model sees before deciding whether to
    load it, so it decides what the whole feature can do. Two failure modes are
    on record, both found live:

    - v0.5.2 read "Trigger when the user names a worker provider — ds, kimi-k3",
      the most specific clause in the description and therefore the operative
      one — so "explore this codebase" never loaded the skill, and the
      mode -> provider table (which lives in the BODY) was never read.
    - v0.5.3 routed by mode but left the judgment open: the surrounding static
      text still said "worth delegating ... Not for small tweaks", so a small
      exploration was judged not worth a worker and the skill never loaded.
      Routing is a MANDATE — ``install ... for <mode>`` is the user making the
      delegation decision once; the sentence must close the per-task
      re-judgment, and the only stated override is explicit user input (the
      top of the priority chain: user input, then config, then do-it-yourself
      fallback).

    Also load-bearing: NO unquoted colon-space anywhere in the sentence. The
    clause is spliced into a plain YAML scalar, and a mid-value ": " is a
    scanner error in strict YAML — v0.5.3's "Route by MODE, not by name:
    explore" made the whole deployed frontmatter unparseable. A host with no
    assignments routes nothing and must not claim otherwise.
    """
    names = ", ".join(provider_keys)
    routed: dict[str, list[str]] = {}
    for mode, key in sorted(modes.items()):
        if key in provider_keys:
            routed.setdefault(key, []).append(mode)
    if not routed:
        return (f"No mode is routed on this host yet, so nothing is delegated "
                f"automatically; naming a provider ({names}) triggers this skill.")
    parts = []
    for key, mode_list in sorted(routed.items()):
        head = ", ".join(sorted(mode_list))
        examples = ", ".join(
            ex for m in sorted(mode_list)
            for ex in MODE_TRIGGER_EXAMPLES.get(m, ()))
        suffix = f" (e.g. {examples})" if examples else ""
        parts.append(f"{head} \u2192 {key}{suffix}")
    groups = "; ".join(parts)
    return (f"This host routes work by mode — {groups}. A request for a routed "
            f"kind of work MUST be dispatched through this skill to its "
            f"assigned provider, even when the task looks too small to be "
            f"worth a worker. Never do routed work in this session and never "
            f"weigh whether delegation is worth it — the `install ... for "
            f"<mode>` that created the route already made that decision. The "
            f"route also binds your own mid-task moves — realizing you need "
            f"to read code to understand it, write code, run tests, or "
            f"review a diff means dispatching, not doing it here; only "
            f"verifying a known location (a cited file, a returned diff or "
            f"verdict) stays local. Explicit user input is the only override "
            f"— naming a provider ({names}) selects it, and an explicit "
            f"request to do the work here keeps it here.")


def substitute_trigger_placeholder(skill_text: str,
                                   provider_keys: list[str],
                                   modes: dict[str, str] | None = None) -> str:
    """Fill the frontmatter trigger token.

    Pure string function: a host's deployed skill names exactly the providers
    configured for it, so "use <provider>" reaches the skill without leaking
    the names of providers the host never got.
    """
    return skill_text.replace(
        TRIGGER_PLACEHOLDER, render_trigger_sentence(provider_keys, modes or {}))


def _frontmatter_block(text: str) -> str | None:
    """The leading YAML frontmatter (both ``---`` fences), or None.

    Closing on the first line that is exactly ``---`` is correct, not a
    shortcut: in YAML a bare unindented ``---`` IS a document separator, so it
    cannot occur inside a value. A multi-line value must be indented (block
    scalar) or quoted, and neither matches ``\n---\n``.
    """
    if not text.startswith("---\n"):
        return None
    closing = text.find("\n---\n", len("---\n"))
    if closing == -1:
        return None
    return text[:closing + len("\n---\n")]


def _packaged_skill_text(host: str) -> str:
    return (INTEGRATIONS_DIR / f"{host}-host" / "skills" / "pilot-workers"
            / "SKILL.md").read_text(encoding="utf-8")


def _render_deployed_skill(installs: dict, host: str) -> str:
    """The deployed SKILL.md text, rendered from the PACKAGED template.

    The deployed tree is a build artifact: every render starts from package
    data plus the recorded config, so a packaged doctrine update propagates on
    the next install of any form. It used to rebase on the already-deployed
    file to preserve hand edits — which meant a package upgrade could never
    deliver a body update to an existing deployment, while the frontmatter,
    the generated region and every sibling file were rewritten anyway: half a
    promise, and the delivered half was permanent staleness. Custom doctrine
    belongs in the packaged template, where it survives upgrades properly.

    A packaged template without a marker pair makes ``apply_generated_region``
    raise loudly: that is a packaging bug, never an upgrade state.
    """
    provider_keys = host_providers(installs, host)
    modes = host_modes(installs, host)
    region = render_worker_region(provider_keys, modes)
    text = substitute_trigger_placeholder(
        _packaged_skill_text(host), provider_keys, modes)
    return apply_generated_region(text, region)


def _host_config_entry(installs: dict, host: str) -> dict:
    # Creating an entry for a host with no entry yet is allowed; such an
    # entry has no ``files`` list, so status correctly reads it as
    # not-installed.
    entry = installs.setdefault(host, {})
    entry.setdefault("providers", [])
    entry.setdefault("modes", {})
    return entry


def _require_known_provider(provider_key: str) -> None:
    if provider_key not in providers.PROVIDERS:
        raise RuntimeError(
            f"unknown provider {provider_key!r}; choose from "
            f"{', '.join(sorted(providers.PROVIDERS))}"
        )


def add_host_provider(installs: dict, host: str, provider_key: str) -> bool:
    _require_known_provider(provider_key)
    entry = _host_config_entry(installs, host)
    if provider_key in entry["providers"]:
        return False
    entry["providers"].append(provider_key)
    return True


def remove_host_provider(installs: dict, host: str, provider_key: str) -> bool:
    # Deliberately does NOT validate against providers.PROVIDERS: deleting a
    # provider YAML must not strand its recorded entry forever.
    entry = installs.get(host)
    if not isinstance(entry, dict):
        return False
    changed = False
    visible = entry.get("providers", [])
    if provider_key in visible:
        visible.remove(provider_key)
        changed = True
    defaults = entry.get("modes", {})
    # No default may survive pointing at an invisible provider.
    for mode in [m for m, p in defaults.items() if p == provider_key]:
        del defaults[mode]
        changed = True
    return changed


def set_host_mode(
    installs: dict, host: str, mode: str, provider_key: str
) -> None:
    _require_known_provider(provider_key)
    defaultable = _assignable_modes()
    if mode not in defaultable:
        raise RuntimeError(
            f"cannot assign mode {mode!r}; choose from "
            f"{', '.join(defaultable)}"
        )
    entry = _host_config_entry(installs, host)
    # A default implies visibility.
    if provider_key not in entry["providers"]:
        entry["providers"].append(provider_key)
    entry["modes"][mode] = provider_key


def clear_host_mode(installs: dict, host: str, mode: str) -> bool:
    entry = installs.get(host)
    if not isinstance(entry, dict):
        return False
    defaults = entry.get("modes", {})
    if mode not in defaults:
        return False
    del defaults[mode]
    return True


# ----------------------------------------------------------------------
# asset installer (host-level playbook skill)
# ----------------------------------------------------------------------


def install_host_destination(host: str, target: Path | None) -> Path:
    """Where a host's skill tree lands. Single source for the layout, so a
    caller checking "is it already deployed elsewhere?" cannot drift from the
    code that does the deploying."""
    if host == "claude":
        base = (target or Path.home() / ".claude").resolve()
        return base / "skills" / "pilot-workers"
    if host == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        base = (target or codex_home / "skills").resolve()
        return base / "pilot-workers"
    raise RuntimeError(f"unknown host: {host}")



def install_host(host: str, target: Path | None = None,
                 skill_text: str | None = None) -> dict:
    """Copy the host's pilot-workers skill tree into its skill directory.

    Returns ``{"files": [...], "created_dirs": [...]}``. ``target`` overrides
    the host's default base (claude: ``~/.claude``; codex: ``$CODEX_HOME/
    skills``) so isolation tests never touch the real host config dirs. When
    ``target`` is given it IS the base: claude files land under
    ``<target>/skills/pilot-workers/``, codex under ``<target>/pilot-workers/``.

    ``skill_text`` is the already-rendered SKILL.md. Callers have it before
    this runs, and passing it means the packaged template — placeholder
    frontmatter, empty worker region — is never the text on disk, not even
    between this copy and the render that used to follow it.
    """
    dst = install_host_destination(host, target)

    src = INTEGRATIONS_DIR / f"{host}-host" / "skills" / "pilot-workers"
    if not src.is_dir():
        raise RuntimeError(f"integration source not found: {src}")

    files: list[str] = []
    created_dirs: list[str] = []

    existed_before = dst.exists()
    dst.mkdir(parents=True, exist_ok=True)
    if not existed_before:
        created_dirs.append(str(dst))
    for src_file in sorted(src.rglob("*")):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(src)
        dest_file = dst / rel
        parent = dest_file.parent
        new_parents: list[str] = []
        p = parent
        while not p.exists() and p != dst:
            new_parents.append(str(p))
            p = p.parent
        parent.mkdir(parents=True, exist_ok=True)
        created_dirs.extend(new_parents)
        if rel.name == "SKILL.md" and skill_text is not None:
            # No unlink first: os.replace swaps the name atomically (replacing a
            # symlink as a symlink, which is the same defense the unlink gave),
            # and a failed write then leaves the previous doctrine in place
            # instead of no file at all.
            _write_skill_atomically(dest_file, skill_text)
        else:
            if dest_file.is_symlink() or dest_file.exists():
                dest_file.unlink()
            shutil.copy2(src_file, dest_file)
        files.append(str(dest_file))
    print(f"  installed skill: {host}/pilot-workers/")
    return {"files": files, "created_dirs": created_dirs}


def _record_deployment(entry: dict, result: dict) -> None:
    """Write one ``install_host`` result onto a host entry. The ONLY writer.

    ``created_dirs`` is a UNION, not a replacement: a refresh creates no
    directories (they already exist), so overwriting dropped the record of the
    ones the FIRST install made — and uninstall removes only what it is recorded
    as having created, so they were left behind forever.

    One function because there were two writers and they disagreed: the
    provider-install path unioned and the host-reinstall path replaced. That is
    the fifth time this session a property was fixed at one site and not its
    sibling, so the sites are collapsed rather than fixed twice.
    """
    entry["installed_at"] = datetime.now(timezone.utc).isoformat()
    entry["package_version"] = _package_version()
    entry["files"] = result["files"]
    entry["created_dirs"] = sorted(
        set(entry.get("created_dirs") or []) | set(result["created_dirs"]))


def _deployed_skill_path(entry) -> Path | None:
    """Path of the deployed SKILL.md recorded in a host entry, if any."""
    if not isinstance(entry, dict):
        return None
    for name in entry.get("files", []):
        path = Path(name)
        if path.name == "SKILL.md":
            return path
    return None


def _rerender_deployed_skill(installs: dict, host: str) -> None:
    """Rewrite the host's already-deployed SKILL.md from the current render.

    Full re-render from package data (see ``_render_deployed_skill``), written
    to the RECORDED path — this is the path routine provider install/uninstall
    takes, which must not guess a target directory. Sibling files (modes/*.md)
    are converged by the host-form install; this rewrites the one file whose
    content depends on the recorded config. No-op when the host has no
    deployed skill on record.
    """
    skill = _deployed_skill_path(installs.get(host))
    if skill is None or not skill.is_file():
        return
    # Atomic like every other skill write: a truncating write_text here would
    # leave a crash as a half-written doctrine file nothing detects or repairs.
    _write_skill_atomically(skill, _render_deployed_skill(installs, host))


def _deploy_skill_tree(installs: dict, host: str, target: Path | None) -> None:
    """Deploy/refresh the host's skill tree, rendered from package data.

    Records ``installed_at``/``package_version``/``files``/``created_dirs``
    on the host's manifest entry exactly as the host-install path does. The
    deployed tree is a build artifact: a refresh converges every file to the
    current packaged render, so package upgrades propagate.
    """
    entry = installs[host]
    # A legacy v1/v2 nesting must be cleaned by whichever install touches the
    # host first. Only the host form used to purge, so a provider install over a
    # legacy manifest left the v0.4.0 files on disk and the sub-entries in the
    # manifest — where nothing would look for them again.
    if not isinstance(entry.get("files"), list):
        for legacy in _host_purge_entries(entry):
            _purge_entry(legacy)
        for key in [k for k in entry
                    if k not in ("providers", "modes")]:
            del entry[key]
    # The previously deployed SKILL.md path: not a render input (the render
    # is pure package data + config), but the orphan sweep below must treat
    # the previous location as in-bounds when --target moves a deployment.
    previous_skill = _deployed_skill_path(entry)

    # Render BEFORE anything on disk changes. Copying first and rendering after
    # means a render failure leaves the host with a half-updated skill — or none
    # at all on the purge path. Rendering needs no file, so nothing forces that
    # order.
    rendered = _render_deployed_skill(installs, host)

    previous_files = set(entry.get("files") or [])
    result = install_host(host, target, skill_text=rendered)
    # A file the packaged tree no longer ships would otherwise stay on disk AND
    # drop out of the record, so nothing could ever remove it — the planner would
    # keep reading doctrine from a version this package abandoned.
    #
    # Only inside a pilot-workers skill directory. The paths come from the
    # manifest, and an install that unlinks whatever the manifest names will
    # cheerfully delete an unrelated file if that file is listed — verified by
    # putting one there. Not an attack (the manifest is the user's own private
    # file) but a hand edit, a manifest copied between machines, or a path that
    # has since come to mean something else all end the same way. The previous
    # location counts too: changing --target legitimately moves a deployment.
    allowed_roots = {install_host_destination(host, target)}
    if previous_skill is not None:
        allowed_roots.add(previous_skill.parent)
    for orphan in sorted(previous_files - set(result["files"])):
        path = Path(orphan)
        if not _looks_deployed_by_us(path, allowed_roots):
            print(f"  note: refusing to remove {orphan}: outside this host's "
                  f"skill directory", file=sys.stderr)
            continue
        try:
            os.unlink(orphan)
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"  note: cannot remove stale {orphan}: {exc}", file=sys.stderr)
            continue
        print(f"  removed stale: {orphan}")
    _record_deployment(entry, result)


# ----------------------------------------------------------------------
# grammar (raw argv, before argparse-style handling)
# ----------------------------------------------------------------------


def _strip_target(argv: list[str]) -> tuple[list[str], Path | None]:
    args: list[str] = []
    target: Path | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--target":
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if not value.strip() or value.startswith("--"):
                # A flag is not a directory. `--target --global-key` set the
                # target to "--global-key" AND consumed the flag, so the user got
                # neither a key prompt nor a usable target, and no complaint.
                raise _UsageError("--target requires a directory argument")
            target = Path(value)
            i += 2
        elif arg.startswith("--target="):
            # An empty value is a mistake, not a request: Path("") resolves to
            # the current working directory, so `--target=` silently deployed
            # the skill tree into wherever the user happened to be standing.
            value = arg.split("=", 1)[1]
            if not value.strip() or value.startswith("--"):
                raise _UsageError("--target requires a directory argument")
            target = Path(value)
            i += 1
        else:
            args.append(arg)
            i += 1
    return args, target


def _parse_grammar(argv: list[str], command: str, usage: str) -> dict:
    """Parse post-subcommand argv into an action spec (raises _UsageError).

    Host form:  ``{"host": <host|all>, "target": <Path|None>}``
    Runner form: ``{"kind": "runner", "name": <name>, "target": <Path|None>}``
    Provider forms: ``{"kind": "provider", "provider": <key>, "host": <host>,
    "mode": <mode|None>, "target": <Path|None>}``
    Drop-default form (uninstall only): ``{"kind": "mode", "mode": <mode>,
    "host": <host>}``
    Help form:  ``{"kind": "help"}``
    """
    if argv and argv[0] in ("-h", "--help"):
        return {"kind": "help"}
    args, target = _strip_target(argv)
    global_key = "--global-key" in args
    args = [a for a in args if a != "--global-key"]

    if args and args[0] == "runner":
        if len(args) == 2:
            from pilot_workers.runners import RUNNERS

            name = args[1]
            if name not in RUNNERS:
                raise _UsageError(
                    f"unknown runner: {name} "
                    f"(available: {', '.join(sorted(RUNNERS))})"
                )
            if global_key:
                # Checked HERE, not only in the fall-through below: the runner
                # branch returns first, so `install runner opencode --global-key`
                # consumed the flag, did nothing with it and said nothing. Both
                # ds and kimi reported it.
                raise _UsageError(
                    "--global-key configures one provider's key; it has no "
                    "meaning for 'runner'")
            return {"kind": "runner", "name": name, "target": target}
        raise _UsageError(f"usage: pilot-workers {command} runner <name>")

    if args and args[0] == "key" and command == "uninstall":
        # The API key was the one thing a user could create but not remove
        # without hand-deleting a file under a dot-directory.
        if len(args) != 2:
            raise _UsageError("usage: pilot-workers uninstall key <provider>")
        return {"kind": "key", "provider": args[1]}

    # Uninstall classifies by SHAPE, not by the registry: deleting a provider
    # YAML must not strand its recorded entry. ``<x> on <host>`` can only be the
    # provider form here — ``for <mode> on <host>`` has four tokens and a
    # literal head, and ``runner`` was handled above. Install still validates
    # against the registry, so only the removal direction is permissive.
    # Reserved words are excluded, or a mode-less ``uninstall default on <host>``
    # would silently read as "remove a provider named 'default'" and exit 0.
    looks_like_provider_form = (
        len(args) == 3
        and args[1] == "on"
        and args[2] in HOSTS
        and args[0] not in providers.RESERVED_PROVIDER_KEYS
    )
    # A mode name in the provider slot is an omitted `for`, not a provider. It
    # matched the provider SHAPE above, so `uninstall code on claude` reached the
    # provider handler, printed "code is not recorded on claude" and exited 0 —
    # reading as done while the assignment was still there. Same class the
    # reserved-word exclusion above already guards; mode names were simply not on
    # that list. Deferred to the registry first: nothing reserves a mode name, so
    # a provider MAY legitimately be called `code`, and its recorded entry must
    # stay removable.
    if (command == "uninstall" and len(args) == 3 and args[1] == "on"
            and args[2] in HOSTS
            and args[0] in _assignable_modes()
            and args[0] not in providers.PROVIDERS):
        raise _UsageError(
            f"{args[0]!r} is a mode, not a provider; to drop a mode assignment "
            f"write: uninstall for {args[0]} on {args[2]}")
    if args and (
        args[0] in providers.PROVIDERS
        or (command == "uninstall" and looks_like_provider_form)
    ):
        # install <provider> on <host> [for <mode>]
        # uninstall <provider> on <host>
        if len(args) in (3, 5) and args[1] == "on" and args[2] not in HOSTS:
            if args[2] == "all":
                raise _UsageError(
                    "'on <host>' takes a single host; 'all' is not accepted here"
                )
            raise _UsageError(
                f"unknown host: {args[2]} (available: {', '.join(HOSTS)})")
        if len(args) not in (3, 5) or args[1] != "on":
            # A known provider with the wrong shape after it: say which element
            # is missing rather than reprinting every form and making the
            # operator diff their command against the list.
            raise _UsageError(
                f"expected 'on <host>' after the provider {args[0]!r} "
                f"(available hosts: {', '.join(HOSTS)}); got: "
                f"{' '.join(args) or '(nothing)'}")
        spec = {
            "kind": "provider",
            "provider": args[0],
            "host": args[2],
            "mode": None,
            "target": target,
            "global_key": global_key,
        }
        if len(args) == 5:
            if command != "install" or args[3] != "for":
                raise _UsageError(usage)
            spec["mode"] = args[4]
        return spec

    if global_key:
        # The flag configures ONE provider's key, so it is meaningless without
        # a provider named on the command line.
        raise _UsageError(usage)

    if args and args[0] == "for" and command == "uninstall":
        # uninstall for <mode> on <host>
        if len(args) != 4 or args[2] != "on" or args[3] not in HOSTS:
            raise _UsageError(usage)
        return {"kind": "mode", "mode": args[1], "host": args[3]}

    if len(args) == 1 and args[0] in (*HOSTS, "all"):
        return {"host": args[0], "target": target}

    # Name the offending token before falling back. A wall of usage text makes
    # the operator diff their command against it; "unknown provider 'glmm'" is
    # a two-second fix. Only for shapes we recognise — anything else is a
    # genuine grammar error and still gets the usage.
    if len(args) in (3, 5) and args[1] == "on":
        if args[2] == "all":
            raise _UsageError(
                "'on <host>' takes a single host; 'all' is not accepted here")
        if args[0] not in providers.PROVIDERS:
            raise _UsageError(
                f"unknown provider: {args[0]} "
                f"(available: {', '.join(sorted(providers.PROVIDERS))})")
        if args[2] not in HOSTS:
            raise _UsageError(
                f"unknown host: {args[2]} (available: {', '.join(HOSTS)})")
    if len(args) == 4 and args[0] == "for" and args[2] == "on":
        # `install for <mode> on <host>` — the mode-assignment shape with the
        # provider left out. That word is the whole point of the command, so
        # name it instead of printing the grammar.
        raise _UsageError(
            f"no provider named: 'for {args[1]} on {args[3]}' assigns a mode, "
            f"so it needs one — "
            f"'pilot-workers {command} <provider> on {args[3]} for {args[1]}'"
            if command == "install" else
            f"'{command} for <mode> on <host>' takes exactly that shape; "
            f"got: {' '.join(args)}")
    if len(args) == 1 and args[0] not in (*HOSTS, "all", "runner"):
        # One bare token can be a mistyped host OR a provider missing its
        # `on <host>`. Naming only the host dimension sent anyone who typed
        # `install <provider>` looking for a host called <provider>.
        raise _UsageError(
            f"unrecognized argument: {args[0]} — expected a host "
            f"({', '.join(HOSTS)}, all), 'runner <name>', or "
            f"'<provider> on <host>'")

    raise _UsageError(usage)


# ----------------------------------------------------------------------
# runner branch
# ----------------------------------------------------------------------


def _uninstall_key(provider_key: str) -> int:
    """Delete a provider's API key file. Absent is success, not an error."""
    from pilot_workers.runners import get_runner

    if provider_key not in providers.PROVIDERS:
        print(f"error: unknown provider: {provider_key} "
              f"(available: {', '.join(sorted(providers.PROVIDERS))})",
              file=sys.stderr)
        return 2
    provider = providers.PROVIDERS[provider_key]
    path = get_runner(provider.runner).credential_path(provider)
    if not path.exists():
        print(f"  no API key recorded for {provider_key}")
        return 0
    try:
        path.unlink()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  removed the {provider_key} API key ({path})")
    return 0



def _install_runner(name: str) -> int:
    """Install the named runner's runtime, through the runner's own seams.

    This used to ignore ``name`` entirely: it imported OpenCode's pin, ran
    OpenCode's script and inspected OpenCode's directory, so a second runner
    would have installed the first one.
    """
    from pilot_workers.runners import RUNNERS, get_runner
    from pilot_workers.runners.opencode_runner import clear_version_cache

    if name not in RUNNERS:
        print(f"error: unknown runner: {name}", file=sys.stderr)
        return 2
    runner = get_runner(name)
    script = runner.install_script()
    if script is None:
        print(f"note: runner {name} needs no runtime install")
        return 0
    rc = subprocess.run(["bash", str(script)]).returncode
    if rc != 0:
        return rc
    # D6: a fresh install invalidates any cached --version result.
    clear_version_cache()
    runtime_root = runner.runtime_root()
    pinned = runner.pinned_version
    if runtime_root is not None and runtime_root.is_dir() and pinned:
        for child in sorted(runtime_root.iterdir()):
            if child.name != pinned:
                print(f"note: stale runner version present: {child}")
    return 0


def _report_remaining_artifacts(installs: dict | None = None) -> None:
    """After a host is removed, name what is still on disk and how to remove it.

    Removing a host purges that host's skill and its manifest entry — not the
    provider API keys (they are global by design), the runner binary, or the run
    sandboxes, logs and worktrees. A user who believed 'uninstall <host>' was
    the whole cleanup had no way to discover the rest.
    """
    from pilot_workers.runners import get_runner

    def has_key(provider) -> bool:
        # A provider YAML naming an unregistered runner must not turn the
        # post-uninstall report into a crash.
        try:
            return get_runner(provider.runner).credential_path(provider).is_file()
        except (OSError, RuntimeError, KeyError):
            return False

    lines: list[str] = []
    keys = [key for key, provider in sorted(providers.PROVIDERS.items())
            if has_key(provider)]
    if keys:
        lines.append(f"  API keys ({', '.join(keys)}): "
                     f"pilot-workers uninstall key <provider>")
    from pilot_workers.runners import RUNNERS
    for runner_name in sorted(RUNNERS):
        try:
            root = get_runner(runner_name).runtime_root()
        except (OSError, RuntimeError, KeyError):
            continue
        if root is not None and root.is_dir():
            lines.append(f"  worker runtime ({runner_name}): "
                         f"pilot-workers uninstall runner {runner_name}")
    if any(providers.runs_root(provider).is_dir()
           for provider in providers.PROVIDERS.values()):
        lines.append("  run sandboxes and logs: "
                     "python3 -m pilot_workers.maintain runs --older-than-days 1")
    worktrees = providers.worktrees_root()
    if worktrees.is_dir() and any(worktrees.iterdir()):
        lines.append("  worker worktrees: "
                     "python3 -m pilot_workers.maintain worktrees list")
    # A host still configured is the most consequential survivor of all: it goes
    # on routing work. Named first, and separately, because it is not global
    # state the user chose to keep — it is a deployment they may have forgotten.
    remaining_hosts = sorted(
        host for host in HOSTS if host_providers(installs or {}, host))
    if remaining_hosts:
        print(f"note: still installed: {', '.join(remaining_hosts)} "
              f"(pilot-workers uninstall <host>)")
    if lines:
        print("Still on this machine:")
        for line in lines:
            print(line)


def _uninstall_runner(name: str) -> int:
    """Remove the named runner's runtime. The path comes from the runner.

    Hardcoding OpenCode's directory here meant uninstalling any other runner
    would have deleted OpenCode's.
    """
    from pilot_workers.runners import RUNNERS, get_runner

    if name not in RUNNERS:
        print(f"error: unknown runner: {name}", file=sys.stderr)
        return 2
    runtime_root = get_runner(name).runtime_root()
    if runtime_root is None:
        print(f"note: runner {name} has no runtime to remove")
        return 0
    if not runtime_root.exists():
        print(f"note: no runner install found at {runtime_root}")
        return 0
    for child in sorted(runtime_root.iterdir()):
        print(f"removed: {child}")
    shutil.rmtree(runtime_root)
    print(f"removed: {runtime_root}")
    return 0


# ----------------------------------------------------------------------
# install
# ----------------------------------------------------------------------


def _warn_if_key_missing(provider_key: str, host: str) -> None:
    """A recorded provider with no API key cannot actually run — say so.

    This is a warning, not a failure: recording the routing decision and
    supplying the secret are separate steps the user may take in either order.
    """
    from pilot_workers import credentials

    try:
        configured = credentials.credential_status(provider_key)["configured"]
    except (KeyError, OSError, RuntimeError):
        return
    if not configured:
        print(f"  note: {provider_key} has no API key yet — run: "
              f"pilot-workers install {provider_key} on {host} --global-key")


def _install_provider_on_host(
    provider_key: str, host: str, mode: str | None, target: Path | None,
    global_key: bool = False,
) -> int:
    """Record provider visibility (and optionally a mode default) for a host.

    The deployed skill is a function of configuration: after recording, the
    host's skill tree is deployed/refreshed and the generated worker region
    spliced into the deployed SKILL.md. With ``target`` the tree is deployed
    there; without it only an already-deployed skill (if any) is regenerated
    in place — a config-only record deploys nothing.

    ``global_key`` prompts for the provider's API key FIRST, so a refused or
    empty key leaves nothing recorded. The key belongs to the provider, not the
    host: configured once, it serves every host.
    """
    try:
        if global_key:
            from pilot_workers import credentials

            credentials.configure(provider_key)
        # Under the manifest lock: a concurrent install must not read a copy
        # that misses this change and then write it back out.
        with manifest_transaction() as installs:
            recorded_skill = _deployed_skill_path(installs.get(host))
            # `is None` counts as missing too. A host whose entry has providers
            # but no files list (hand-edited, or copied between machines) hit the
            # early return below, printed "already recorded" and exited 0 without
            # ever deploying - leaving a host that cannot delegate and no
            # indication from the command that anything was wrong.
            skill_missing = (recorded_skill is None
                             or not recorded_skill.is_file())
            added = add_host_provider(installs, host, provider_key)
            if (mode is None and not added and target is None
                    and not skill_missing):
                # Nothing changed: say so and skip the fsync+rename. A MISSING
                # deployed skill is a change worth making, though — re-running
                # the command is the user's only lever, and it must repair.
                print(f"  already recorded: {provider_key} on {host}")
                return 0
            if mode is not None:
                set_host_mode(installs, host, mode, provider_key)
            # The skill exists wherever a provider is configured. ``target`` only
            # overrides WHERE; its absence means the host's default directory, never
            # "skip the deploy" — otherwise the ordinary command would record a
            # routing decision the planner never gets to see.
            existing = _deployed_skill_path(installs.get(host))
            if target is not None and existing is not None and existing.is_file():
                # Redirecting the deploy would replace the recorded file list with
                # the new location, leaving the skill the planner still reads stale
                # and untracked — uninstall could not purge it, status could not see
                # it. Uninstall the host first if the location really must change.
                want = install_host_destination(host, target) / existing.name
                if want.resolve() != existing.resolve():
                    raise RuntimeError(
                        f"{host} is already deployed at {existing.parent}; "
                        f"--target would orphan it. Run "
                        f"'pilot-workers uninstall {host}' first if you mean to move it."
                    )
            # A recorded-but-missing skill counts as "not deployed": re-running
            # the command is the user's only lever, so it has to repair.
            if (target is not None or existing is None
                    or not existing.is_file()):
                _deploy_skill_tree(installs, host, target)
            else:
                _rerender_deployed_skill(installs, host)
            if mode is None:
                if added:
                    print(f"  recorded: {provider_key} on {host}")
                else:
                    print(f"  already recorded: {provider_key} on {host}")
            else:
                print(f"  recorded: {provider_key} on {host}; "
                      f"{mode} -> {provider_key}")
            if not global_key:
                _warn_if_key_missing(provider_key, host)
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        spec = _parse_grammar(argv, "install", INSTALL_USAGE)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if spec.get("kind") == "help":
        print(INSTALL_USAGE)
        return 0
    if spec.get("kind") == "runner":
        if spec.get("target"):
            print("error: --target is not supported for runner installs",
                  file=sys.stderr)
            return 2
        return _install_runner(spec["name"])
    if spec.get("kind") == "provider":
        return _install_provider_on_host(
            spec["provider"], spec["host"], spec["mode"], spec.get("target"),
            spec.get("global_key", False))

    hosts = list(HOSTS) if spec["host"] == "all" else [spec["host"]]
    try:
        manifest_path = _manifest_path()
        with manifest_transaction() as installs:
            for host in hosts:
                previous = installs.get(host)
                if not host_providers(installs, host):
                    # A skill exists only where a worker was configured: with none,
                    # the planner must not see one and does the work itself.
                    is_flat = (isinstance(previous, dict)
                               and isinstance(previous.get("files"), list))
                    if previous and not is_flat:
                        # Legacy v1/v2 files belong to an architecture that no
                        # longer exists, so they are cleaned even though nothing
                        # new is deployed. A current (flat) deployment is left
                        # alone: this is the upgrade path and must not break
                        # working delegation.
                        for entry in _host_purge_entries(previous):
                            _purge_entry(entry)
                        installs.pop(host, None)
                    print(f"{host}: no workers configured — nothing to install.")
                    print(f"  next: pilot-workers install <provider> on {host}")
                    continue
                # Render before destroying: a render failure must not leave the host
                # with a purged skill and nothing to replace it.
                rendered = _render_deployed_skill(installs, host)
                # Migration / reinstall: purge every previous entry for this host
                # (v3/v4 flat reinstall OR legacy v1 __all__ + v2 providers), then
                # write a clean host-level v4 entry.
                for entry in _host_purge_entries(previous):
                    _purge_entry(entry)
                print(f"Installing {host} integrations...")
                result = install_host(host, spec["target"], skill_text=rendered)
                new_entry: dict = {}
                if isinstance(previous, dict):
                    # Carry the previous created_dirs in so the union below keeps
                    # directories an earlier install made and this one found
                    # already present.
                    new_entry["created_dirs"] = previous.get("created_dirs") or []
                _record_deployment(new_entry, result)
                # A plain reinstall must not wipe recorded per-host worker config.
                if isinstance(previous, dict):
                    if isinstance(previous.get("providers"), list):
                        new_entry["providers"] = previous["providers"]
                    if isinstance(previous.get("modes"), dict):
                        new_entry["modes"] = previous["modes"]
                installs[host] = new_entry
                # The deployed skill reflects the recorded configuration; the copy
                # above already wrote the text rendered from package data plus
                # that configuration.
            # The manifest is committed ONCE, when the transaction exits — not
            # per host. A crash mid-``install all`` therefore records no hosts,
            # rather than some: the deployed files of the completed hosts are
            # then unrecorded until the command is re-run, which is idempotent
            # and repairs it — better than a manifest that records half the
            # hosts and looks complete.
            print("Done.")
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ----------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------


def _uninstall_provider_on_host(provider_key: str, host: str) -> int:
    """Remove a provider (and every default naming it) from a host's config.

    While the host still has providers left, the deployed skill stays and its
    worker region is regenerated without the removed provider. Removing the
    LAST provider deletes the skill: the deployed files are purged and the
    host's manifest entry dropped. A config-only entry (nothing deployed) is
    kept, simply left with an empty provider list.
    """
    manifest_path = _manifest_path()
    if not manifest_path.exists():
        print(f"error: no install manifest found at {manifest_path}", file=sys.stderr)
        return 1
    try:
        with manifest_transaction(delete_when_empty=True) as installs:
            if not remove_host_provider(installs, host, provider_key):
                print(f"note: {provider_key} is not recorded on {host}")
                return 0
            entry = installs.get(host)
            if host_providers(installs, host):
                _rerender_deployed_skill(installs, host)
            elif isinstance(entry, dict) and isinstance(entry.get("files"), list):
                # That was the last provider: purge the deployed skill and drop
                # the host's manifest entry entirely.
                _purge_entry(entry)
                del installs[host]
            # A config-only entry means nothing was ever deployed, so there is
            # no skill to delete; the (now empty) entry is left in place and the
            # transaction writes it.
            print(f"  removed: {provider_key} on {host}")
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _uninstall_default_on_host(mode: str, host: str) -> int:
    """Drop one mode default from a host's config, leaving providers intact.

    The provider stays visible; the deployed skill's worker region is
    regenerated so the mode is no longer listed as defaulted.
    """
    if mode not in _assignable_modes():
        # The mode may have been removed from the registry after it was
        # recorded in the manifest. Removal must still work — check
        # whether the manifest actually has this mode before rejecting.
        manifest_path = _manifest_path()
        has_stale = False
        if manifest_path.exists():
            data = _load_manifest(manifest_path)
            entry = data.get("installs", {}).get(host, {})
            has_stale = mode in entry.get("modes", {})
        if not has_stale:
            print(f"error: unknown mode: {mode} "
                  f"(expected one of {', '.join(_assignable_modes())})",
                  file=sys.stderr)
            return 2
    manifest_path = _manifest_path()
    if not manifest_path.exists():
        print(f"error: no install manifest found at {manifest_path}", file=sys.stderr)
        return 1
    try:
        with manifest_transaction(delete_when_empty=True) as installs:
            if clear_host_mode(installs, host, mode):
                _rerender_deployed_skill(installs, host)
                print(f"  removed: {mode} assignment on {host}")
            else:
                print(f"note: no provider assigned to mode {mode!r} on {host}")
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def uninstall_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        spec = _parse_grammar(argv, "uninstall", UNINSTALL_USAGE)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if spec.get("kind") == "help":
        print(UNINSTALL_USAGE)
        return 0
    if spec.get("target"):
        print("error: --target is not supported for uninstall", file=sys.stderr)
        return 2
    if spec.get("kind") == "runner":
        return _uninstall_runner(spec["name"])
    if spec.get("kind") == "provider":
        return _uninstall_provider_on_host(spec["provider"], spec["host"])
    if spec.get("kind") == "mode":
        return _uninstall_default_on_host(spec["mode"], spec["host"])
    if spec.get("kind") == "key":
        return _uninstall_key(spec["provider"])

    hosts = list(HOSTS) if spec["host"] == "all" else [spec["host"]]
    manifest_path = _manifest_path()
    if not manifest_path.exists():
        print(f"error: no install manifest found at {manifest_path}", file=sys.stderr)
        return 1
    purged_any = False
    try:
        with manifest_transaction(delete_when_empty=True) as installs:
            for host in hosts:
                host_entry = installs.get(host)
                if not host_entry:
                    continue
                purged_any = True
                print(f"Uninstalling {host} integrations...")
                # Works on v3 flat installs AND legacy v1/v2 nestings: purge
                # every recorded entry for the host.
                for entry in _host_purge_entries(host_entry):
                    _purge_entry(entry)
                del installs[host]
            if not purged_any:
                print(f"error: no manifest entry for: {', '.join(hosts)}",
                      file=sys.stderr)
                return 1
            survivors = dict(installs)
        print("Done.")
        _report_remaining_artifacts(survivors)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
