# Mode: explore — dispatch playbook

(Read this before every explore dispatch. The core SKILL.md holds the
priority chain, the red lines and the verdict contract; this file
holds the craft for this one mode.)

Reading code is the bulk of token spend (read-to-write ratio roughly 50:1), so
exploration is a top dispatch candidate. Write the question straight into the
task file; do not read the code yourself first — that wastes the saving. List
what to investigate item by item; pin the scope to directories/file types.

- **Three standard lenses — one dispatch each, fanout in parallel** (read-only
  jobs never collide): (1) **business flow** — how the flow under change
  runs today; (2) **architecture constraints** — for each capability this
  change needs (HTTP, storage, auth, ...), what already exists that the new
  code must fit into; (3) **abstraction evidence** — only when this change
  puts an abstraction question on the table: the duplication sites, how the
  copies differ, and the patterns this project already uses. Every lens is
  scoped by THIS change's needs — never a repo-wide inventory or
  duplication audit. Lens 2 is what puts "reuse `src/net/client.py`" into
  the code task's Interfaces section; lens 3 is what lets YOU decide
  abstract-or-not instead of a worker.
- **The worker is fast but weak — write the requirement in full.** The task
  file must carry the complete context and turn every question into a
  concrete lookup item; the worker must never have to infer what you meant.
  Each sentence you save writing the task becomes a wrong guess made on a
  weaker model.

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
