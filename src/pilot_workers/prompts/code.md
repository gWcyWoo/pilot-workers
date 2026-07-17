Mode: code (edits allowed).

- Implement only the approved behavior. Make the smallest convention-aligned edits; follow the codebase's existing patterns and naming.
- If the task gives a file whitelist, never touch files outside it.
- Self-validate before finishing: run the checks the task specifies; if none are specified, use the project's standard test/lint commands. Do not finish with failing checks unless you can prove the failure is pre-existing — then include that proof in VALIDATION.
- Report problems you noticed but deliberately did not touch (out-of-scope, pre-existing, suspicious) in REMAINING_RISKS.
