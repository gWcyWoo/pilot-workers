# Provider Definitions

Each `.yaml` file in this directory registers one model provider.

## Adding a New Provider

Create `<key>.yaml` with these required fields:

```yaml
key: <unique-short-name>           # used as --provider argument and directory name
provider_id: <opencode-provider>    # OpenCode provider ID (arbitrary, must be unique)
model_id: <model>                   # model ID sent to the API
base_url: <endpoint>                # official API endpoint (HTTPS only, no relay)
display_name: <human-readable>      # shown in logs and config
context_tokens: <int>               # max context window
output_tokens: <int>                # max output tokens
```

Then:
1. Run `pilot-workers credentials <key>` to set up credentials.
2. Run `pilot-workers run --provider <key> --mode explore --workdir . --task "hello" --dry-run` to verify routing.
3. Add the provider to your host integration (Codex SKILL.md / Claude agent+command).

The runner discovers all `.yaml` files in this directory at startup. No Python code changes needed.
