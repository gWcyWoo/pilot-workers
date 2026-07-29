# Mode: explore — dispatch playbook

(Read this before every explore dispatch. The core SKILL.md holds the
priority chain, the red lines and the verdict contract; this file
holds the craft for this one mode.)

Reading code is the bulk of token spend (read-to-write ratio roughly 50:1), so
exploration is a top dispatch candidate. Write the question straight into the
task file; do not read the code yourself first — that wastes the saving.

- List what to investigate item by item; pin the scope to specific
  directories/file types to shrink roaming room.
- **Planning is the verification — do not sample.** Sampling a couple of
  conclusions proves nothing about the rest and manufactures confidence in it.
  Instead take the report into planning; the gaps surface as you use it:
  a broad gap (wrong subsystem, missing the leads you need) goes back as a
  rewritten explore dispatch, while three or five lines you still need — read
  them here, a round-trip costs more than the read.
- Conclusions carrying no `file:line` are untrusted: you cannot plan on them and
  cannot check them, so treat them as absent.
- Bring conclusions back **with their file:line references, verbatim** — the
  main thread requires those references to plan; losing them ruins everything.
- Boundary: judgment/trade-off questions ("which approach is better") are your
  job, not the worker's. Mixed explore+change tasks: explore first, plan,
  then dispatch code.
