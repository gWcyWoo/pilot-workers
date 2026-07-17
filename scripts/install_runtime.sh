#!/usr/bin/env bash
set -euo pipefail

# The pinned version has a single source of truth: providers.py.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCODE_VERSION="$(python3 "${SCRIPT_DIR}/providers.py")"
if [[ -z "${OPENCODE_VERSION}" ]]; then
  echo "error: could not read PINNED_OPENCODE_VERSION from providers.py" >&2
  exit 1
fi

readonly CODEX_ROOT="${CODEX_HOME:-${HOME}/.codex}"
readonly INSTALL_ROOT="${CODEX_ROOT}/worker-runtime/opencode/${OPENCODE_VERSION}"
readonly OPENCODE_BIN="${INSTALL_ROOT}/node_modules/.bin/opencode"

if ! command -v npm >/dev/null 2>&1; then
  echo "error: npm is required to install the pinned OpenCode runtime" >&2
  exit 1
fi

if [[ -x "${OPENCODE_BIN}" ]]; then
  actual_version="$(${OPENCODE_BIN} --version 2>&1)"
  if [[ "${actual_version}" == "${OPENCODE_VERSION}" ]]; then
    echo "OpenCode ${OPENCODE_VERSION} is already installed at ${OPENCODE_BIN}"
    exit 0
  fi
fi

mkdir -p "${INSTALL_ROOT}"
chmod 700 "${CODEX_ROOT}/worker-runtime" "${CODEX_ROOT}/worker-runtime/opencode" "${INSTALL_ROOT}"

npm install \
  --prefix "${INSTALL_ROOT}" \
  --no-package-lock \
  --no-save \
  --ignore-scripts=false \
  "opencode-ai@${OPENCODE_VERSION}"

if [[ ! -x "${OPENCODE_BIN}" ]]; then
  echo "error: npm completed but did not create ${OPENCODE_BIN}" >&2
  exit 1
fi

actual_version="$(${OPENCODE_BIN} --version 2>&1)"
if [[ "${actual_version}" != "${OPENCODE_VERSION}" ]]; then
  echo "error: expected OpenCode ${OPENCODE_VERSION}, got ${actual_version}" >&2
  exit 1
fi

echo "Installed OpenCode ${OPENCODE_VERSION} at ${OPENCODE_BIN}"
