#!/usr/bin/env python3
"""Hard checks on task text before it leaves the machine.

The task is sent verbatim to a third-party model endpoint, so a secret in it is
exfiltrated the moment a worker starts. The task template used to *ask* the
author not to include credentials; an instruction in a comment is not a control,
and nothing read it. These are the controls.

Three checks — this machine's own configured keys by exact match, generic
credential shapes, and unfilled template placeholders — all refusing loudly
rather than sanitising: silently stripping a secret would leave the author
believing it was delivered.
"""

from __future__ import annotations

import re


# Below this length a "secret" matches ordinary prose, so exact-match scanning
# would reject harmless tasks. Real provider keys are far longer.
MIN_SECRET_LENGTH = 12

# Pieces of the inline-assignment shape, named so the three name-side branches
# below can differ in ONE respect (whether a content hash is excused) without
# three copies of the value side.
_BARE_NOUN = r"(?:token|key|secret|password|passwd)"
_COMPOUND_NOUN = r"(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)"
_ASSIGN = r"\s*[:=]\s*['\"]?"
_NOT_A_HASH = (
    r"(?-i:(?!(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})\b))")
_KEY_VALUE = (
    r"(?=[A-Za-z0-9_\-]{20,})(?=[A-Za-z0-9_\-]*[A-Za-z])"
    r"(?=[A-Za-z0-9_\-]*[0-9])[A-Za-z0-9_\-]{20,}")

# Shapes that are unambiguously key material rather than talk about keys. Each
# must not fire on prose like "read the api_key from the env" — that is the
# difference between a usable guard and one people route around.
_CREDENTIAL_SHAPES = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a PEM private key block"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    # An assignment whose value is a long opaque token: `KEY=sk-...`,
    # `token: "ghp_..."`, `HF_TOKEN=hf_...`. The NAME side matches any word
    # ending in a credential noun rather than an enumerated list: vendors keep
    # inventing env var names (HF_TOKEN, GROQ_API_KEY, REPLICATE_API_TOKEN),
    # and a fixed list of six meant the most common ones sailed through.
    # This is the general net; the prefix shapes below catch bare tokens that
    # appear without an assignment at all.
    # The name prefix REPEATS: `_` is a word character, so `\b` never falls
    # between the segments of `AWS_SECRET_ACCESS_KEY`. A single optional segment
    # matched `HF_TOKEN` but not `AWS_SESSION_TOKEN`, `AZURE_CLIENT_SECRET` or
    # `DATABASE_MASTER_PASSWORD` — the most common cloud credential names of
    # all.
    # Requires >= 20 value chars so `api_key=os.environ[...]` and
    # `password reset` prose do not match, plus at least one letter AND one
    # digit so a repeated-character fixture (`password=aaaa...`) or an English
    # phrase (`password=ResetYourPasswordNow`) is not mistaken for key
    # material. Requiring UPPERCASE, as this once did, excluded the entire
    # class of lowercase-hex keys — which is most of them. The digit
    # requirement does let an all-letter value through (`TOKEN=hf_QwEr...`);
    # the prefix shapes below cover the vendors that issue those, and
    # loosening it here refused ordinary text like
    # `password=please-change-me-before-deploying`.
    # A UUID and a content hash are explicitly NOT credentials: both otherwise
    # matched after `key:`, and a guard that refuses a task for quoting a
    # commit hash is a guard people work around.
    # The exclusions are EXACT hash lengths (md5/sha1/sha256) and
    # case-sensitive: `(?i)` above would otherwise make a blanket
    # `[0-9a-f]{32,}` exclusion swallow real all-hex API keys — a 32-char
    # uppercase key, or a 33-char lowercase one, is not a hash at all.
    # They also apply ONLY after a bare noun. `commit key: <sha1>` is prose
    # about a commit; `API_KEY=<sha1>` is a credential that happens to be 40
    # hex chars, and excusing it because a hash is that shape was a hole big
    # enough to drive a key through. A prefixed (`NGC_KEY=`) or compound
    # (`api_key=`, `access_token:`) name is never prose.
    (re.compile(
        r"(?i)(?:"
        # `NGC_KEY=`, `AWS_SESSION_TOKEN=` — a prefix makes it a variable name.
        r"\b(?:[a-z0-9]+[_-])+" + _BARE_NOUN + r"\b" + _ASSIGN + _KEY_VALUE +
        # `api_key=`, `secret_key:`, `access_token=` — credential nouns outright.
        r"|\b(?:[a-z0-9]+[_-])*" + _COMPOUND_NOUN + r"\b" + _ASSIGN + _KEY_VALUE +
        # `key:`, `token:` alone — here, and only here, a hash is not a secret.
        r"|\b" + _BARE_NOUN + r"\b" + _ASSIGN + _NOT_A_HASH + _KEY_VALUE +
        r")"),
     "an inline credential assignment"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "an API key with an `sk-` prefix"),
    # Stripe uses an UNDERSCORE after the prefix, so the hyphen shape above never
    # matched a live payments credential.
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"), "a Stripe key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "a GitHub personal access token"),
    # Each of the following is a real current format. The prefixes are distinctive
    # enough that a length requirement keeps them off prose that merely names
    # them ("the github_pat convention", "glpat tokens are a GitLab thing").
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
     "a fine-grained GitHub token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b"), "a GitLab access token"),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b"), "a Google API key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{20,}\b"), "a Slack token"),
    # Model-hosting vendors, the ones most likely to appear in a task about
    # this kind of tool. All lowercase-hex-ish, so only the prefix separates
    # them from an ordinary identifier.
    # Google OAuth access tokens are dot-separated, so the assignment shape's
    # value run stops at the first dot after only four characters. The prefix is
    # distinctive enough to match on its own, which is the cheaper fix: adding
    # `.` to the value class would refuse ordinary text like
    # `key: some.module.attribute2`.
    (re.compile(r"\bya29\.[A-Za-z0-9_\-\.]{20,}"), "a Google OAuth access token"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), "a Hugging Face token"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"), "a Groq API key"),
    (re.compile(r"\br8_[A-Za-z0-9]{20,}\b"), "a Replicate API token"),
    # A JWT: three base64url segments. Requires real length in each so ordinary
    # dotted prose cannot match.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
     "a JSON Web Token"),
)

# Template placeholders are MARKED, and only marked ones count. Flagging every
# HTML comment refused real tasks: a hand-written task may legitimately carry a
# mode banner or a note to the reader, and shape alone cannot tell those from an
# unfilled section.
PLACEHOLDER_MARK = "PILOT_FILL"

# A template puts each placeholder on its own line; prose that merely QUOTES one
# does so inline, usually inside backticks or a fenced block. Both are needed:
# anchoring to line start alone still trips a quote that happens to open a line,
# and stripping code alone still trips an unquoted inline mention.
# Leading list bullets and blockquote markers count as line start: a hand-edited
# template puts placeholders inside bullets, and those must still be caught.
_PLACEHOLDER = re.compile(
    r"^[ \t]*(?:[-*+>][ \t]*)*<!--\s*" + PLACEHOLDER_MARK + r"(?:(?!-->).)*-->",
    re.DOTALL | re.MULTILINE)

# Markdown fences come in backtick and tilde flavours; both must be stripped or
# a task quoting the marker in a ~~~ block is refused for quoting it.
_FENCED_BLOCK = re.compile(
    r"^[ \t]*(```|~~~).*?^[ \t]*\1", re.DOTALL | re.MULTILINE)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _without_code(text: str) -> str:
    """Blank out fenced blocks and inline code, preserving line structure.

    Documentation and reviews of this tool must be able to show the markers they
    describe; a guard that refuses the tasks discussing it is a guard people work
    around. Newlines are kept so line anchoring still means what it says.
    """
    def _blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _INLINE_CODE.sub(_blank, _FENCED_BLOCK.sub(_blank, text))


def check_task(task: str, known_secrets: list[str]) -> None:
    """Raise ``RuntimeError`` if ``task`` must not be sent to a worker.

    ``known_secrets`` are this machine's configured provider keys. They are
    matched exactly, which is precise and false-positive free; the shape
    patterns then catch keys this machine does not hold. The raised message
    never contains the offending value.
    """
    for secret in known_secrets:
        if secret and len(secret) >= MIN_SECRET_LENGTH and secret in task:
            raise RuntimeError(
                "refusing to dispatch: the task contains a configured provider "
                "credential. The task is sent verbatim to a third-party model "
                "endpoint — remove the value and reference it by env var instead."
            )

    for pattern, what in _CREDENTIAL_SHAPES:
        if pattern.search(task):
            raise RuntimeError(
                f"refusing to dispatch: the task appears to contain a "
                f"credential — {what}. The task is sent verbatim to a "
                f"third-party model endpoint; remove the value and reference "
                f"it by env var instead."
            )

    if _PLACEHOLDER.search(_without_code(task)):
        raise RuntimeError(
            "refusing to dispatch: the task still has unfilled template "
            "placeholders. The worker cannot see this conversation, so an "
            "unfilled section is simply missing information."
        )
