# Spec: Plesk Migrator Orchestrator (cPanel → Plesk Obsidian)

> Documento de implementação consolidado a partir de três revisões paralelas
> (critic, architect, document-specialist). Aplica correções bloqueantes
> identificadas pelo critic e validadas contra documentação oficial Plesk
> ([docs.plesk.com/en-US/obsidian/migration-guide](https://docs.plesk.com/en-US/obsidian/migration-guide/migrating-via-the-command-line.75722/)).
>
> Base: `docs/plan.md` (decisões já confirmadas pelo usuário).

---

## 1. Correções obrigatórias vs. plan.md

Aplicar **antes** de implementar — derivadas da revisão crítica + validação documental.

| # | Item | Plano original | Correção aplicada | Fonte |
|---|------|----------------|---------------------|-------|
| 1 | Comando único `copy-content` | Fase 6 chamava `plesk-migrator copy-content` | **Não existe**. Plesk Migrator expõe três comandos separados: `copy-web-content`, `copy-mail-content`, `copy-db-content`. Substituir fase única por três sub-fases (web/mail/db), executáveis ou puláveis individualmente. | docs.plesk.com migrating-via-the-command-line |
| 2 | Install via `plesk bin extension --install` | Plano usava `plesk bin extension --install panel-migrator` | **Não documentado oficialmente** para `panel-migrator`. Método oficial: `plesk installer --select-release-current --install-component panel-migrator`. Manter `--list` via `/usr/local/psa/bin/extension --list` para detecção. | installation-and-prerequisites.75498 |
| 3 | Sem timeouts em `_run()` | Plano não definia timeout | Adicionar `timeout` por fase: install=600s, generate-list=3600s, transfer-accounts=14400s (4h), copy-*-content=14400s cada, test-all=7200s. | (engenharia) |
| 4 | Stderr não tratado | Plano só checava `returncode` | `subprocess.Popen(..., stderr=subprocess.STDOUT, text=True)` + parser linha-a-linha + mascaramento aplicado também a stderr. | (engenharia) |
| 5 | Sem idempotência em `generate_migration_list` | Re-rodar sobrescreve edições do usuário | Detectar `migration-list` existente; abortar com mensagem clara a menos que `--force-regenerate`. | (critic) |
| 6 | Sem lock file | Duas execuções simultâneas corromperiam sessão | `fcntl.flock` em `/var/lock/plesk-migration-orchestrator.lock` durante `run_all()`. Liberar em `finally`. | (critic) |
| 7 | Sem signal handler | CtrlC durante transfer deixa subprocess órfão | `signal.signal(SIGINT/SIGTERM, _cleanup)` que mata subprocess filho via `Popen.terminate()` → `kill()` após grace period. | (critic) |
| 8 | Cleanup config.ini ausente | Senha cPanel fica em disco pós-migração | Flag `--cleanup-config` que apaga `config.ini` no fim. Não automático. README orienta. | (critic) |
| 9 | Dry-run grava arquivos | `generate_config_ini` escrevia em disco mesmo em dry-run | Dry-run pula **todas** escritas em filesystem do Plesk. Apenas loga conteúdo que seria escrito (mascarado). | (critic) |
| 10 | Sem pre-flight checks | Falha tarde se SSH não conecta / espaço cheio | Método `preflight_checks()` chama `plesk-migrator check` (comando oficial documentado) antes de `transfer-accounts`. | docs.plesk.com (comando `check`) |

---

## 2. Layout final de arquivos

```
/home/fcs/Documents/opiniao/
├── plesk_migrator_orchestrator.py   # ~600 linhas, classe + main + argparse
├── run.sh                            # ~40 linhas, wrapper bash
├── config.example.yaml               # template input com comentários
├── docs/
│   ├── plan.md                       # decisões de alto nível (existe)
│   └── spec.md                       # ESTE arquivo
└── README.md                         # quick start, troubleshooting, segurança
```

---

## 3. Constantes (top-of-module)

```python
DEFAULT_PLESK_MIGRATOR_BIN = "/usr/local/psa/admin/sbin/modules/panel-migrator/plesk-migrator"
DEFAULT_CONF_DIR           = "/usr/local/psa/var/modules/panel-migrator/conf"
DEFAULT_SESSIONS_DIR       = "/usr/local/psa/var/modules/panel-migrator/sessions"
DEFAULT_SESSION_NAME       = "migration-session"
DEFAULT_LOG_DIR            = "/var/log/plesk-migration-orchestrator"
PLESK_EXTENSION_BIN        = "/usr/local/psa/bin/extension"
LOCK_FILE                  = "/var/lock/plesk-migration-orchestrator.lock"

# Timeouts (segundos)
TIMEOUT_INSTALL        = 600     # 10 min
TIMEOUT_GENERATE_LIST  = 3600    # 1 h
TIMEOUT_CHECK          = 1800    # 30 min
TIMEOUT_TRANSFER       = 14400   # 4 h
TIMEOUT_COPY_CONTENT   = 14400   # 4 h cada (web/mail/db)
TIMEOUT_TEST_ALL       = 7200    # 2 h

SENSITIVE_KEY_PATTERN = re.compile(
    r"(ssh[_-]password|postgres[_-]password)\s*[:=]\s*['\"]?([^'\"\s]+)['\"]?",
    re.IGNORECASE,
)
```

---

## 4. Hierarquia de exceções

```python
class PleskMigrationError(Exception): ...
class ValidationError(PleskMigrationError): ...
class PreflightError(PleskMigrationError): ...
class PhaseExecutionError(PleskMigrationError): ...
class LockError(PleskMigrationError): ...
```

---

## 5. Schema YAML (validável)

```yaml
source:                       # OBRIGATÓRIA
  host: str                   # OBRIGATÓRIO
  ssh_port: int (default 22)
  ssh_password: str           # OBRIGATÓRIO
  postgres_password: str | null

dest:                         # OBRIGATÓRIA
  host: str                   # OBRIGATÓRIO

migration:                    # opcional
  allowlist: list[str]        # default []
  denylist: list[str]         # default []

paths:                        # opcional — null = default Plesk
  plesk_migrator_bin: str | null
  conf_dir: str | null
  sessions_dir: str | null
  session_name: str           # default "migration-session"
  log_dir: str                # default DEFAULT_LOG_DIR

behavior:                     # opcional — CLI sobrescreve
  dry_run: bool               # default false
  skip_install: bool          # default false
  force_regenerate: bool      # default false
  cleanup_config: bool        # default false
  skip:
    web_content: bool         # default false
    mail_content: bool        # default false
    db_content: bool          # default false
```

`_validate_config()`:
- Verifica chaves obrigatórias presentes e não-vazias.
- Tipos: `host` string, `ssh_port` int 1..65535, listas são listas, etc.
- Erros amigáveis: `"source.ssh_password é obrigatório"`.

---

## 6. Assinaturas (classe `PleskMigrationOrchestrator`)

```python
class PleskMigrationOrchestrator:
    def __init__(self, config: dict, *,
                 dry_run: bool = False,
                 force_regenerate: bool = False,
                 cleanup_config: bool = False) -> None: ...

    # helpers
    def _validate_config(self) -> None: ...
    def _setup_logger(self) -> logging.Logger: ...
    def _mask(self, text: str) -> str: ...
    def _run(self, cmd: list[str], *,
             check: bool = True,
             log_to: pathlib.Path | None = None,
             timeout: int | None = None,
             input_text: str | None = None) -> subprocess.CompletedProcess: ...
    def _acquire_lock(self) -> None: ...
    def _release_lock(self) -> None: ...
    def _install_signal_handlers(self) -> None: ...
    def _cleanup_subprocess(self, signum=None, frame=None) -> None: ...

    # fases
    def ensure_plesk_migrator_installed(self) -> None: ...
    def generate_config_ini(self) -> pathlib.Path: ...
    def preflight_checks(self) -> None: ...
    def generate_migration_list(self) -> pathlib.Path: ...
    def filter_migration_list(self,
                              allowlist: list[str] | None = None,
                              denylist: list[str] | None = None) -> None: ...
    def transfer_accounts(self, *,
                          skip_web: bool = False,
                          skip_mail: bool = False,
                          skip_db: bool = False) -> None: ...
    def copy_web_content(self) -> None: ...
    def copy_mail_content(self) -> None: ...
    def copy_db_content(self) -> None: ...
    def test_all(self) -> None: ...
    def cleanup_config_ini(self) -> None: ...

    # pipeline
    def run_all(self, *,
                skip_install: bool = False,
                only_phase: str | None = None) -> None: ...
```

---

## 7. Detalhes por fase

### 7.1 `ensure_plesk_migrator_installed()`

```
1. _run([PLESK_EXTENSION_BIN, "--list"], check=False)
2. Se "panel-migrator" in stdout.lower() → log "já instalado", return
3. Senão:
   - log "instalando panel-migrator via plesk installer"
   - _run(["plesk", "installer", "--select-release-current",
           "--install-component", "panel-migrator"], timeout=TIMEOUT_INSTALL)
4. Re-verifica via --list; se ainda ausente → PreflightError
```

### 7.2 `generate_config_ini()`

```
1. self.conf_dir.mkdir(parents=True, exist_ok=True)
2. ConfigParser:
   [GLOBAL]
     source-type = cpanel
     source-servers = cpanel
     target-type = plesk
   [plesk]
     ip = dest.host
     os = unix
   [cpanel]
     ip = source.host
     os = unix
     ssh-password = source.ssh_password
     [se source.ssh_port != 22] ssh-port = N
     [se source.postgres_password] postgres-password = ...
3. config_path = conf_dir/"config.ini"
4. Se dry_run: log conteúdo mascarado, return path simbólico, NÃO escreve
5. Senão:
   - config_path.unlink(missing_ok=True)
   - config_path.touch(mode=0o600)
   - escreve com cfg.write(f)
   - os.chmod(config_path, 0o600) reforçado
6. Log "config.ini criado em {path} (chmod 600)"
```

### 7.3 `preflight_checks()`

Executa o comando oficial `plesk-migrator check` que valida SSH, espaço em
disco, versão da origem, etc.

```
_run([plesk_migrator_bin, "check"], timeout=TIMEOUT_CHECK,
     log_to=log_dir/"preflight.log")
Returncode != 0 → PreflightError
```

### 7.4 `generate_migration_list()`

```
1. session_dir = sessions_dir / session_name
2. migration_list = session_dir / "migration-list"
3. Se migration_list.exists() e not self.force_regenerate:
     raise PhaseExecutionError(
       f"migration-list existe em {migration_list}. "
       "Use --force-regenerate para sobrescrever."
     )
4. _run([bin, "generate-migration-list"], timeout=TIMEOUT_GENERATE_LIST,
        log_to=log_dir/"generate-migration-list.log")
5. Valida que arquivo foi criado, conta linhas, loga
6. return migration_list
```

### 7.5 `filter_migration_list(allowlist, denylist)`

```
1. Se ambas listas vazias → log e return (no-op).
2. backup_path = migration_list.with_suffix(".bak")
   shutil.copy2(migration_list, backup_path)
3. Lê linhas, para cada:
   - vazia OU começa com "#" → mantém
   - senão: domain = first_token.lower()
     keep = (not allowlist or domain in allowlist_lower)
            and (domain not in denylist_lower)
     kept → mantém + count_kept++
     senão → removed.append(domain)
4. Se count_kept == 0 → restaura backup e raise PhaseExecutionError
5. Escreve filtered_lines de volta
6. Log: "{kept}/{initial} domínios mantidos. Removidos: {sample}…"
```

### 7.6 `transfer_accounts()` (com skip flags)

```python
cmd = [bin, "transfer-accounts"]
if skip_web:  cmd.append("--skip-copy-web-content")
if skip_mail: cmd.append("--skip-copy-mail-content")
if skip_db:   cmd.append("--skip-copy-db-content")
_run(cmd, timeout=TIMEOUT_TRANSFER, log_to=log_dir/"transfer-accounts.log")

# EXTEND: --migration-list-file <path>
# EXTEND: --skip-services-checks
```

### 7.7 `copy_web_content() / copy_mail_content() / copy_db_content()`

Três métodos triviais para **re-sincronizar** após `transfer-accounts`:

```
_run([bin, "copy-web-content"],  timeout=TIMEOUT_COPY_CONTENT, log_to=…/copy-web.log)
_run([bin, "copy-mail-content"], timeout=TIMEOUT_COPY_CONTENT, log_to=…/copy-mail.log)
_run([bin, "copy-db-content"],   timeout=TIMEOUT_COPY_CONTENT, log_to=…/copy-db.log)
```

Cada um pulável via YAML `behavior.skip.{web,mail,db}_content`.

### 7.8 `test_all()`

```
_run([bin, "test-all"], timeout=TIMEOUT_TEST_ALL, log_to=log_dir/"test-all.log")
```

### 7.9 `cleanup_config_ini()`

Apaga `config.ini` se `self.cleanup_config`. Loga "config.ini removido — senha
cPanel não persiste em disco".

---

## 8. `_run()` — contrato

```python
def _run(self, cmd, *, check=True, log_to=None, timeout=None, input_text=None):
    """
    - Loga linha de comando mascarada via _mask().
    - Se self.dry_run e cmd modifica estado → log [DRY-RUN], retorna fake CP.
      Exceções imutáveis (rodam mesmo em dry-run):
        ["extension", "--list"], ["extension", "--info"],
        ["plesk-migrator", "help"], ["plesk-migrator", "check"]
    - subprocess.Popen com:
        stdout=PIPE, stderr=STDOUT, text=True, bufsize=1
    - self._current_proc = proc  (p/ signal handler matar)
    - Stream linha-a-linha:
        for line in proc.stdout:
            masked = self._mask(line.rstrip())
            self.logger.debug(masked)
            if log_to: log_to_fh.write(masked + "\n")
    - proc.wait(timeout=timeout) — TimeoutExpired → proc.kill() + raise
    - if check and proc.returncode != 0 → CalledProcessError(masked output)
    - return CompletedProcess
    """
```

---

## 9. Pipeline `run_all()` ordenado

```
1. _acquire_lock()
2. _install_signal_handlers()
3. try:
     phases = [
        ("install",   ensure_plesk_migrator_installed,  not skip_install),
        ("config",    generate_config_ini,              True),
        ("preflight", preflight_checks,                 True),
        ("list",      generate_migration_list,          True),
        ("filter",    filter_migration_list,            allowlist or denylist),
        ("transfer",  transfer_accounts,                True),
        ("copy-web",  copy_web_content,                 not skip.web_content),
        ("copy-mail", copy_mail_content,                not skip.mail_content),
        ("copy-db",   copy_db_content,                  not skip.db_content),
        ("test",      test_all,                         True),
        ("cleanup",   cleanup_config_ini,               self.cleanup_config),
     ]
     se only_phase → roda apenas essa
     senão → roda todas as enabled em ordem
   finally:
     _release_lock()
```

`--only-phase` choices:
`install, config, preflight, list, filter, transfer, copy-web, copy-mail, copy-db, test, cleanup, all`.

---

## 10. CLI `main()` — argparse

```
--config PATH              (obrigatório)
--dry-run                  flag
--skip-install             flag
--force-regenerate         flag (sobrescreve migration-list)
--cleanup-config           flag (apaga config.ini no fim)
--only-phase {…,all}       default "all"
--verbose                  ativa DEBUG em stdout
--skip-web-content         flag → --skip-copy-web-content em transfer-accounts
--skip-mail-content        flag
--skip-db-content          flag
```

Flags CLI sobrescrevem `behavior.*` do YAML.

Exit codes:
- `0` sucesso
- `1` erro de validação YAML
- `2` pré-flight falhou
- `3` fase do plesk-migrator falhou
- `4` lock indisponível (outra instância)
- `130` SIGINT (convenção POSIX)

---

## 11. Logging

- Logger root: `plesk_migrator_orchestrator`
- Handlers:
  - `RotatingFileHandler(orchestrator.log, maxBytes=10MB, backupCount=5)` — DEBUG
  - `StreamHandler(stdout)` — INFO (DEBUG se `--verbose`)
- Per-fase logs em `log_dir/`:
  - `generate-migration-list.log`, `preflight.log`, `transfer-accounts.log`,
    `copy-web.log`, `copy-mail.log`, `copy-db.log`, `test-all.log`
- Formato: `%(asctime)s [%(levelname)s] %(message)s` com
  `datefmt="%Y-%m-%dT%H:%M:%S"`
- Mascaramento:
  - Lista interna `self.sensitive_values` com `ssh_password` e
    `postgres_password` literais
  - `_mask(text)`:
    1. Substitui cada valor sensível literal por `***`
    2. Aplica `SENSITIVE_KEY_PATTERN.sub(...)` para pegar
       `ssh_password=...` em texto livre
- **Aplicado a stdout E stderr** (stream merged via `stderr=STDOUT`).

---

## 12. Lock file + signal handlers

```python
def _acquire_lock(self):
    self._lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise LockError(f"Outra instância rodando ({LOCK_FILE})")

def _release_lock(self):
    if self._lock_fd:
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)

def _install_signal_handlers(self):
    signal.signal(signal.SIGINT,  self._cleanup_subprocess)
    signal.signal(signal.SIGTERM, self._cleanup_subprocess)

def _cleanup_subprocess(self, signum, frame):
    self.logger.warning(f"Sinal {signum} recebido. Encerrando subprocess…")
    if self._current_proc and self._current_proc.poll() is None:
        self._current_proc.terminate()
        try:
            self._current_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._current_proc.kill()
    self._release_lock()
    sys.exit(130)
```

---

## 13. `run.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "ERRO: precisa root (sudo)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERRO: python3 ausente" >&2; exit 1; }
python3 -c "import yaml" 2>/dev/null || {
  echo "ERRO: PyYAML ausente. Instale: pip3 install pyyaml OU yum install python3-pyyaml" >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/plesk_migrator_orchestrator.py" "$@"
```

Permissão: `chmod +x run.sh` após criar.

---

## 14. `config.example.yaml`

```yaml
# Plesk Migrator Orchestrator — exemplo
# Após editar: chmod 600 /etc/plesk-migration.yaml

source:
  host: 192.0.2.10
  ssh_port: 22
  ssh_password: "TROQUE_AQUI"     # sensível — não commitar
  postgres_password: null

dest:
  host: 192.0.2.20

migration:
  allowlist: []
  denylist:  []

paths:
  plesk_migrator_bin: null
  conf_dir: null
  sessions_dir: null
  session_name: migration-session
  log_dir: /var/log/plesk-migration-orchestrator

behavior:
  dry_run: false
  skip_install: false
  force_regenerate: false
  cleanup_config: false
  skip:
    web_content: false
    mail_content: false
    db_content: false
```

---

## 15. `README.md` — seções obrigatórias

1. **Quick Start** (cp + chmod + edit + dry-run + run completo)
2. **Pré-requisitos** (Plesk Obsidian, root, Python 3.8+, PyYAML, SSH origem→destino)
3. **Instalação** (clone + chmod +x run.sh + cp config + chmod 600)
4. **Uso** (dry-run, pipeline completo, fases isoladas via `--only-phase`, `--verbose`)
5. **Fases** (install → config → preflight → list → filter → transfer → copy-web/mail/db → test → cleanup)
6. **Troubleshooting**:
   - Logs em `/var/log/plesk-migration-orchestrator/*.log`
   - `tail -f` em sessão paralela durante transfer
   - Restaurar migration-list do `.bak`
   - `plesk-migrator help <cmd>` p/ flags avançadas
7. **Segurança**:
   - Senha cPanel em texto plano no YAML é exigência Plesk
   - `chmod 600` mandatório
   - Logs mascararam senhas automaticamente
   - Usar `--cleanup-config` ao final p/ apagar `config.ini`
8. **Limitações** (escopo fora: SSH-key, multi-origem, rollback, UI)

---

## 16. Sequência de implementação (subagent-driven)

| Task | Arquivo | Depende | Done quando |
|------|---------|---------|-------------|
| T1  | `plesk_migrator_orchestrator.py` skeleton | — | `python3 -m py_compile` passa |
| T2  | `_validate_config()` + YAML load em `main()` | T1 | Carregar `config.example.yaml` sem erro |
| T3  | `_setup_logger()` + `_mask()` | T1 | Log file + stdout; senha aparece como `***` |
| T4  | `_run()` wrapper (dry-run, timeout, stream, mask) | T3 | `ls` loga mascarado; dry-run não executa |
| T5  | Lock + signal handlers | T1 | 2ª execução falha com LockError; Ctrl+C limpa |
| T6  | `ensure_plesk_migrator_installed()` | T4 | Detecta via `--list`; instala via `plesk installer` |
| T7  | `generate_config_ini()` | T4 | `config.ini` criado chmod 600; bate fixture |
| T8  | `preflight_checks()` | T4 | `plesk-migrator check` invocado e logado |
| T9  | `generate_migration_list()` (+ idempotência) | T4 | 2ª execução sem `--force-regenerate` falha |
| T10 | `filter_migration_list()` | T9 | allow/deny respeitados; `.bak` criado; aborta se 0 |
| T11 | `transfer_accounts()` (skip flags) | T4 | Comando montado com flags conforme YAML |
| T12 | `copy_web/mail/db_content()` (3 métodos) | T4 | Três comandos disparados em sequência |
| T13 | `test_all()` + `cleanup_config_ini()` | T4 | Test loga; cleanup remove `config.ini` |
| T14 | `run_all()` + `main()` + argparse | T1–T13 | Pipeline executa em ordem; `--only-phase` funciona |
| T15 | `run.sh` | — | Root + deps check + dispatch |
| T16 | `config.example.yaml` | T2 | Validável por `_validate_config()` |
| T17 | `README.md` | T14, T15 | Todas seções da §15 presentes |

T6–T13 paralelizáveis após T4.

---

## 17. Plano de verificação end-to-end

### Local (laptop, sem Plesk)

1. `python3 -m py_compile plesk_migrator_orchestrator.py` — sem erros.
2. `python3 plesk_migrator_orchestrator.py --config config.example.yaml --dry-run --skip-install`:
   - Loga config.ini que seria escrito (mascarado)
   - Loga cada comando `plesk-migrator …` mascarado
   - Não toca filesystem do Plesk
   - Exit 0
3. Duas execuções simultâneas → 2ª termina exit 4 (lock).
4. `Ctrl+C` durante dry-run → exit 130, lock liberado.

### Plesk de teste (ambiente do usuário)

5. `sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase install` — extensão instalada.
6. `sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase config` — `config.ini` em `/usr/local/psa/var/modules/panel-migrator/conf/` com chmod 600.
7. `sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase preflight` — `plesk-migrator check` passa.
8. `sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase list` — `migration-list` populada.
9. Editar YAML com `denylist: ["dominio-teste.com"]` → `--only-phase filter` → `.bak` criado, linha removida.
10. **Janela de manutenção**: pipeline completo `sudo ./run.sh --config /etc/plesk-migration.yaml`. Monitorar:
    - `tail -f /var/log/plesk-migration-orchestrator/transfer-accounts.log`
    - `tail -f /var/log/plesk-migration-orchestrator/copy-web.log`
11. Pós-pipeline: `--only-phase test` reexecuta validações.
12. `--only-phase cleanup --cleanup-config` apaga `config.ini`.

---

## 18. Não está no escopo

- Auth SSH por chave (usuário escolheu senha — padrão oficial)
- Pytest formal (validação em Plesk real)
- Múltiplos servidores origem na mesma execução
- Retomada granular pós-falha (Plesk Migrator gerencia internamente)
- Rollback automatizado
- Dashboard / UI

---

## 19. Pontos de extensão `# EXTEND:`

1. `transfer_accounts()` — `--migration-list-file`, `--skip-services-checks`, `--start-from`
2. Auth SSH key (chave em vez de senha)
3. Múltiplas origens (`source-servers: cpanel1, cpanel2` + seções por servidor)
4. Comando `plesk-migrator resume` (se Plesk vier a documentar)
5. Notificação pós-pipeline (webhook, e-mail) em `run_all() finally`

---

## 20. Referências

- Plesk Obsidian — Migrating via the Command Line:
  https://docs.plesk.com/en-US/obsidian/migration-guide/migrating-via-the-command-line.75722/
- Sample config files cPanel → Plesk:
  https://docs.plesk.com/en-US/obsidian/migration-guide/sample-configuration-files/configuration-files-for-cpanel-migration.75601/
- Installation and prerequisites:
  https://docs.plesk.com/en-US/obsidian/migration-guide/installation-and-prerequisites.75498/
- KB cPanel → Plesk SSH config:
  https://support.plesk.com/hc/en-us/articles/12377957202327
- Plano de alto nível: `docs/plan.md`
