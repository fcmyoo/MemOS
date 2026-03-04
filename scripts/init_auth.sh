#!/bin/bash
# MemOS Auth Initialization Script
# Usage: bash scripts/init_auth.sh

set -euo pipefail

ENV_FILE=".env"

generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  else
    python - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
  fi
}

echo "== MemOS Auth Initialization =="

if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  POSTGRES_PASSWORD="$(generate_password)"
  echo "POSTGRES_PASSWORD is not set. Generated a random password."
else
  echo "POSTGRES_PASSWORD is already set in current shell."
fi

echo
echo "Generating master key..."
MASTER_OUTPUT="$(python -m memos.api.utils.generate_master_key)"
echo "$MASTER_OUTPUT"
MASTER_HASH_LINE="$(printf '%s\n' "$MASTER_OUTPUT" | grep '^MASTER_KEY_HASH=' | tail -n 1 || true)"

if [[ -z "$MASTER_HASH_LINE" ]]; then
  echo "Failed to parse MASTER_KEY_HASH line from generator output."
  exit 1
fi

echo
echo "Add these lines to ${ENV_FILE}:"
echo "AUTH_ENABLED=true"
echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
echo "${MASTER_HASH_LINE}"
echo
read -r -p "Write these lines to ${ENV_FILE}? [y/N]: " CONFIRM

if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
  {
    echo ""
    echo "# Added by scripts/init_auth.sh"
    echo "AUTH_ENABLED=true"
    echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
    echo "${MASTER_HASH_LINE}"
  } >> "$ENV_FILE"
  echo "Configuration appended to ${ENV_FILE}."
else
  echo "Cancelled. No file changes were made."
fi
