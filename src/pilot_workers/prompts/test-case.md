Mode: test-case (edits allowed).

- Generate test cases for the target code described in the task. Write test files that cover the specified scenarios, edge cases, and boundaries.
- Follow the project's existing test conventions: framework, naming, directory structure, assertion style. If the task does not specify a framework, detect from existing tests.
- Each test must be independently runnable and must not depend on execution order.
- These tests target behavior that does NOT exist yet. Self-validate before finishing: run the generated tests and confirm every single one FAILS. A test that passes means it is not testing new behavior — rewrite it until it fails. Zero green tests is the contract.
- Report pre-existing issues you noticed but deliberately did not address in REMAINING_RISKS.

End your report with exactly this block (fill the braces; nothing after the block):

<!--PILOT_RESULT_BEGIN-->
{"status": "<complete|partial|blocked>",
 "files_changed": ["<path>"],
 "validation": {"commands": [{"cmd": "<test command run>", "exit_code": 1,
                              "output_summary": "<N tests, N failed, verbatim>"}],
                "passed": true},
 "remaining_risks": "<unmet boundaries, assumptions, or none>"}
<!--PILOT_RESULT_END-->
