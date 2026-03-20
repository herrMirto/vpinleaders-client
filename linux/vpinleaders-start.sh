#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SCRIPT_DIR}/VPinLeaders-Linux-x64"
APP_BIN="${APP_DIR}/VPinLeaders-Linux-x64"
REQUIRED_CAP="cap_sys_ptrace=eip"

if [[ ! -x "${APP_BIN}" ]]; then
  echo "VPinLeaders binary not found: ${APP_BIN}" >&2
  exit 1
fi

current_cap=""
if command -v getcap >/dev/null 2>&1; then
  current_cap="$(getcap "${APP_BIN}" 2>/dev/null || true)"
fi

if [[ "${current_cap}" != *"${REQUIRED_CAP}"* ]]; then
  if ! command -v setcap >/dev/null 2>&1; then
    echo "setcap is required on Linux for live VPX monitoring." >&2
    echo "Install libcap and run: sudo setcap ${REQUIRED_CAP} ${APP_BIN}" >&2
    exit 1
  fi

  echo "Setting Linux capability for live VPX monitoring..."
  if [[ "$(id -u)" -eq 0 ]]; then
    setcap "${REQUIRED_CAP}" "${APP_BIN}"
  elif command -v sudo >/dev/null 2>&1; then
    sudo setcap "${REQUIRED_CAP}" "${APP_BIN}"
  else
    echo "sudo is not available. Run this manually:" >&2
    echo "setcap ${REQUIRED_CAP} ${APP_BIN}" >&2
    exit 1
  fi
fi

exec "${APP_BIN}" "$@"
