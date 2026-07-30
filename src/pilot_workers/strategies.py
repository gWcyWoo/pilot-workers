"""Strategy configuration: default axes/layers shipped with the package,
user-level overrides stored in $PILOT_WORKERS_HOME/strategies/.

Each mode with a strategy (review, test) has a default JSON in
``data/strategies/<mode>.json`` and an optional user override JSON at
``$PILOT_WORKERS_HOME/strategies/<mode>.json``.  The effective config is:
default entries whose names are not removed by the user, plus any user-added
entries, in order (defaults first, then additions).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pilot_workers.providers import pilot_home

STRATEGIES_DIR = Path(__file__).resolve().parent / "data" / "strategies"

MODE_LIST_KEY = {
    "review": "axes",
    "test": "layers",
}


def _load_default(mode: str) -> list[dict[str, str]]:
    """Load the packaged default strategy for a mode."""
    path = STRATEGIES_DIR / f"{mode}.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    key = MODE_LIST_KEY.get(mode, "axes")
    items = data.get(key, [])
    return [item for item in items if isinstance(item, dict)]


def overrides_path(mode: str) -> Path:
    return pilot_home() / "strategies" / f"{mode}.json"


def load_overrides(mode: str) -> dict[str, Any]:
    """Load user overrides: {added: [...], removed: [name, ...]}."""
    path = overrides_path(mode)
    if not path.is_file():
        return {"added": [], "removed": []}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read strategy overrides: {path}: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"strategy overrides are not valid JSON: {path}: {exc}")
    if not isinstance(data, dict):
        raise RuntimeError(f"strategy overrides must be a JSON object: {path}")
    return {
        "added": data.get("added", []),
        "removed": data.get("removed", []),
    }


def save_overrides(mode: str, overrides: dict[str, Any]) -> Path:
    from pilot_workers import runtime

    path = overrides_path(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.atomic_write_text(
        path, json.dumps(overrides, indent=2) + "\n", mode=0o600,
        prefix=".strategies.")
    return path


def effective(mode: str) -> list[dict[str, str]]:
    """Merge default + user overrides into the final list of items."""
    defaults = _load_default(mode)
    overrides = load_overrides(mode)
    removed = set(overrides.get("removed", []))
    result = [item for item in defaults if item.get("name") not in removed]
    for item in overrides.get("added", []):
        if isinstance(item, dict) and item.get("name"):
            # User addition replaces a default with the same name (edit).
            result = [r for r in result if r.get("name") != item["name"]]
            result.append(item)
    return result


def add_item(mode: str, name: str, focus: str) -> None:
    """Add or replace a user-level item."""
    overrides = load_overrides(mode)
    added = [i for i in overrides["added"]
             if isinstance(i, dict) and i.get("name") != name]
    added.append({"name": name, "focus": focus})
    overrides["added"] = added
    # Adding back something that was removed: un-remove it.
    overrides["removed"] = [n for n in overrides.get("removed", [])
                            if isinstance(n, str) and n != name]
    save_overrides(mode, overrides)


def remove_item(mode: str, name: str) -> bool:
    """Remove an item (default or user-added). Returns True if anything changed."""
    defaults = _load_default(mode)
    overrides = load_overrides(mode)
    default_names = {item["name"] for item in defaults}
    added = [i for i in overrides.get("added", []) if isinstance(i, dict)]
    before = len(added)
    added = [i for i in added if i.get("name") != name]
    overrides["added"] = added
    changed = len(added) < before
    # If it's a default, mark it removed.
    removed = [n for n in overrides.get("removed", []) if isinstance(n, str)]
    if name in default_names and name not in removed:
        removed.append(name)
        overrides["removed"] = removed
        changed = True
    if changed:
        # Clean up: empty overrides → delete the file.
        if not overrides["added"] and not overrides["removed"]:
            path = overrides_path(mode)
            if path.is_file():
                path.unlink()
            return True
        save_overrides(mode, overrides)
    return changed


def edit_item(mode: str, name: str, focus: str) -> None:
    """Edit an existing item's focus. Works for both defaults and user-added."""
    add_item(mode, name, focus)
