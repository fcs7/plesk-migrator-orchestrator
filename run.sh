#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERRO: precisa root (sudo)" >&2
  exit 1
fi

# Detecta Python >= 3.8. Prefere binários explícitos antes do "python3" genérico
# (CentOS/RHEL 7-8 podem ter python3 = 3.6 com python3.8 instalado lado-a-lado).
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERRO: Python >= 3.8 ausente. Instale (CentOS/RHEL 8):" >&2
  echo "  yum install python38" >&2
  echo "  # ou: dnf install python3.11" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c "import yaml" 2>/dev/null; then
  echo "ERRO: PyYAML ausente para $PYTHON_BIN. Instale:" >&2
  echo "  $PYTHON_BIN -m pip install pyyaml" >&2
  echo "  # ou pacote do sistema: yum install python3-pyyaml / apt install python3-yaml" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PYTHON_BIN" "$SCRIPT_DIR/plesk_migrator_orchestrator.py" "$@"
