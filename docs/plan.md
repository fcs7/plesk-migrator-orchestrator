# Plano: Orquestrador Plesk Migrator (cPanel → Plesk) via CLI

## Estado atual do repositório

- Repo GitHub: https://github.com/fcs7/plesk-migrator-orchestrator (privado)
- Branch ativa: `feat/plesk-migrator-orchestrator`
- Commit inicial: `README.md` placeholder (8 linhas). Implementação ainda não iniciada.

## Context

Usuário precisa automatizar migração de servidor cPanel → Plesk Obsidian usando a
extensão oficial **Plesk Migrator** via CLI. Hoje o processo manual tem várias
etapas frágeis: instalar extensão, montar `config.ini` à mão em
`/usr/local/psa/var/modules/panel-migrator/conf/`, rodar `generate-migration-list`,
editar lista, disparar `transfer-accounts`, `copy-content` e `test-all`.

Objetivo: entregar um orquestrador Python que execute todas as fases de forma
idempotente, com:
- Geração automática do `config.ini` a partir de YAML legível.
- Filtragem programática do `migration-list` (allowlist + denylist por domínio).
- Logging seguro (sem vazar `ssh-password`/`postgres-password`).
- Modo `--dry-run` para validar antes de tocar produção.
- Auto-install da extensão `panel-migrator` se ausente.

Usuário tem servidor Plesk de teste — validação final será nesse ambiente real
em vez de pytest formal.

Diretório de trabalho: `/home/fcs/Documents/opiniao/` (vazio, projeto novo).

## Decisões confirmadas

| Item | Escolha |
|------|---------|
| Linguagem | Python 3 principal + wrapper `run.sh` |
| Auth SSH origem | Senha root em `config.ini` (padrão oficial Plesk) |
| Formato config | YAML (PyYAML) |
| Filtro migration-list | Allowlist + denylist por nome domínio |
| Execução | Servidor Plesk destino, como root |
| Layout | Flat (4 arquivos no projeto) |
| Testes formais | Não — usuário valida em Plesk real, dry-run robusto |
| Install extensão | `ensure_plesk_migrator_installed()` automático |

## Arquivos a criar

```
/home/fcs/Documents/opiniao/
├── plesk_migrator_orchestrator.py   # orquestrador principal
├── run.sh                            # wrapper bash (valida root + deps)
├── config.example.yaml               # template de input
└── README.md                         # uso, pré-reqs, troubleshooting
```

### 1. `plesk_migrator_orchestrator.py` (~350 linhas)

**Imports:** `argparse`, `configparser`, `logging`, `os`, `pathlib`, `re`,
`shutil`, `subprocess`, `sys`, `time`, `yaml`.

**Constantes default:**
```python
DEFAULT_PLESK_MIGRATOR_BIN = "/usr/local/psa/admin/sbin/modules/panel-migrator/plesk-migrator"
DEFAULT_CONF_DIR           = "/usr/local/psa/var/modules/panel-migrator/conf"
DEFAULT_SESSIONS_DIR       = "/usr/local/psa/var/modules/panel-migrator/sessions"
DEFAULT_SESSION_NAME       = "migration-session"
DEFAULT_LOG_DIR            = "/var/log/plesk-migration-orchestrator"
PLESK_BIN                  = "/usr/local/psa/bin/extension"   # plesk bin extension
```

**Classe `PleskMigrationOrchestrator`:**

```python
class PleskMigrationOrchestrator:
    def __init__(self, config: dict, dry_run: bool = False):
        # valida chaves obrigatórias: source.host, source.ssh_password, dest.host
        # extrai paths com fallback nas constantes default
        # configura logger (arquivo + stdout) com formatter timestamped
        # registra sensitive_values p/ mascaramento (senhas)

    # --- helpers privados ---
    def _mask(self, text: str) -> str: ...          # substitui senhas por ***
    def _run(self, cmd: list[str], *,
             check: bool = True,
             sensitive_env: dict | None = None,
             log_to: pathlib.Path | None = None) -> subprocess.CompletedProcess:
        # log da linha (mascarada), respeita dry_run, stream stdout p/ log_to opc.

    # --- fases ---
    def ensure_plesk_migrator_installed(self) -> None:
        # plesk bin extension --list | grep -i panel-migrator
        # se ausente: plesk bin extension --install panel-migrator
        # se falhar: tenta 'plesk installer --select-release-current --install-component panel-migrator'

    def generate_config_ini(self) -> pathlib.Path:
        # mkdir -p conf_dir
        # monta ConfigParser com seções [GLOBAL] [plesk] [cpanel]
        # GLOBAL: source-type=cpanel, source-servers=cpanel, target-type=plesk
        # [plesk]: ip, os=unix
        # [cpanel]: ip, os=unix, ssh-password, ssh-port (se != 22),
        #          postgres-password (se fornecido)
        # escreve em conf_dir/config.ini com chmod 600 antes do write

    def generate_migration_list(self) -> pathlib.Path:
        # _run([bin, 'generate-migration-list'])
        # retorna sessions_dir/session_name/migration-list

    def filter_migration_list(self, allowlist: list[str], denylist: list[str]) -> None:
        # backup .bak; lê linhas; parse domínio (1ª coluna até espaço/tab)
        # mantém se (allowlist vazia OR domínio in allowlist) AND domínio not in denylist
        # regrava; log de qtos mantidos/removidos
        # validação: se restou 0 domínios e havia entradas, aborta

    def transfer_accounts(self) -> None:
        # _run([bin, 'transfer-accounts'], log_to=logs/transfer.log)

    def copy_content(self) -> None:
        # _run([bin, 'copy-content'], log_to=logs/copy.log)

    def test_all(self) -> None:
        # _run([bin, 'test-all'], log_to=logs/test-all.log)

    # --- pipeline ---
    def run_all(self, skip_install: bool = False) -> None:
        # ordem: ensure_plesk_migrator_installed (se !skip)
        #     -> generate_config_ini
        #     -> generate_migration_list
        #     -> filter_migration_list (se allow/deny não vazias)
        #     -> transfer_accounts
        #     -> copy_content
        #     -> test_all
```

**`main()`:**
- `argparse`: `--config PATH` (obrigatório), `--dry-run`, `--skip-install`,
  `--only-phase {config,list,filter,transfer,copy,test,all}` (default `all`).
- Carrega YAML, instancia orquestrador, despacha pela fase.
- Captura `subprocess.CalledProcessError` → loga stderr mascarado, exit 1.

### 2. `run.sh` (~40 linhas)

```bash
#!/usr/bin/env bash
set -euo pipefail
# 1. exigir UID 0
[[ $EUID -eq 0 ]] || { echo "Precisa root"; exit 1; }
# 2. detectar python3
command -v python3 >/dev/null || { echo "python3 ausente"; exit 1; }
# 3. validar PyYAML
python3 -c "import yaml" 2>/dev/null || {
    echo "PyYAML ausente. Instale: pip3 install pyyaml OU yum/apt install python3-yaml"
    exit 1
}
# 4. invoca orquestrador passando args
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/plesk_migrator_orchestrator.py" "$@"
```

### 3. `config.example.yaml`

```yaml
# Origem cPanel/WHM
source:
  host: 192.0.2.10
  ssh_port: 22                # opcional; omita p/ default 22
  ssh_password: "TROQUE_AQUI" # senha root cPanel (atenção: arquivo sensível!)
  postgres_password: null     # opcional; preencha se houver PostgreSQL

# Destino Plesk (este servidor)
dest:
  host: 192.0.2.20
  # os é fixo unix neste projeto

# Filtros de migração (vazio = migra tudo)
migration:
  allowlist: []   # ex: ["dominio1.com", "dominio2.com"] — só migra esses
  denylist: []    # ex: ["teste.dominio.com"] — exclui esses

# Paths (deixe em branco p/ usar defaults oficiais Plesk)
paths:
  plesk_migrator_bin: null
  conf_dir: null
  sessions_dir: null
  session_name: migration-session
  log_dir: /var/log/plesk-migration-orchestrator

# Comportamento
behavior:
  dry_run: false        # CLI --dry-run sobrescreve
  skip_install: false   # pula auto-install da extension
```

Permissões: README orienta `chmod 600 /etc/plesk-migration.yaml`.

### 4. `README.md`

Seções:
- **Pré-requisitos**: Plesk Obsidian no destino, root, Python 3, PyYAML, acesso
  SSH origem→destino aberto.
- **Instalação**: `cp config.example.yaml /etc/plesk-migration.yaml && chmod 600 ...`
- **Uso**:
  ```bash
  ./run.sh --config /etc/plesk-migration.yaml --dry-run
  ./run.sh --config /etc/plesk-migration.yaml --only-phase config
  ./run.sh --config /etc/plesk-migration.yaml            # pipeline completo
  ```
- **Fases** (resumo da ordem).
- **Onde olhar quando der erro**: `/var/log/plesk-migration-orchestrator/*.log`,
  `/usr/local/psa/var/modules/panel-migrator/sessions/migration-session/`,
  `plesk-migrator help <cmd>` p/ flags avançadas (`--migration-list-file`,
  `--skip-services-checks`, etc.).
- **Segurança**: nota sobre senha em texto plano no config.ini é exigência da
  documentação oficial Plesk; restrinja permissões e remova após migração.

## Pontos de extensão (comentados no código)

Marcar com `# EXTEND:` os locais onde fica natural adicionar:
- Flag `--migration-list-file` em `transfer_accounts()`.
- Flag `--skip-services-checks` em `transfer_accounts()`.
- Suporte a múltiplos servidores origem (lista em `source-servers`).
- Modo SSH-key (atualmente fora de escopo — confirmado pelo usuário).

## Verificação end-to-end

Ordem para o usuário validar no Plesk de teste:

1. **Dry-run sem origem real:**
   ```bash
   sudo ./run.sh --config ./config.example.yaml --dry-run --skip-install
   ```
   Esperado: imprime cada comando que rodaria, mostra config.ini gerado
   (mascarando senha), não toca filesystem do Plesk.

2. **Smoke install + config:**
   ```bash
   sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase config
   ```
   Esperado: extension instalada, `/usr/local/psa/var/modules/panel-migrator/conf/config.ini`
   existe com seções `[GLOBAL]/[plesk]/[cpanel]`, permissão 600.

3. **Gerar lista:**
   ```bash
   sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase list
   ```
   Esperado: arquivo `sessions/migration-session/migration-list` populado.
   Inspecionar manualmente.

4. **Aplicar filtros:**
   Editar `migration.allowlist`/`denylist` no YAML, rodar:
   ```bash
   sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase filter
   ```
   Esperado: `migration-list.bak` criado, lista filtrada conforme YAML, log
   mostra quantos domínios mantidos/removidos.

5. **Janela de manutenção** — pipeline completo:
   ```bash
   sudo ./run.sh --config /etc/plesk-migration.yaml
   ```
   Pula `--only-phase` (roda tudo). Acompanhar
   `/var/log/plesk-migration-orchestrator/transfer.log` em outra sessão
   (`tail -f`).

6. **Pós-migração:** rodar `test-all` isolado se quiser repetir:
   ```bash
   sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase test
   ```

## Não está no escopo

- Autenticação SSH por chave (usuário escolheu senha oficial).
- Testes pytest (usuário valida em Plesk real).
- Suporte a múltiplos servidores origem simultâneos.
- UI / dashboard de progresso.
- Rollback automatizado pós-migração.
