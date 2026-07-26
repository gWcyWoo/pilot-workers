# Mode: explore — dispatch playbook

(Read this before every explore dispatch. The core SKILL.md holds the
priority chain, the red lines and the verdict contract; this file
holds the craft for this one mode.)

Reading code is the bulk of token spend (read-to-write ratio roughly 50:1), so
exploration is a top dispatch candidate. Write the question straight into the
task file; do not read the code yourself first — that wastes the saving. List
what to investigate item by item; pin the scope to directories/file types.

- **Planning is the verification — do not sample.** Sampling a couple of
  conclusions proves nothing about the rest and manufactures confidence in it.
  Take the report into planning and let the gaps surface: a broad gap goes back
  as a rewritten explore dispatch, while three or five lines you still need —
  read them here, a round-trip costs more than the read.
- Conclusions carrying no `file:line` are untrusted: you cannot plan on them and
  cannot check them, so treat them as absent.
- Bring conclusions back **with their file:line references, verbatim** —
  losing the references ruins the main thread's planning.
- Judgment/trade-off questions and file changes are your job; split mixed
  explore+change tasks: explore first, plan, then dispatch code.
