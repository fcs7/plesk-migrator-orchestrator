# Plesk Migrator Orchestrator

Orquestrador Python: cPanel → Plesk Obsidian.

## Requisitos
- Python 3.8+ (`from __future__ import annotations`, PEP 604, `collections.abc`).
- PyYAML no mesmo Python: `python3.8 -m pip install pyyaml`.
- `run.sh` probe `python3.{12,11,10,9,8}` → `python3` (CentOS/RHEL/Alma 8: `python3` = 3.6).
- Root no Plesk destino.

## Comandos
- Dry-run: `sudo ./run.sh --config /etc/plesk-migration.yaml --dry-run --skip-install`
- Real: `sudo ./run.sh --config /etc/plesk-migration.yaml --skip-install`
- Retomar sessão: `sudo ./run.sh --config /etc/plesk-migration.yaml --skip-install --resume`
- Retomar de step nativo: `sudo ./run.sh ... --resume --only-phase transfer --start-from <step>`
- Validar: `python3.8 -m py_compile plesk_migrator_orchestrator.py`

## Debug (Plesk destino)
Path: `/usr/local/psa/var/modules/panel-migrator/sessions/<session>/`
- `debug.log` — master verbose
- `pmm-agent.*/shallow-dump.log`, `configuration-dump.log` — pmm_agent origem
- `plesk.backup.cpanel.shallow.xml` — enumeração (vazio = bug silencioso origem)
- `migration-list` — YAML plans+domains+clients
- `agent-config.cpanel.json` — agent config

## Armadilhas
- **Conta cPanel órfã** (owner = reseller ausente em `whmapi1 listresellers`): shallow-dump retorna `<resellers/><clients/><domains/>` sem erro. Fix: `whmapi1 modifyacct user=X OWNER=root` (uppercase! `newowner=` ignorado silencioso).
- **rsync exit 23 + "dump.xml: No such file"** no preflight: pmm_agent recebeu seleção vazia, saiu com help. Migration-list só plans, sem domínios.
- **`max_allowed_packet` destino < origem**: preflight bloqueia. Zero-downtime: `SET GLOBAL max_allowed_packet=...` + drop-in `/etc/my.cnf.d/zz-plesk-migration.cnf`.
- `--skip-infrastructure-checks`: pula bloqueio, quebra copy-db. Evitar.
- Retry limpo: `rm -rf /usr/local/psa/var/modules/panel-migrator/sessions/migration-session`.
- **`--resume` valida fingerprint SHA-256 do YAML** (salvo em `<session>/.orchestrator-fingerprint` na 1ª execução). Mismatch = aborta com instrução de fix. Restaure YAML original OU descarte sessão.
- **`--resume` ∧ `--force-regenerate` = mutex** (erro em main + __init__).
- **`--start-from` é raw para plesk-migrator**: steps variam por versão. `plesk-migrator transfer-accounts --help` lista válidos. Comuns: `copy-database`, `copy-web-content`, `copy-mail-content`, `deploy-database`, `deploy-domain`, `restore-hosting`.
- **`--start-from` sem `--only-phase transfer`** rejeitado em main (válido só com transfer/all/default).
- **Backup automático em resume**: `migration-list.pre-resume.<UTC-ts>.bak` antes de skip.
- **Idempotência de transfer-accounts**: plesk-migrator nativo rastreia task-id via pmmcli. Re-rodar sem `--start-from` skipa etapas concluídas (conflict-resolve já validado em execução prévia).
- **Docroot vazio pós-migração** (httpdocs/ vazio, public_html/ com 3GB): plesk-migrator deposita conteúdo em `/var/www/vhosts/<dom>/public_html/` (preserva layout cPanel) mas Plesk subscription default `www-root` = `httpdocs/`. Fase `fix-docroot` (entre copy-db e test) chama `plesk bin subscription -u <dom> -www-root .../public_html` por domínio migrado. Idempotente: skipa vhost ausente, public_html vazio, ou httpdocs já populado. Domínios carregados de `<session>/successful-subscriptions.*` (fallback: JSON status/report). Skip: `--skip-fix-docroot`. Rodar isolado: `--only-phase fix-docroot --resume`.
- **Mail vazio pós-migração**: plesk-migrator pode depositar Maildirs em path cPanel-style (`/var/www/vhosts/<dom>/mail/<dom>/<user>/` ou `/var/qmail/mailnames/<dom>/<user>/` sem subpasta `Maildir/`) enquanto Plesk lê `/var/qmail/mailnames/<dom>/<user>/Maildir/{cur,new,tmp}`. Fase `fix-mailpath` (entre copy-mail e check-mail-passwords) audita read-only e grava `<log_dir>/fix-mailpath.log` com mismatches. NÃO move arquivos (correção manual: rsync + `plesk repair mail -domain <dom>`). Lista contas via `plesk bin mail --list -domain <dom>` (cache por sessão). Skip: `--skip-fix-mailpath`.
- **Senhas de e-mail vazias** (psa.accounts.password NULL): hashes cPanel não-compatíveis com armazenamento Plesk → login IMAP/SMTP falha. Fase `check-mail-passwords` audita via `plesk db -Nse <SQL>` join mail+domains+accounts. Reset opcional via `--reset-mail-passwords`: gera `secrets.token_urlsafe(16)` por conta, aplica `plesk bin mail -u <addr> -passwd <pwd>`, grava `<log_dir>/mail-password-reset.csv` (chmod 600, header `timestamp,email,new_password`). Senhas adicionadas a `sensitive_values` para mascarar em logs. Distribuir via canal seguro fora-de-banda. Skip: `--skip-check-mail-passwords`.
