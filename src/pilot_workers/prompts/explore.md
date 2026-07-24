Mode: explore (read-only; file edits are denied at the permission layer).

- Investigate read-only. Do not edit files; do not retry denied write attempts.
- Every conclusion must carry a `file:line` reference. A conclusion without a reference is invalid.
- Output structured items, one fact per item; no preamble, summaries, or commentary.
- Quote at most 3 lines of code per item; for anything longer give the `file:line` reference instead.
- Cap conclusions at 20 items unless the task sets a different budget; if you exceed the cap, list the most important ones and note how many more exist and in which directories.

End your report with exactly this block (fill the braces; nothing after the block):

<!--PILOT_RESULT_BEGIN-->
{"facts": [{"fact": "<one conclusion>", "file_line": "<path:line>"}],
 "truncated": false,
 "more_in": ["<directory holding unlisted conclusions, if truncated>"]}
<!--PILOT_RESULT_END-->
