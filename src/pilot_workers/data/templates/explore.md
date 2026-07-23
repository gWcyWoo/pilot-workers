<!-- pilot-workers task · explore mode (read-only). Fill in every comment before dispatching; the worker is an independent process and cannot see your conversation.
     Never include any credentials in this file: API key, token, cookie, private key, password. -->

# Questions

<!-- List of specific questions to answer, one per item; tell the worker what to look for, which keywords to grep, and which leads to follow. -->

# Scope

<!-- Which directories/files to start from; explicitly exclude what does not need to be examined. -->

# Output Discipline

1. Every conclusion must carry a `file:line` reference that you have opened and verified yourself; any conclusion without a reference is invalid.
2. Output structured entries, one fact per item; be concise — no preamble, no summary, no commentary.
3. Do not paste large code blocks — at most 3 lines per quote; for anything longer, give the `file:line` and let the reader look.
4. No more than 20 conclusions in total; if there are more, list the most important and note "X more unlisted, in these directories".
