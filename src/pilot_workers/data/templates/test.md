<!-- pilot-workers task · test mode (read-only, run-only, no fixes). Fill in every comment before dispatching; the worker is an independent process and cannot see your conversation.
     Never include any credentials in this file: API key, token, cookie, private key, password. -->

# Commands

<!-- Exact commands to run and the directory to run them in, one per line. -->

# Known Pre-existing Failures

<!-- Known pre-existing failures (to avoid false positives reported as new issues); write none if there are none. -->

# Output Discipline

1. Only run commands to collect results; do not modify any code or source file.
2. Record for each command: the original command, exit code, and key output; for failures, paste the full error text verbatim.
3. Summarize at the end: passed X / failed Y / skipped Z.
4. On failure, do not attempt fixes — bring the output back as-is. Diagnosis and repair are the main session's job.
