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
SCRIPT="$SCRIPT_DIR/plesk_migrator_orchestrator.py"

# Auto-trigger wizard se ninguém passou --config, --init nem -h, e o
# config padrão /etc/plesk-migration.yaml não existe.
need_wizard=1
for arg in "$@"; do
  case "$arg" in
    --config|--config=*|--init|-h|--help)
      need_wizard=0
      break ;;
  esac
done
if [[ "$need_wizard" -eq 1 && ! -f /etc/plesk-migration.yaml ]]; then
  if [[ -t 0 && -t 1 ]]; then
    printf 'Nenhum config em /etc/plesk-migration.yaml.\n'
    printf 'Iniciar wizard interativo? [Y/n] '
    read -r ans
    case "${ans:-y}" in
      [Yy]*|[Ss]*)
        # NÃO encaminha "$@" — args originais (--dry-run, --skip-install
        # etc.) podem confundir o wizard. Depois do wizard, usuário roda
        # comando real com flags desejadas.
        "$PYTHON_BIN" "$SCRIPT" --init
        rc=$?
        if [[ $rc -ne 0 ]]; then exit "$rc"; fi
        if [[ $# -gt 0 ]]; then
          echo
          echo "Wizard pronto. Rode agora seu comando original:"
          echo "  sudo $0 $*"
        fi
        exit 0
        ;;
      *) echo "Abortado. Use --init ou --config <arquivo>." >&2; exit 1 ;;
    esac
  else
    echo "ERRO: sem --config e sem TTY para wizard. Use --config <arquivo>." >&2
    exit 1
  fi
fi

exec "$PYTHON_BIN" "$SCRIPT" "$@"
