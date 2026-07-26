# Mode: review — dispatch playbook

(Read this before every review dispatch. The core SKILL.md holds the
priority chain, the red lines and the verdict contract; this file
holds the craft for this one mode.)

- **Axis-splitting is your judgment call.** Fix 2-4 orthogonal axes
  (correctness/boundary conditions, security, performance, consistency with
  codebase conventions); one instance handles exactly one axis. Write each
  axis as its own self-contained task file.
- **Parallel fanout recipe**: one job per axis, launched together —

  ```bash
  pilot-workers fanout --job <providerA>:review:/tmp/review-correctness-<ts>.md \
                       --job <providerB>:review:/tmp/review-security-<ts>.md
  ```

  (`--job PROVIDER:MODE:TASK_FILE`; mixing providers distributes load across
  quotas.) Each job collects its verdict independently.
- **Aggregation is your job.** Merge and dedupe findings across axes, sort by
  severity, report directly. **Verify every finding you intend to act on,
  before acting** — not a sample of them. A review finding is a claim you will
  answer by editing code, so a false positive turns into a wrong edit; open its
  cited `file:line` and confirm the defect is real. Findings you are NOT acting
  on need no verification: say they are unverified. Findings with no `file:line`
  are untrusted.
- **Try to REFUTE each high before acting on it, and refute its fix too.**
  Reviewers cannot run code (interpreters are denied in review mode), so their
  findings are readings — plausible and sometimes wrong. Run the thing: the
  input against the regex, the race with the lock disabled, the command in a
  scratch copy. A proposed fix is a separate claim and has been wrong even when
  the diagnosis was right. For a high you cannot settle by running something,
  dispatch a *different* provider in review mode to argue against it.
- Review mode cannot edit; fixes go through your planning — small fixes
  yourself, bulk mechanical fixes via a code dispatch.
- Axis too broad ("review the entire repo") → split axes first; unclear diff
  baseline → clarify the two versions being compared first.
