#!/bin/bash
set -euo pipefail

APP_NAME="VPinLeaders"
APP_DIR="/userdata/system/vpinleaders-client/current"
CONFIG_DIR="/userdata/system/configs/vpinleaders-client"
CONFIG_PATH="${CONFIG_DIR}/config.ini"
LOG_DIR="/userdata/system/logs"
SERVICE_DIR="/userdata/system/services"
SERVICE_NAME="VPinLeaders"
SERVICE_PATH="${SERVICE_DIR}/${SERVICE_NAME}"
TMP_DIR="$(mktemp -d /tmp/vpinleaders-install.XXXXXX)"

GITHUB_OWNER="${GITHUB_OWNER:-herrmirto}"
GITHUB_REPO="${GITHUB_REPO:-vpinleaders-client}"
RELEASE_FILE="${RELEASE_FILE:-vpinleaders-batocera-linux-x86_64.zip}"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

log() {
  echo "[vpinleaders-installer] $*" >&2
}

fail() {
  echo "[vpinleaders-installer] ERROR: $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

check_batocera() {
  [[ -d /userdata ]] || fail "This installer is intended for Batocera"
}

check_arch() {
  local arch
  arch="$(uname -m)"
  [[ "${arch}" == "x86_64" ]] || fail "Unsupported architecture: ${arch} (x86_64 only)"
}

download_bundle() {
  local target="${TMP_DIR}/${RELEASE_FILE}"
  local release_url="https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest/download/${RELEASE_FILE}"
  log "GitHub owner: ${GITHUB_OWNER}"
  log "GitHub repo: ${GITHUB_REPO}"
  log "Release file: ${RELEASE_FILE}"
  log "Release URL: ${release_url}"
  log "Downloading latest ${RELEASE_FILE} release asset"
  curl -fL "${release_url}" -o "${target}"
  echo "${target}"
}

install_bundle() {
  local archive="$1"
  local unzip_err="${TMP_DIR}/unzip.stderr"
  rm -rf "${APP_DIR}"
  mkdir -p "${APP_DIR}"
  if ! unzip -oq "${archive}" -d "${APP_DIR}" 2>"${unzip_err}"; then
    cat "${unzip_err}" >&2
    fail "Failed to extract ${RELEASE_FILE}"
  fi
  if [[ -s "${unzip_err}" ]]; then
    if ! grep -Fvx 'lchmod (file attributes) error: Operation not supported' "${unzip_err}" >/dev/null 2>&1; then
      :
    else
      grep -Fvx 'lchmod (file attributes) error: Operation not supported' "${unzip_err}" >&2
    fi
  fi
}

install_service() {
  mkdir -p "${SERVICE_DIR}"
  cat > "${SERVICE_PATH}" <<'EOF'
#!/bin/bash

APP_ROOT="/userdata/system/vpinleaders-client/current"
APP_BIN="${APP_ROOT}/vpinleaders-client"
CONFIG_PATH="/userdata/system/configs/vpinleaders-client/config.ini"
LOG_PATH="/userdata/system/logs/vpinleaders-client.log"
PID_FILE="/var/run/vpinleaders-client.pid"
APP_ARGS=(--headless --config "${CONFIG_PATH}")

start_service() {
  mkdir -p "/userdata/system/configs/vpinleaders-client" "/userdata/system/logs"

  if [[ ! -x "${APP_BIN}" ]]; then
    echo "VPinLeaders binary not found: ${APP_BIN}"
    return 1
  fi

  if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "VPinLeaders config not found: ${CONFIG_PATH}"
    echo "Run registration first:"
    echo "${APP_BIN} --register --machine-id YOUR_MACHINE_ID"
    return 1
  fi

  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "VPinLeaders already running"
      return 0
    fi
    rm -f "${PID_FILE}"
  fi

  cd "${APP_ROOT}" || return 1
  nohup "${APP_BIN}" "${APP_ARGS[@]}" >> "${LOG_PATH}" 2>&1 &
  echo $! > "${PID_FILE}"
  echo "VPinLeaders started"
}

stop_service() {
  if [[ ! -f "${PID_FILE}" ]]; then
    echo "VPinLeaders is not running"
    return 0
  fi

  local pid
  pid="$(cat "${PID_FILE}")"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    sleep 1
    if kill -0 "${pid}" 2>/dev/null; then
      kill -9 "${pid}" 2>/dev/null || true
    fi
  fi
  rm -f "${PID_FILE}"
  echo "VPinLeaders stopped"
}

status_service() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "running"
      return 0
    fi
  fi
  echo "stopped"
  return 1
}

case "${1:-}" in
  start) start_service ;;
  stop) stop_service ;;
  restart) stop_service; start_service ;;
  status) status_service ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
EOF
  chmod +x "${SERVICE_PATH}"
}

print_post_install() {
  log "Install complete"
  log "App dir: ${APP_DIR}"
  log "Service: ${SERVICE_PATH}"
  log "Config path: ${CONFIG_PATH}"
  log ""
  log "Next steps"
  log "1. Register this cabinet:"
  log "   ${APP_DIR}/vpinleaders-client --register --machine-id YOUR_MACHINE_ID"
  log "2. Start the service:"
  log "   ${SERVICE_PATH} start"
}

main() {
  check_batocera
  check_arch
  need_cmd curl
  need_cmd unzip

  mkdir -p "${CONFIG_DIR}" "${LOG_DIR}" "${SERVICE_DIR}"

  local archive
  archive="$(download_bundle)"
  install_bundle "${archive}"
  install_service
  print_post_install
}

main "$@"
