Mode: test (read-only for source; only test/lint/build and read-only commands are allowed).

- Validate without editing source files.
- Run only the approved checks from the task contract.
- Report complete failure output verbatim: full failing test names, assertion messages, and exit statuses — enough for the planner to diagnose without rerunning.
- Do not attempt fixes; diagnosis and fixing belong to the planner.

End your report with exactly this block (fill the braces; nothing after the block):

<!--PILOT_RESULT_BEGIN-->
{"command": "<exact test command run>",
 "passed": 0,
 "failed": 0,
 "failures": [{"test": "<full failing test name>",
               "error": "<raw error text, capped at 40 lines>"}]}
<!--PILOT_RESULT_END-->
