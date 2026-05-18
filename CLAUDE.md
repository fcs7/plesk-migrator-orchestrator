# Plesk Migrator Orchestrator

Orquestrador Python para migração cPanel → Plesk Obsidian.

## Requisitos
- Python 3.8+ (script usa `from __future__ import annotations`, PEP 604, `collections.abc`).
- PyYAML instalado no mesmo Python (`python3.8 -m pip install pyyaml`).
- `run.sh` faz probe automático `python3.{12,11,10,9,8}` → `python3` (CentOS/RHEL/AlmaLinux 8 default `python3` é 3.6).
- Rodar como root no Plesk destino.

## Comandos
- Dry-run: `sudo ./run.sh --config /etc/plesk-migration.yaml --dry-run --skip-install`
- Real: `sudo ./run.sh --config /etc/plesk-migration.yaml --skip-install`
- Validar: `python3.8 -m py_compile plesk_migrator_orchestrator.py`

## Debug do migrator (lado Plesk destino)
Tudo fica em `/usr/local/psa/var/modules/panel-migrator/sessions/<session>/`:
- `debug.log` — master verbose
- `pmm-agent.*/shallow-dump.log` / `configuration-dump.log` — pmm_agent na origem
- `plesk.backup.cpanel.shallow.xml` — enumeração inicial (vazio = bug silencioso na origem)
- `migration-list` — YAML com plans + domains + clients
- `agent-config.cpanel.json` — config do agent

## Armadilhas conhecidas
- **Conta cPanel órfã** (`owner` aponta pra reseller que não existe em `whmapi1 listresellers`): shallow-dump retorna `<resellers/><clients/><domains/>` sem erro. Fix: `whmapi1 modifyacct user=X OWNER=root` (uppercase!). `newowner=` é ignorado silencioso.
- **rsync exit 23 + "dump.xml: No such file"** durante preflight: pmm_agent recebeu seleção vazia e saiu com help. Migration-list provavelmente só tem plans, sem domínios.
- **`max_allowed_packet` destino < origem**: preflight bloqueia. Zero-downtime: `SET GLOBAL max_allowed_packet=...` + drop-in `/etc/my.cnf.d/zz-plesk-migration.cnf`.
- `--skip-infrastructure-checks`: pula bloqueio mas pode quebrar no copy-db. Evitar.
- Limpar sessão pra retry limpo: `rm -rf /usr/local/psa/var/modules/panel-migrator/sessions/migration-session`.
