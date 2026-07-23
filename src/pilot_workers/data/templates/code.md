<!-- pilot-workers task · code mode. Fill in every comment before dispatching; the worker is an independent process and cannot see your conversation, so the content must be self-contained.
     Never include any credentials in this file: API key, token, cookie, private key, password.
     The worker always emits a four-section report: STATUS / FILES_CHANGED / VALIDATION / REMAINING_RISKS, which you can reference during acceptance.
     The workspace may contain pre-existing uncommitted changes — that is normal; the worker must not explain, revert, or count them in its change list. -->

# Objective

<!-- Observable completion-result checklist; each item corresponds to one verification command in Verification. -->

# Locked Decisions

<!-- Settled approach/interface/naming the worker must not redesign. If the approach is not yet decided, do not dispatch. -->

# Allowed Scope

<!-- Whitelist of files/directories the worker may touch; explicitly list files that must not be changed. -->

# Known Context

<!-- Entry paths (most important — the key to keeping the worker on track), relevant callers/callees, tests that constrain current behavior, and file:line leads. -->

# Work

<!-- What to change and how; use precise paths — never say "that file". -->

# Verification

<!-- Sub-second verification commands (grep/diff/typecheck/single-file tests), each matching an Objective item one-to-one; leave the heavyweight full test suite to the main session. -->
