<!-- pilot-workers task · review mode (read-only, no fixes). Fill in every comment before dispatching; the worker is an independent process and cannot see your conversation.
     Never include any credentials in this file: API key, token, cookie, private key, password. -->

# Review Target

<!-- What to review: which files / which diff / which change. Include background context. -->

# Directions

<!-- Review focus areas (correctness/security/performance/consistency...); list 3-5 specific checkpoints for each. -->

# Output Discipline

1. Each finding: `[high|medium|low] topic — argument (cite file:line) — impact — suggested fix`.
2. Sort by severity; no more than 15 findings in total.
3. Review only — do not fix or modify any file.
4. Every cited file:line must have been opened and verified by you.
5. Final line is an overall verdict: "pass" / "blocking issues" / "non-blocking issues".
