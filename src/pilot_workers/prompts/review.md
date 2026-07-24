Mode: review (read-only; file edits are denied at the permission layer).

- Review read-only. Do not edit files and do not fix anything.
- Report only substantiated findings: each finding needs a severity, an exact `file:line` location, the observed behavior, the concrete impact, and a specific fix direction.
- Do not pad the report with style opinions or unverified suspicions; if something is a hunch, label it as unverified in one line or drop it.

End your report with exactly this block (fill the braces; nothing after the block):

<!--PILOT_RESULT_BEGIN-->
{"overall": "<one-paragraph verdict on the reviewed scope>",
 "severity_counts": {"high": 0, "medium": 0, "low": 0},
 "findings": [{"severity": "<high|medium|low>",
               "file_line": "<path:line>",
               "summary": "<one-sentence problem>",
               "impact": "<one-sentence why-it-matters>",
               "suggested_fix": "<specific fix direction>"}]}
<!--PILOT_RESULT_END-->
