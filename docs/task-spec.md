# Worker Task Contract

Use this template only after Codex has resolved the requirements. Delete unused prompts and keep the contract self-contained.

## Objective

State one concrete outcome.

## Completion Boundaries

- List every observable result required for completion.
- Name the focused command or runtime behavior that proves each result.

## Locked Decisions

- Record fixed behavior, interfaces, versions, and constraints.
- State whether source edits are allowed.

## Required Reading

- Give exact entry paths and relevant callers/callees to inspect before acting.
- Include current tests or fixtures that define existing behavior.

## Allowed Scope

- List files or directories the worker may edit.
- List explicit non-goals and behavior that must remain unchanged.

## Work

Describe the bounded implementation, exploration, validation, or review task. Do not ask the worker to redesign already-settled requirements.

## Verification

- Provide focused commands in execution order.
- Define what a successful result looks like.
- Require failures and exit statuses to be reported verbatim enough for Codex to diagnose.

## Final Report

Require:

1. `STATUS`: complete, incomplete, or blocked.
2. `FILES_CHANGED`: exact paths and purpose, or `none`.
3. `VALIDATION`: commands and outcomes.
4. `REMAINING_RISKS`: unmet boundaries, assumptions, or `none`.

Never include API keys, tokens, cookies, private keys, passwords, or unrelated secret-bearing files.
