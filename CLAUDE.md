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
- **Quota mailbox=0 da subscription** (`available: 0` em transfer-accounts): Plesk Migrator cria subscription unplanned com `Limits.mbox_quota=0`. Sem CLI estável (`-mailboxes` rejeitado em unplanned). Fase `fix-limits` (entre transfer e copy-web) faz `UPDATE Limits SET value='-1' WHERE limit_name IN (mbox_quota, max_box, max_subdom, max_db, ...)` por domínio migrado + `plesk repair db -y` pra invalidar cache. Idempotente. Skip: `--skip-fix-limits`.
- **Subscriptions falhando em transfer-accounts** (mailboxes não criadas, sites bloqueados): fase `retransfer-failed` (após fix-limits) auto-reroda `plesk-migrator transfer-accounts --migration-list-file failed-subscriptions.<ts>` até zero falhas, `MAX_RETRANSFER_ATTEMPTS=3` (override `--max-retransfer-attempts N` ou `migration.max_retransfer_attempts` YAML), ou mesma lista 2x consecutivas (raise PhaseExecutionError "progresso estagnado"). Skip: `--skip-retransfer-failed`.
- **Subdomains reservados Plesk** (webmail/mail/ftp/smtp/imap/pop/ns1/ns2 etc): Plesk usa `webmail.<dom>` para webmail nativo; cliente cPanel que tinha esse subdomain bate erro `Incorrect subdomain name: used for accessing webmail`. Fase `sanitize-list` (após `generate-migration-list`) detecta e relata em `<log_dir>/reserved-subdomains-report.csv`. Opt-in `--rename-reserved-subdomains`: reescreve migration-list trocando label conforme `DEFAULT_RESERVED_RENAME` (webmail→correio, mail→email, etc) — override via `migration.reserved_renames` YAML. Backup `migration-list.pre-sanitize.<ts>.bak`. Risco: cliente pode ter URLs hardcoded — por isso opt-in. Skip: `--skip-sanitize-list`.
- **DNS conflicts cPanel-only**: cPanel cria automaticamente registros `cpcontacts.<dom>`, `cpanel.<dom>`, `whm.<dom>`, `webdisk.<dom>` que viram conflito em Plesk (erro `DNS record already exists`). Fase `fix-dns-conflicts` consulta `dns_recs` via `plesk db`, lista hits. Opt-in `--apply-dns-cleanup`: `DELETE FROM dns_recs ... WHERE host REGEXP '^(cpcontacts|cpanel|whm|webdisk)\.'`. Skip: `--skip-fix-dns-conflicts`.
- **Mailbox individual quota=0** (após criação): Plesk-migrator preserva quota cPanel literal (`0` = ilimitado lá, = bloqueio aqui). Conta criada não recebe nada. Fase `fix-mail-quota` (após `check-mail-passwords`) faz `UPDATE mail SET mbox_quota=-1 WHERE dom_id IN (...) AND mbox_quota=0`. Idempotente. Skip: `--skip-fix-mail-quota`.
- **FTP users renomeados silenciosamente** (`user@dom` → `user_dom`): Plesk rejeita `@` em login → renomeia. Cliente não sabe; apps com FTP hardcoded quebram. Fase `fix-ftp-renames` (audit-only) parsea `accounts_report_tree.*` mais recente, regex `Login of FTP user 'X' does not conform to Plesk rules. It was changed to 'Y'`, grava `<log_dir>/ftp-renames.csv` (header `timestamp,original_login,new_login,domain`). Dedup por (original, new). Skip: `--skip-fix-ftp-renames`.
- **fix-mailpath apply mode**: além de auditar (default), `--apply-mailpath-fix` rsync `-a` do path alternativo populado para canonical `/var/qmail/mailnames/<dom>/<user>/Maildir/` + `plesk repair mail -domain <dom> -y`. Não remove origem (operador inspeciona antes). Idempotente (skipa se canonical já populado).
- **Helper `_run_plesk_db(sql, *, fetch=False)`**: executa `plesk db -Nse`. Em dry_run loga e retorna ''. Raise `PhaseExecutionError` em rc≠0. Use pra `UPDATE`/`DELETE` em todas as fases SQL. `_sql_escape()` escapa quote única (suporte minimal — só pra nomes de domínio/limit_name).
- **PHASES_ORDER definitivo**: sanity-check → install → config → list → sanitize-list → filter → preflight → transfer → fix-limits → retransfer-failed → copy-web → copy-mail → fix-mailpath → check-mail-passwords → fix-mail-quota → fix-ftp-renames → fix-dns-conflicts → copy-db → fix-docroot → test → cleanup-config.
