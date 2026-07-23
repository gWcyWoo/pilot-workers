# Permission Profiles

Drop `.yaml` files here to define custom permission profiles.
Profiles override the built-in mode defaults — unspecified rules keep their defaults.

Requires `pyyaml` (`pip install pyyaml`).

## Usage

Reference a profile by name (filename without `.yaml`):

```bash
# CLI flag (highest priority)
python3 -m pilot_workers.cli.run --permissions relaxed ...

# Or in provider YAML (lower priority, CLI overrides)
# providers/glm.yaml
permissions: relaxed
```

## Format

```yaml
# _all applies to every mode; mode-specific sections merge on top.
# Shell rules: appended after defaults (last-match-wins in OpenCode).
# Tool rules: overwrite defaults.

_all:
  shell:
    "curl *": allow
  tools:
    webfetch: allow

code:
  shell:
    "wget *": allow

explore:
  tools:
    edit: allow
```

Valid top-level keys: `_all`, `code`, `explore`, `test`, `review`.

Each section can contain:
- `shell` — pattern → `allow` or `deny`
- `tools` — tool name → `allow` or `deny`
