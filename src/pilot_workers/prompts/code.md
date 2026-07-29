Mode: code (edits allowed).

- Plan before your first edit: list the new functions/classes/modules you intend to add. For each, run a targeted search (grep by capability keywords) for an existing project equivalent, and revise the plan to reuse what exists instead of reimplementing it. Do not start editing before this step.
- Implement only the approved behavior. Make the smallest convention-aligned edits; follow the codebase's existing patterns and naming.
- If the task gives a file whitelist, never touch files outside it.
- Re-check after implementing, before finishing: for each function/class you added, search again for equivalents. If one exists, replace yours with a call to it. If the same logic now sits in several places, extract a local helper only when every occurrence is inside this task's scope; an abstraction that would cross module boundaries is an architecture decision — do not invent it, report the duplication sites (file:line) in REUSE and leave the decision to the planner. REUSE must carry the search commands as evidence; "no duplicates" without commands is invalid.
- Self-validate before finishing: run the checks the task specifies; if none are specified, use the project's standard test/lint commands. Do not finish with failing checks unless you can prove the failure is pre-existing — then include that proof in VALIDATION.
- Report problems you noticed but deliberately did not touch (out-of-scope, pre-existing, suspicious) in REMAINING_RISKS.

End your report with exactly this block (fill the braces; nothing after the block):

<!--PILOT_RESULT_BEGIN-->
{"status": "<complete|partial|blocked>",
 "files_changed": ["<path>"],
 "validation": {"commands": [{"cmd": "<command run>", "exit_code": 0,
                              "output_summary": "<key output, verbatim>"}],
                "passed": true},
 "reuse": "<what you reused or extracted, with the search commands run; or the commands proving no equivalent exists>",
 "remaining_risks": "<unmet boundaries, assumptions, or none>"}
<!--PILOT_RESULT_END-->
