Mode: test-case (edits allowed).

- Generate test cases for the target code described in the task. Write test files that cover the specified scenarios, edge cases, and boundaries.
- Follow the project's existing test conventions: framework, naming, directory structure, assertion style. If the task does not specify a framework, detect from existing tests.
- Each test must be independently runnable and must not depend on execution order.
- Self-validate before finishing: run the test suite against the generated tests. All new tests must pass. If a test exposes a genuine bug in the target code, mark it as expected-failure with a comment explaining the bug — do not silently skip it.
- Report pre-existing issues you noticed but deliberately did not address in REMAINING_RISKS.

End your report with exactly this block (fill the braces; nothing after the block):

<!--PILOT_RESULT_BEGIN-->
{"status": "<complete|partial|blocked>",
 "files_changed": ["<path>"],
 "validation": {"commands": [{"cmd": "<command run>", "exit_code": 0,
                              "output_summary": "<key output, verbatim>"}],
                "passed": true},
 "remaining_risks": "<unmet boundaries, assumptions, or none>"}
<!--PILOT_RESULT_END-->
