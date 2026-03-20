#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build/batocera"
DIST_DIR="${ROOT_DIR}/dist"
ARTIFACT_NAME="vpinleaders-batocera-linux-x86_64.zip"
PYINSTALLER_NAME="vpinleaders-client"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[build-bundle] Missing required command: $1" >&2
    exit 1
  }
}

need_cmd python3
need_cmd pyinstaller
need_cmd zip

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

pyinstaller \
  --noconfirm \
  --clean \
  --onedir \
  --windowed \
  --name "${PYINSTALLER_NAME}" \
  --contents-directory . \
  --add-data "${ROOT_DIR}/assets:assets" \
  --add-data "${ROOT_DIR}/config.example.ini:." \
  --add-data "${ROOT_DIR}/nvram-maps:nvram-maps" \
  --hidden-import="pynput.keyboard._xorg" \
  --hidden-import="pynput.mouse._xorg" \
  "${ROOT_DIR}/main.py"

STAGE_DIR="${BUILD_DIR}/stage"
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"
cp -R "${ROOT_DIR}/dist/${PYINSTALLER_NAME}/." "${STAGE_DIR}/"

(
  cd "${STAGE_DIR}"
  zip -qry "${DIST_DIR}/${ARTIFACT_NAME}" .
)

echo "[build-bundle] Created ${DIST_DIR}/${ARTIFACT_NAME}"
