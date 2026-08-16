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

Optional fields, each defaulting to the behaviour that existed before it did:

```yaml
permissions: <profile-name>         # a profile from data/permissions/
runner: claude-code                 # execution engine; default `opencode`.
                                    #   `claude-code` drives the `claude` CLI
                                    #   headless, so base_url must be the
                                    #   vendor's ANTHROPIC-compatible endpoint
                                    #   and provider_id/npm/declare are unused
npm: <package>                      # default @ai-sdk/openai-compatible
declare: false                      # the engine's registry already knows
                                    #   provider_id (openai, anthropic) and
                                    #   carries endpoint + model catalogue;
                                    #   base_url is then unnecessary
auth: oauth                         # the engine owns the login flow
auth_method: "<method LABEL>"       # its picker's label, NOT the internal id
effort: high                        # reasoning budget: minimal | low |
                                    #   medium | high | xhigh | max.
                                    #   Omit for a non-reasoning model —
                                    #   sending the option there is an error.
```

Then:
1. Run `pw9 key <key>` to set up the API credential.
2. Run `pw9 status` to verify the credential is configured.
3. Run `pw9 run --provider <key> --mode explore --workdir . --task "hello" --dry-run` to verify routing.

The runner discovers all `.yaml` files in this directory at startup. No Python code changes needed.
