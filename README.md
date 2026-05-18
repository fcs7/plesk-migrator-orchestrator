# Plesk Migrator Orchestrator

Orquestrador Python para migração automatizada cPanel → Plesk Obsidian via
extensão oficial **Plesk Migrator** (`panel-migrator`) CLI.

Executa, em pipeline idempotente e seguro, todas as fases da migração:
auto-install da extensão, geração do `config.ini`, pré-flight checks, geração e
filtragem da migration-list, transferência de contas e re-sincronização de
conteúdo web/mail/db.

---

## Quick Start

```bash
git clone https://github.com/fcs7/plesk-migrator-orchestrator.git
cd plesk-migrator-orchestrator
chmod +x run.sh

sudo cp config.example.yaml /etc/plesk-migration.yaml
sudo chmod 600 /etc/plesk-migration.yaml
sudo $EDITOR /etc/plesk-migration.yaml   # preencha source/dest/ssh_password

# 1. Sempre rode um dry-run primeiro
sudo ./run.sh --config /etc/plesk-migration.yaml --dry-run --skip-install

# 2. Pipeline completo (janela de manutenção)
sudo ./run.sh --config /etc/plesk-migration.yaml
```

---

## Pré-requisitos

- **Servidor Plesk Obsidian** rodando no destino (este host).
- **Acesso root** (orquestrador chama `plesk installer` e escreve em
  `/usr/local/psa/var/modules/panel-migrator/`).
- **Python 3.8+** e **PyYAML** instalados:
  ```bash
  python3 --version
  pip3 install pyyaml
  # ou: yum install python3-pyyaml / apt install python3-yaml
  ```
- **SSH origem → destino aberto** (o Plesk Migrator conecta na origem para
  puxar contas e conteúdo).
- **Credenciais root** do servidor cPanel (senha — autenticação por chave SSH
  está fora do escopo deste projeto).

---

## Instalação

```bash
git clone https://github.com/fcs7/plesk-migrator-orchestrator.git
cd plesk-migrator-orchestrator
chmod +x run.sh
sudo cp config.example.yaml /etc/plesk-migration.yaml
sudo chmod 600 /etc/plesk-migration.yaml
sudo $EDITOR /etc/plesk-migration.yaml
```

---

## Uso

### Dry-run (recomendado antes de qualquer execução real)

```bash
sudo ./run.sh --config /etc/plesk-migration.yaml --dry-run --skip-install
```

Loga cada comando que seria executado e o conteúdo do `config.ini` que seria
escrito (com senhas mascaradas como `***`). Não toca filesystem do Plesk.

### Pipeline completo

```bash
sudo ./run.sh --config /etc/plesk-migration.yaml
```

### Fases isoladas

```bash
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase install
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase config
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase preflight
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase list
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase filter
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase transfer
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase copy-web
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase copy-mail
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase copy-db
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase test
sudo ./run.sh --config /etc/plesk-migration.yaml --only-phase cleanup --cleanup-config
```

### Outras flags

| Flag | Efeito |
|------|--------|
| `--verbose` | Habilita DEBUG no stdout (arquivo já é DEBUG) |
| `--skip-install` | Pula auto-install da extensão `panel-migrator` |
| `--force-regenerate` | Sobrescreve `migration-list` existente |
| `--cleanup-config` | Apaga `config.ini` ao fim (remove senha do disco) |
| `--skip-web-content` | Pula `copy-web-content` + flag em `transfer-accounts` |
| `--skip-mail-content` | Pula `copy-mail-content` + flag em `transfer-accounts` |
| `--skip-db-content` | Pula `copy-db-content` + flag em `transfer-accounts` |

Flags CLI sempre sobrescrevem o bloco `behavior.*` do YAML.

---

## Fases (ordem do pipeline)

| # | Fase | O que faz |
|---|------|-----------|
| 1 | `install` | Detecta `panel-migrator` via `extension --list`; se ausente, instala via `plesk installer --select-release-current --install-component panel-migrator` |
| 2 | `config` | Gera `config.ini` em `conf_dir/` com seções `[GLOBAL]`/`[plesk]`/`[cpanel]` (chmod 600) |
| 3 | `preflight` | Roda `plesk-migrator check` (valida SSH, espaço, versão da origem etc.) |
| 4 | `list` | Roda `generate-migration-list`; aborta se já existe (use `--force-regenerate`) |
| 5 | `filter` | Aplica allowlist/denylist em `migration-list` (cria `.bak`); pula se ambas vazias |
| 6 | `transfer` | Roda `transfer-accounts` (com flags `--skip-copy-*-content` opcionais) |
| 7 | `copy-web` | Roda `copy-web-content` para re-sincronizar arquivos web |
| 8 | `copy-mail` | Roda `copy-mail-content` para re-sincronizar mailboxes |
| 9 | `copy-db` | Roda `copy-db-content` para re-sincronizar bancos |
| 10 | `test` | Roda `test-all` para validar o resultado |
| 11 | `cleanup` | Apaga `config.ini` se `--cleanup-config` (default: pula) |

Cada fase tem timeout configurado (10 min para install, 4 h para transfer e
cada `copy-*`, 1 h para `generate-list`, 30 min para `check`, 2 h para `test`).

---

## Troubleshooting

### Onde olhar quando der erro

- **Log principal**: `/var/log/plesk-migration-orchestrator/orchestrator.log`
  (rotaciona a 10 MB × 5 arquivos).
- **Logs por fase**:
  ```
  /var/log/plesk-migration-orchestrator/install.log
  /var/log/plesk-migration-orchestrator/preflight.log
  /var/log/plesk-migration-orchestrator/generate-migration-list.log
  /var/log/plesk-migration-orchestrator/transfer-accounts.log
  /var/log/plesk-migration-orchestrator/copy-web.log
  /var/log/plesk-migration-orchestrator/copy-mail.log
  /var/log/plesk-migration-orchestrator/copy-db.log
  /var/log/plesk-migration-orchestrator/test-all.log
  ```
- **Sessão do Plesk Migrator**:
  `/usr/local/psa/var/modules/panel-migrator/sessions/migration-session/`.

### Acompanhar uma fase longa em paralelo

```bash
tail -f /var/log/plesk-migration-orchestrator/transfer-accounts.log
tail -f /var/log/plesk-migration-orchestrator/copy-web.log
```

### Restaurar migration-list filtrada

```bash
mv /usr/local/psa/var/modules/panel-migrator/sessions/migration-session/migration-list.bak \
   /usr/local/psa/var/modules/panel-migrator/sessions/migration-session/migration-list
```

### Flags avançadas do Plesk Migrator

```bash
plesk-migrator help
plesk-migrator help transfer-accounts
plesk-migrator help copy-web-content
```

Algumas (`--migration-list-file`, `--skip-services-checks`, `--start-from`)
estão marcadas como `# EXTEND:` no código para serem facilmente expostas.

### Exit codes

| Código | Significado |
|--------|-------------|
| `0`    | Sucesso |
| `1`    | Erro de validação de YAML / argumentos |
| `2`    | Pré-flight falhou (`plesk-migrator check`) |
| `3`    | Fase do `plesk-migrator` falhou |
| `4`    | Lock indisponível (outra instância rodando) |
| `130`  | SIGINT / Ctrl+C (convenção POSIX) |

### Lock file

Apenas uma execução por vez. O lock fica em
`/var/lock/plesk-migration-orchestrator.lock` e é liberado automaticamente ao
término ou via signal handler (SIGINT/SIGTERM).

---

## Segurança

- A senha `ssh-password` em texto plano dentro de `config.ini` é **exigência
  oficial do Plesk Migrator** (não há suporte a SSH-key para esta extensão).
- O orquestrador grava `config.ini` com `chmod 600`.
- **Faça `chmod 600` também no YAML**:
  ```bash
  sudo chmod 600 /etc/plesk-migration.yaml
  ```
- Todos os logs aplicam mascaramento automático de `ssh_password` e
  `postgres_password` (substitui pelo literal `***` em stdout, stderr e
  arquivos por fase).
- Use `--cleanup-config` (ou `behavior.cleanup_config: true`) ao final da
  migração para apagar `config.ini` e remover a senha do disco.
- Nunca commite seu `config.yaml` real — o `.gitignore` já cobre
  `config.yaml`, `config.local.yaml` e `*.local.yaml`.

---

## Limitações (fora de escopo)

- Autenticação SSH por chave (Plesk Migrator exige senha).
- Múltiplos servidores origem na mesma execução.
- Retomada granular pós-falha (Plesk Migrator gerencia internamente via sessão).
- Rollback automatizado.
- Dashboard / UI de progresso.
- Suíte pytest formal — validação é feita em ambiente Plesk real seguindo a
  sequência de verificação descrita em `docs/spec.md` §17.

---

## Documentação interna

- `docs/spec.md` — especificação de implementação (fonte-de-verdade).
- `docs/plan.md` — decisões macro do usuário (superseded por `spec.md`).

## Referências oficiais

- [Migrating via the Command Line](https://docs.plesk.com/en-US/obsidian/migration-guide/migrating-via-the-command-line.75722/)
- [Sample configuration files (cPanel)](https://docs.plesk.com/en-US/obsidian/migration-guide/sample-configuration-files/configuration-files-for-cpanel-migration.75601/)
- [Installation and prerequisites](https://docs.plesk.com/en-US/obsidian/migration-guide/installation-and-prerequisites.75498/)
