#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://github.com/hermes-gadget/smallville.git"
REPO_DIR="${HOME}/smallville"
VENV_DIR="${REPO_DIR}/.venv"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi
command -v uv >/dev/null 2>&1 || { echo "uv was not found after installation" >&2; exit 1; }

if [[ -d "${REPO_DIR}/.git" ]]; then
  git -C "${REPO_DIR}" pull --ff-only
else
  git clone "${REPO_URL}" "${REPO_DIR}"
fi

uv python install 3.9
uv venv --python 3.9 "${VENV_DIR}"
uv pip install -r "${REPO_DIR}/requirements.txt" --python "${VENV_DIR}/bin/python"

if [[ ! -f "${HOME}/.opencode-go.key" ]]; then
  echo "Missing ${HOME}/.opencode-go.key; copy the key before starting Smallville." >&2
  exit 1
fi

for settings in \
  "${REPO_DIR}/environment/frontend_server/frontend_server/settings/local.py" \
  "${REPO_DIR}/environment/frontend_server/frontend_server/settings/base.py"; do
  [[ -f "${settings}" ]] || continue
  if ! grep -q "['\"]smallville.justarobot.uk['\"]" "${settings}"; then
    if grep -q '^ALLOWED_HOSTS = \[\][[:space:]]*$' "${settings}"; then
      sed -i "s/^ALLOWED_HOSTS = \[\].*$/ALLOWED_HOSTS = ['smallville.justarobot.uk']/" "${settings}"
    else
      sed -i "s/^ALLOWED_HOSTS = \[\(.*\)\]$/ALLOWED_HOSTS = [\1, 'smallville.justarobot.uk']/" "${settings}"
    fi
  fi
done

UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "${UNIT_DIR}"
install -m 0644 "${SCRIPT_DIR}/smallville-django.service" "${UNIT_DIR}/smallville-django.service"
install -m 0644 "${SCRIPT_DIR}/smallville-reverie.service" "${UNIT_DIR}/smallville-reverie.service"
systemctl --user daemon-reload
systemctl --user enable smallville-reverie.service smallville-django.service

echo "Smallville setup complete. Start the tunnel first, then reverie and django."
