#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERRO: precisa root (sudo)" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERRO: python3 ausente" >&2
  exit 1
fi

if ! python3 -c "import yaml" 2>/dev/null; then
  echo "ERRO: PyYAML ausente. Instale:" >&2
  echo "  pip3 install pyyaml" >&2
  echo "  # ou: yum install python3-pyyaml / apt install python3-yaml" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/plesk_migrator_orchestrator.py" "$@"
