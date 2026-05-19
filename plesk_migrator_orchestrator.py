#!/usr/bin/env python3
"""Plesk Migrator Orchestrator — cPanel → Plesk Obsidian via CLI.

Orquestra as fases da extensão oficial Plesk Migrator (panel-migrator)
em pipeline idempotente, com logging seguro (mascaramento de senhas),
dry-run, lock file, signal handlers e timeouts por fase.

Referência: docs/spec.md (fonte-de-verdade) e docs/plan.md (decisões).
"""

from __future__ import annotations

import argparse
import configparser
import datetime as _dt
import fcntl
import getpass
import hashlib
import json
import logging
import os
import pathlib
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from logging.handlers import RotatingFileHandler

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "ERRO: PyYAML ausente. Instale com:\n"
        "  pip3 install pyyaml\n"
        "  # ou: yum install python3-pyyaml / apt install python3-yaml\n"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constantes (top-of-module) — §3 spec
# ---------------------------------------------------------------------------

DEFAULT_PLESK_MIGRATOR_BIN = (
    "/usr/local/psa/admin/sbin/modules/panel-migrator/plesk-migrator"
)
DEFAULT_CONF_DIR = "/usr/local/psa/var/modules/panel-migrator/conf"
DEFAULT_SESSIONS_DIR = "/usr/local/psa/var/modules/panel-migrator/sessions"
DEFAULT_SESSION_NAME = "migration-session"
DEFAULT_LOG_DIR = "/var/log/plesk-migration-orchestrator"
LOCK_FILE = "/var/lock/plesk-migration-orchestrator.lock"

# Locais conhecidos para auto-discovery (ordem importa: primeiro hit ganha).
# Plesk usa caminhos canônicos, mas alguns hosters/builds movem binários — o
# auto-discovery reduz erros silenciosos por caminho errado.
_PLESK_BIN_CANDIDATES = [
    "/usr/local/psa/bin/plesk",
    "/usr/sbin/plesk",
    "/opt/psa/bin/plesk",
]
_EXTENSION_BIN_CANDIDATES = [
    "/usr/local/psa/bin/extension",
    "/opt/psa/bin/extension",
]
_PANEL_MIGRATOR_MODULE_CANDIDATES = [
    "/usr/local/psa/var/modules/panel-migrator",
    "/opt/psa/var/modules/panel-migrator",
]
_PLESK_MIGRATOR_BIN_CANDIDATES = [
    "/usr/local/psa/admin/sbin/modules/panel-migrator/plesk-migrator",
    "/opt/psa/admin/sbin/modules/panel-migrator/plesk-migrator",
]

# Timeouts por fase (segundos)
TIMEOUT_INSTALL = 600          # 10 min
TIMEOUT_GENERATE_LIST = 3600   # 1 h
TIMEOUT_CHECK = 1800           # 30 min
TIMEOUT_TRANSFER = 14400       # 4 h
TIMEOUT_COPY_CONTENT = 14400   # 4 h cada (web/mail/db)
TIMEOUT_TEST_ALL = 7200        # 2 h
TIMEOUT_FIX_DOCROOT = 600      # 10 min — apenas chamadas `plesk bin subscription`
TIMEOUT_FIX_MAILPATH = 600     # 10 min — auditoria de Maildir paths
TIMEOUT_CHECK_MAIL_PASSWORDS = 300  # 5 min — query `plesk db` + reset opcional
TIMEOUT_FIX_LIMITS = 600       # 10 min — UPDATE Limits + `plesk repair db`
TIMEOUT_RETRANSFER = 14400     # 4 h — re-roda transfer-accounts pra failed
TIMEOUT_FIX_MAIL_QUOTA = 300   # 5 min — UPDATE mail.mbox_quota
TIMEOUT_FIX_DNS = 300          # 5 min — DELETE dns_recs cPanel-only
TIMEOUT_FTP_AUDIT = 60         # 1 min — leitura de accounts_report_tree
TIMEOUT_SANITIZE_LIST = 60     # 1 min — regex em migration-list
TIMEOUT_FIX_OWNER = 1800       # 30 min — cria customer + reassign subscription

# Subpastas escaneadas por fix-docroot dentro de /var/www/vhosts/<domain>/.
# Ordem importa apenas para tie-break determinístico (mesmo total_bytes):
# httpdocs primeiro porque é o canonical Plesk; se rivaliza com outro, vence.
DOCROOT_CANDIDATES = ("httpdocs", "public_html", "www", "web")

# Subdomains reservados pelo Plesk (primeiro label do FQDN). Aplicação cPanel
# que use esses nomes precisa rename — sanitize-list propõe alternativas.
RESERVED_PLESK_SUBDOMAINS = (
    "webmail", "mail", "ftp", "ns1", "ns2", "smtp", "imap", "pop", "pop3",
)

# Hosts criados automaticamente pelo cPanel (e migrados via shallow-dump) que
# entram em conflito com DNS Plesk. Lista conservadora — evolui via PR.
CPANEL_ONLY_DNS_HOSTS = ("cpcontacts", "cpanel", "whm", "webdisk")

# Renames default usados por sanitize-list (override via
# migration.reserved_renames no YAML).
DEFAULT_RESERVED_RENAME = {
    "webmail": "correio",
    "mail": "email",
    "ftp": "arquivos",
    "smtp": "smtp2",
    "imap": "imap2",
    "pop": "pop2",
    "pop3": "pop2",
    "ns1": "dns1",
    "ns2": "dns2",
}

MAX_RETRANSFER_ATTEMPTS = 3

# Pattern para capturar pares chave=valor com senha em texto livre
SENSITIVE_KEY_PATTERN = re.compile(
    r"(ssh[_-]password|postgres[_-]password)\s*[:=]\s*['\"]?([^'\"\s]+)['\"]?",
    re.IGNORECASE,
)

# Comandos imutáveis que podem rodar mesmo em --dry-run (apenas leitura).
# NB: `plesk-migrator check` NÃO está aqui — apesar de ler, ele depende de
# `config.ini` e da `migration-list`, que dry-run não escreve/gera. Rodar em
# dry-run lê estado obsoleto ou falha — preflight é pulado em dry-run.
_READ_ONLY_COMMANDS = (
    ("extension", "--list"),
    ("extension", "--info"),
    ("plesk-migrator", "help"),
)

PHASES_ORDER = [
    "sanity-check",
    "install",
    "config",
    "list",
    "sanitize-list",
    "filter",
    "preflight",
    "transfer",
    "fix-limits",
    "retransfer-failed",
    "fix-owner",
    "copy-web",
    "copy-mail",
    "fix-mailpath",
    "check-mail-passwords",
    "fix-mail-quota",
    "fix-ftp-renames",
    "fix-dns-conflicts",
    "copy-db",
    "fix-docroot",
    "test",
    "cleanup-config",
]


# ---------------------------------------------------------------------------
# Exceções (§4 spec)
# ---------------------------------------------------------------------------

class PleskMigrationError(Exception):
    """Base para todos os erros do orquestrador."""


class ValidationError(PleskMigrationError):
    """Erro de validação de configuração YAML."""


class PreflightError(PleskMigrationError):
    """Falha nas verificações pré-migração."""


class PhaseExecutionError(PleskMigrationError):
    """Falha durante execução de uma fase do pipeline."""


class LockError(PleskMigrationError):
    """Não foi possível adquirir o lock (outra instância rodando)."""


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------

class PleskMigrationOrchestrator:
    """Pipeline completo cPanel → Plesk via panel-migrator CLI."""

    def __init__(
        self,
        config: dict,
        *,
        dry_run: bool = False,
        force_regenerate: bool = False,
        cleanup_config: bool = False,
        verbose: bool = False,
        resume: bool = False,
        start_from: str | None = None,
    ) -> None:
        self.config = config or {}
        self.dry_run = dry_run
        self.force_regenerate = force_regenerate
        self.cleanup_config = cleanup_config
        self.verbose = verbose
        self.resume = resume
        self.start_from = start_from
        if resume and force_regenerate:
            raise ValidationError(
                "resume e force_regenerate são mutuamente exclusivos"
            )

        self._validate_config()

        paths = self.config.get("paths") or {}
        self.log_dir = pathlib.Path(paths.get("log_dir") or DEFAULT_LOG_DIR)

        # Valores sensíveis para mascaramento literal
        src = self.config["source"]
        self.sensitive_values: list[str] = []
        if src.get("ssh_password"):
            self.sensitive_values.append(str(src["ssh_password"]))
        if src.get("postgres_password"):
            self.sensitive_values.append(str(src["postgres_password"]))

        self.logger = self._setup_logger()

        # Auto-discovery dos caminhos do Plesk no servidor. Atributos definidos:
        # plesk_bin, plesk_extension_bin, plesk_migrator_bin, conf_dir,
        # sessions_dir, session_name. Re-roda após install para detectar
        # plesk-migrator recém-instalado.
        self._discover_paths()

        self._lock_fd: int | None = None
        self._current_proc: subprocess.Popen | None = None

    # ------------------------------------------------------------------
    # Validação
    # ------------------------------------------------------------------

    def _validate_config(self) -> None:
        cfg = self.config
        if not isinstance(cfg, dict):
            raise ValidationError("Config raiz precisa ser mapping YAML")

        for section in ("source", "dest"):
            if section not in cfg or not isinstance(cfg[section], dict):
                raise ValidationError(f"Seção obrigatória ausente: {section}")

        src = cfg["source"]
        dst = cfg["dest"]

        if not src.get("host"):
            raise ValidationError("source.host é obrigatório")
        if not isinstance(src["host"], str):
            raise ValidationError("source.host deve ser string")
        if not src.get("ssh_password"):
            raise ValidationError("source.ssh_password é obrigatório")
        if not isinstance(src["ssh_password"], str):
            raise ValidationError("source.ssh_password deve ser string")

        ssh_port = src.get("ssh_port", 22)
        if (
            isinstance(ssh_port, bool)
            or not isinstance(ssh_port, int)
            or not (1 <= ssh_port <= 65535)
        ):
            raise ValidationError(
                "source.ssh_port deve ser inteiro 1..65535"
            )
        src["ssh_port"] = ssh_port

        pg = src.get("postgres_password")
        if pg is not None and not isinstance(pg, str):
            raise ValidationError("source.postgres_password deve ser string ou null")

        if not dst.get("host"):
            raise ValidationError("dest.host é obrigatório")
        if not isinstance(dst["host"], str):
            raise ValidationError("dest.host deve ser string")

        migration = cfg.get("migration") or {}
        for key in ("allowlist", "denylist"):
            val = migration.get(key, [])
            if not isinstance(val, list):
                raise ValidationError(f"migration.{key} deve ser lista")
            if not all(isinstance(v, str) for v in val):
                raise ValidationError(
                    f"migration.{key} deve conter apenas strings"
                )
            if val:
                # `migration-list` contém objetos estruturados (resellers,
                # customers, plans, domínios) e o filtro por "primeiro token"
                # corrompe entradas não-domínio. Bloqueia até termos parser
                # do formato oficial. Alternativas: editar migration-list
                # manualmente (--only-phase list, edita, --only-phase
                # preflight em diante) ou usar `--migration-list-file` do
                # plesk-migrator nativo.
                raise ValidationError(
                    f"migration.{key} não é suportado nesta versão — o filtro "
                    "local pode corromper migration-list. Edite a lista "
                    "manualmente ou use plesk-migrator --migration-list-file. "
                    "Ver README seções 'Upgrading' e 'Filtragem de "
                    "migration-list'."
                )
        # Preserva reserved_renames + max_retransfer_attempts; reseta apenas
        # allow/denylist (não-suportados nesta versão).
        reserved_renames = migration.get("reserved_renames") or {}
        if not isinstance(reserved_renames, dict):
            raise ValidationError(
                "migration.reserved_renames deve ser mapping (ex: webmail: correio)"
            )
        if not all(isinstance(k, str) and isinstance(v, str)
                   for k, v in reserved_renames.items()):
            raise ValidationError(
                "migration.reserved_renames: chaves e valores devem ser strings"
            )
        max_retr = migration.get("max_retransfer_attempts", 3)
        if (
            isinstance(max_retr, bool)
            or not isinstance(max_retr, int)
            or not (1 <= max_retr <= 100)
        ):
            raise ValidationError(
                "migration.max_retransfer_attempts deve ser inteiro 1..100"
            )
        cfg["migration"] = {
            "allowlist": [],
            "denylist": [],
            "reserved_renames": reserved_renames,
            "max_retransfer_attempts": max_retr,
        }

        paths = cfg.get("paths") or {}
        # Overrides que o orchestrator NÃO consegue propagar para plesk-migrator
        # (que lê config.ini de path fixo e gerencia sessões em path fixo).
        # Aceitar override aqui criaria mismatch silencioso: o orchestrator
        # olharia num lugar e o plesk-migrator escreveria em outro.
        for locked in ("conf_dir", "sessions_dir", "session_name"):
            if paths.get(locked):
                raise ValidationError(
                    f"paths.{locked} não pode ser sobrescrito — o orchestrator "
                    "não propaga esse caminho para o plesk-migrator. Remova a "
                    "chave do YAML (auto-discovery resolve o caminho real). "
                    "Ver README seção 'Upgrading'."
                )
        for path_key in (
            "plesk_migrator_bin", "plesk_bin", "plesk_extension_bin", "log_dir",
        ):
            if path_key in paths and paths[path_key] is not None:
                if not isinstance(paths[path_key], str):
                    raise ValidationError(
                        f"paths.{path_key} deve ser string ou null"
                    )

        behavior = cfg.get("behavior") or {}
        skip = behavior.get("skip") or {}
        for key in (
            "web_content", "mail_content", "db_content",
            "fix_docroot", "fix_mailpath", "check_mail_passwords",
            "sanitize_list", "fix_limits", "retransfer_failed",
            "fix_mail_quota", "fix_ftp_renames", "fix_dns_conflicts",
            "fix_owner",
        ):
            if key in skip and not isinstance(skip[key], bool):
                raise ValidationError(f"behavior.skip.{key} deve ser bool")
        for key in (
            "dry_run", "skip_install", "force_regenerate",
            "cleanup_config", "resume",
            "apply_owner_fix", "apply_dns_cleanup", "apply_mailpath_fix",
            "reset_mail_passwords", "rename_reserved_subdomains",
        ):
            if key in behavior and not isinstance(behavior[key], bool):
                raise ValidationError(f"behavior.{key} deve ser bool")
        if "start_from" in behavior and behavior["start_from"] is not None:
            if not isinstance(behavior["start_from"], str):
                raise ValidationError("behavior.start_from deve ser string")

    # ------------------------------------------------------------------
    # Auto-discovery de caminhos
    # ------------------------------------------------------------------

    def _resolve_binary(
        self,
        override: str | None,
        candidates: list[str],
        *,
        label: str,
        optional: bool = False,
    ) -> pathlib.Path | None:
        """Resolve um binário: override YAML > candidatos fixos > $PATH.

        Override é validado de forma rigorosa: se o arquivo não existe ou não
        é executável, raise ValidationError (em vez de devolver path inválido
        e deixar FileNotFoundError vazar mais tarde com traceback feio).
        Auto-discovery sem override permanece leniente conforme `optional`.
        """
        if override:
            p = pathlib.Path(override)
            if not p.is_file():
                raise ValidationError(
                    f"paths.{label} aponta para {override} mas o arquivo "
                    "não existe (ou não é arquivo regular)."
                )
            if not os.access(p, os.X_OK):
                raise ValidationError(
                    f"paths.{label} aponta para {override} mas não é "
                    "executável (chmod +x?)."
                )
            return p
        for cand in candidates:
            cp = pathlib.Path(cand)
            if cp.exists():
                return cp
        # Última tentativa: $PATH com o basename do primeiro candidato.
        if candidates:
            found = shutil.which(pathlib.Path(candidates[0]).name)
            if found:
                return pathlib.Path(found)
        if not optional:
            self.logger.warning(
                "Não localizei binário %s em locais conhecidos nem no $PATH",
                label,
            )
        return None

    def _resolve_module_dir(self) -> pathlib.Path | None:
        """Procura o diretório do módulo panel-migrator (anchor para conf/sessions)."""
        for cand in _PANEL_MIGRATOR_MODULE_CANDIDATES:
            cp = pathlib.Path(cand)
            if cp.is_dir():
                return cp
        return None

    def _discover_paths(self) -> None:
        """Probing do filesystem para resolver caminhos reais do Plesk.

        Honra overrides do YAML quando presentes; senão procura nos locais
        canônicos. Re-executável: o install phase chama de novo para detectar
        plesk-migrator recém-instalado.
        """
        paths_cfg = self.config.get("paths") or {}

        self.plesk_bin = self._resolve_binary(
            paths_cfg.get("plesk_bin"),
            _PLESK_BIN_CANDIDATES,
            label="plesk_bin",
            optional=True,
        )
        self.plesk_extension_bin = self._resolve_binary(
            paths_cfg.get("plesk_extension_bin"),
            _EXTENSION_BIN_CANDIDATES,
            label="plesk_extension_bin",
            optional=True,
        )
        self.plesk_migrator_bin = self._resolve_binary(
            paths_cfg.get("plesk_migrator_bin"),
            _PLESK_MIGRATOR_BIN_CANDIDATES,
            label="plesk_migrator_bin",
            optional=True,  # pode não existir antes do install
        )

        module_dir = self._resolve_module_dir()
        if module_dir:
            self.conf_dir = module_dir / "conf"
            self.sessions_dir = module_dir / "sessions"
        else:
            # Fallback para defaults canônicos; o install phase cria a árvore.
            self.conf_dir = pathlib.Path(DEFAULT_CONF_DIR)
            self.sessions_dir = pathlib.Path(DEFAULT_SESSIONS_DIR)
        self.session_name = DEFAULT_SESSION_NAME

        self.logger.info("Auto-discovery de caminhos:")
        self.logger.info(
            "  plesk:           %s",
            self.plesk_bin or "(não encontrado — necessário para install)",
        )
        self.logger.info(
            "  extension:       %s",
            self.plesk_extension_bin
            or "(não encontrado — necessário para detectar panel-migrator)",
        )
        self.logger.info(
            "  plesk-migrator:  %s",
            self.plesk_migrator_bin
            or "(não instalado — será resolvido após fase install)",
        )
        self.logger.info("  conf_dir:        %s", self.conf_dir)
        self.logger.info("  sessions_dir:    %s", self.sessions_dir)
        self.logger.info("  session_name:    %s", self.session_name)

    # ------------------------------------------------------------------
    # Logging + mascaramento
    # ------------------------------------------------------------------

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("plesk_migrator_orchestrator")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                self.log_dir / "orchestrator.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )
            logger.addHandler(file_handler)
        except PermissionError:
            sys.stderr.write(
                f"AVISO: sem permissão para criar log_dir {self.log_dir}; "
                "apenas stdout será usado.\n"
            )

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        stream_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(stream_handler)
        logger.propagate = False
        return logger

    def _mask(self, text: str) -> str:
        if not text:
            return text
        masked = text
        for value in self.sensitive_values:
            if value:
                masked = masked.replace(value, "***")
        masked = SENSITIVE_KEY_PATTERN.sub(
            lambda m: f"{m.group(1)}=***", masked
        )
        return masked

    # ------------------------------------------------------------------
    # Subprocess wrapper
    # ------------------------------------------------------------------

    @staticmethod
    def _is_read_only(cmd: list[str]) -> bool:
        if not cmd:
            return False
        # Compara o nome do binário (basename, lowercase) com o 1º token
        # e o 2º argumento exato com o 2º token — evita falso positivo
        # com substrings como "help" em "--help".
        bin_name = os.path.basename(str(cmd[0])).lower()
        second = str(cmd[1]).lower() if len(cmd) > 1 else ""
        for tokens in _READ_ONLY_COMMANDS:
            expected_bin, expected_arg = tokens
            if bin_name == expected_bin and second == expected_arg:
                return True
        return False

    def _run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        log_to: pathlib.Path | None = None,
        timeout: int | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        masked_cmd = self._mask(" ".join(str(c) for c in cmd))
        self.logger.info("$ %s", masked_cmd)

        if self.dry_run and not self._is_read_only(cmd):
            self.logger.info("[DRY-RUN] comando não executado")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        log_fh = None
        if log_to is not None:
            try:
                log_to.parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(log_to, "a", encoding="utf-8")
                log_fh.write(f"\n--- {masked_cmd} ---\n")
            except OSError as exc:
                self.logger.warning("Não foi possível abrir %s: %s", log_to, exc)
                log_fh = None

        collected: list[str] = []
        try:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE if input_text is not None else None,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError as exc:
                if self.dry_run:
                    self.logger.info(
                        "[DRY-RUN] binário ausente (%s) — leitura simulada",
                        exc.filename or cmd[0],
                    )
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                raise
            self._current_proc = proc

            if input_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(input_text)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass

            # Timer mata o subprocess se o timeout estourar — necessário
            # porque o loop `for raw_line in proc.stdout` pode bloquear
            # indefinidamente quando o processo trava sem emitir newline.
            timeout_fired = threading.Event()
            timer: threading.Timer | None = None
            if timeout is not None and timeout > 0:
                def _on_timeout() -> None:
                    # SIGTERM + grace antes de SIGKILL — fases longas (transfer,
                    # copy-*) podem ter rsync/SSH ativos na origem; matar direto
                    # deixa locks e conexões zumbis na sessão do Plesk Migrator.
                    timeout_fired.set()
                    self.logger.error(
                        "Timeout (%ss) excedido. SIGTERM → wait 15s → SIGKILL.",
                        timeout,
                    )
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    except ProcessLookupError:
                        pass
                timer = threading.Timer(timeout, _on_timeout)
                timer.daemon = True
                timer.start()

            assert proc.stdout is not None
            try:
                try:
                    for raw_line in proc.stdout:
                        line = raw_line.rstrip("\n")
                        masked = self._mask(line)
                        collected.append(masked)
                        self.logger.debug(masked)
                        if log_fh is not None:
                            log_fh.write(masked + "\n")
                            log_fh.flush()
                except Exception:
                    proc.kill()
                    raise

                proc.wait()
            finally:
                if timer is not None:
                    timer.cancel()

            if timeout_fired.is_set():
                raise PhaseExecutionError(
                    f"Timeout após {timeout}s em: {masked_cmd}"
                )

            rc = proc.returncode
            if check and rc != 0:
                output = "\n".join(collected)
                raise subprocess.CalledProcessError(
                    rc, masked_cmd, output=output
                )

            return subprocess.CompletedProcess(
                cmd, rc, stdout="\n".join(collected), stderr=""
            )
        finally:
            self._current_proc = None
            if log_fh is not None:
                log_fh.close()

    # ------------------------------------------------------------------
    # Lock + signals (§12 spec)
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> None:
        try:
            self._lock_fd = os.open(
                LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600
            )
        except PermissionError as exc:
            raise LockError(
                f"Sem permissão para criar lock {LOCK_FILE}: {exc}"
            )
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise LockError(
                f"Outra instância já rodando ({LOCK_FILE})"
            )
        self.logger.debug("Lock adquirido em %s", LOCK_FILE)

    def _release_lock(self) -> None:
        # async-signal-safe: nada de self.logger aqui (ver _cleanup_subprocess).
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._cleanup_subprocess)
        signal.signal(signal.SIGTERM, self._cleanup_subprocess)

    def _cleanup_subprocess(self, signum=None, frame=None) -> None:
        # NÃO usar self.logger aqui: o módulo logging usa lock interno e
        # signals podem interromper outra thread que já está logando — re-entrar
        # no mesmo lock causa deadlock. Usamos os.write para o stderr direto,
        # que é async-signal-safe.
        try:
            os.write(
                2,
                f"\n[SIGNAL] Sinal {signum} recebido — encerrando subprocess…\n"
                .encode("utf-8"),
            )
        except OSError:
            pass
        proc = self._current_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._release_lock()
        sys.exit(130)

    # ------------------------------------------------------------------
    # Fases (§7 spec)
    # ------------------------------------------------------------------

    def sanity_check(self) -> None:
        """Auto-diagnóstico do ambiente antes de qualquer fase mutativa.

        Roda como primeira fase do pipeline. Confirma que estamos num host
        Plesk Obsidian operacional rodando como root. Aborta com mensagem
        clara em vez de deixar o pipeline falhar fragmentado mais adiante.

        Em dry-run: skip (`plesk version` exige binário real).
        """
        self.logger.info("Fase: sanity_check")
        if self.dry_run:
            self.logger.info(
                "[DRY-RUN] sanity-check pulado — `plesk version` precisa "
                "do binário real."
            )
            return

        # 1. Privilégio: orchestrator escreve em /usr/local/psa/var/... e
        # invoca `plesk installer` — exige root.
        euid = os.geteuid() if hasattr(os, "geteuid") else -1
        if euid != 0:
            raise PreflightError(
                f"Orchestrator precisa rodar como root (uid 0), atual={euid}. "
                "Use `sudo ./run.sh --config /etc/plesk-migration.yaml`."
            )

        # 2. Auto-discovery resolveu o binário plesk?
        if not self.plesk_bin:
            raise PreflightError(
                "Binário 'plesk' não localizado em locais conhecidos nem no "
                f"$PATH. Procurei em: {', '.join(_PLESK_BIN_CANDIDATES)}. "
                "Este host não parece ser um servidor Plesk Obsidian. "
                "Sobrescreva paths.plesk_bin no YAML se o caminho for outro."
            )

        # 3. Plesk operacional e versão Obsidian (>= 18.x). `plesk version`
        # imprime info do produto; aceitamos "obsidian" no output OU major
        # version >= 18 (tolerante a variações de formatação).
        result = self._run([str(self.plesk_bin), "version"], check=False)
        if result.returncode != 0:
            raise PreflightError(
                f"`plesk version` retornou rc={result.returncode} — Plesk "
                "não está respondendo. Verifique se o serviço está rodando."
            )
        output = result.stdout or ""
        version_ok = "obsidian" in output.lower()
        if not version_ok:
            match = re.search(r"(\d+)\.\d+", output)
            if match and int(match.group(1)) >= 18:
                version_ok = True
        if not version_ok:
            raise PreflightError(
                "Versão do Plesk não identificada como Obsidian (>= 18.x). "
                f"Saída de `plesk version`:\n{output[:500]}"
            )

        self.logger.info(
            "✓ sanity-check OK: rodando como root, plesk em %s, Obsidian 18.x+",
            self.plesk_bin,
        )

    def _require_runtime_state(self) -> None:
        """Aborta se config.ini/migration-list faltam (pré-req de fases pós-list).

        Sem isso, `--only-phase transfer` (ou copy-*, test, preflight) num
        diretório limpo deixaria o plesk-migrator falhar com erro críptico
        em vez do orchestrator avisar qual fase rodar antes.

        Skip em dry-run (estado real não é gerado nesse modo).
        """
        if self.dry_run:
            return
        config_path = self.conf_dir / "config.ini"
        migration_list = self.sessions_dir / self.session_name / "migration-list"
        missing: list[str] = []
        if not config_path.exists():
            missing.append(
                f"config.ini não existe em {config_path} — rode "
                "--only-phase config primeiro"
            )
        if not migration_list.exists():
            missing.append(
                f"migration-list não existe em {migration_list} — rode "
                "--only-phase list primeiro"
            )
        if missing:
            raise PhaseExecutionError("; ".join(missing))

    def _require_plesk_migrator_bin(self) -> None:
        """Garante plesk-migrator localizado; em dry-run usa placeholder canônico."""
        if self.plesk_migrator_bin:
            return
        if self.dry_run:
            # Em dry-run o binário não é executado; usar o default permite
            # logar o comando que SERIA invocado. O fallback de
            # FileNotFoundError em _run cobre o caso do path não existir.
            self.plesk_migrator_bin = pathlib.Path(DEFAULT_PLESK_MIGRATOR_BIN)
            self.logger.info(
                "[DRY-RUN] plesk-migrator não localizado; usando placeholder "
                "%s para log de comandos.",
                self.plesk_migrator_bin,
            )
            return
        raise PhaseExecutionError(
            "plesk-migrator não localizado no servidor. Rode a fase "
            "'install' antes (sem --skip-install) ou aponte "
            "paths.plesk_migrator_bin no YAML."
        )

    @staticmethod
    def _sql_escape(value: str) -> str:
        """Escape minimal pra SQL string literal. Não suporta NULL/bytes.
        Use só pra nomes de domínio/limit_name (alfa+pontos)."""
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    def _run_plesk_db(self, sql: str, *, fetch: bool = False,
                      log_to: pathlib.Path | None = None) -> str:
        """Executa SQL via `plesk db -Nse`. Retorna stdout (vazio em dry_run
        ou UPDATE/DELETE). Use fetch=True quando precisa parse output."""
        if not self.plesk_bin:
            raise PhaseExecutionError(
                "_run_plesk_db: binário 'plesk' não localizado"
            )
        compact = " ".join(sql.split())
        if self.dry_run and not self._is_read_only(["plesk", "db"]):
            self.logger.info("[DRY-RUN] plesk db -Nse \"%s\"", compact[:300])
            return ""
        try:
            proc = subprocess.run(
                [str(self.plesk_bin), "db", "-Nse", sql],
                capture_output=True, text=True, timeout=300, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PhaseExecutionError(
                f"_run_plesk_db: falha executando: {exc}"
            ) from exc
        if proc.returncode != 0:
            raise PhaseExecutionError(
                f"_run_plesk_db: rc={proc.returncode} stderr={proc.stderr.strip()[:300]}"
            )
        if log_to is not None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with log_to.open("a", encoding="utf-8") as fh:
                    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                    fh.write(f"# {ts} SQL\n{sql}\n# rows affected (best-effort):\n{proc.stdout}\n\n")
            except OSError:
                pass
        return proc.stdout

    def _read_failed_set(self, path: pathlib.Path) -> set[str]:
        """Extrai conjunto de domínios FQDN-válidos de
        failed-subscriptions.<ts>. Usa o mesmo parser de
        _load_migrated_domains (regex FQDN + skip cabeçalhos)."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return set()
        out: set[str] = set()
        for ln in lines:
            stripped = ln.strip()
            if not stripped or stripped.startswith("#") or ":" in stripped:
                continue
            candidate = stripped.lower()
            if self._FQDN_RE.match(candidate):
                out.add(candidate)
        return out

    def _list_mail_accounts(self, domain: str) -> list[str]:
        """Lista contas de e-mail de um domínio via `plesk bin mail --list`.
        Cache em self._mail_accounts_cache (lazy). Retorna lista de endereços
        completos (user@dom). Em dry_run sem binário plesk, retorna []."""
        cache = getattr(self, "_mail_accounts_cache", None)
        if cache is None:
            cache = {}
            self._mail_accounts_cache = cache
        if domain in cache:
            return cache[domain]
        if not self.plesk_bin:
            cache[domain] = []
            return []
        try:
            proc = subprocess.run(
                [str(self.plesk_bin), "bin", "mail", "--list", "-domain", domain],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("_list_mail_accounts(%s): %s", domain, exc)
            cache[domain] = []
            return []
        if proc.returncode != 0:
            self.logger.debug("plesk bin mail --list -domain %s rc=%d stderr=%s",
                              domain, proc.returncode, proc.stderr.strip()[:200])
            cache[domain] = []
            return []
        suffix = f"@{domain.lower()}"
        accounts: list[str] = []
        for line in proc.stdout.splitlines():
            line = line.strip().lower()
            if not line.endswith(suffix):
                continue
            accounts.append(line)
        cache[domain] = accounts
        return accounts

    _FQDN_RE = re.compile(
        r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
    )

    def _load_migrated_domains(self) -> list[str]:
        """Retorna domínios migrados na sessão atual lendo, em ordem:
        successful-subscriptions.<ts> (mais recente), subscriptions-status.json,
        subscriptions-report.json. Vazio = nenhuma evidência de migração.

        Formato de successful-subscriptions.<ts> (Plesk Migrator nativo):
            # Admin subscriptions and customers
                Customer: <name>
                        domain1.example
                        domain2.example
                Reseller: <name>
                Plan: <name>
                        ...
        Parser aceita só linhas FQDN-válidas (regex), descarta cabeçalhos
        (Customer:/Reseller:/Plan:/...) e comentários."""
        session_dir = self.sessions_dir / self.session_name
        if not session_dir.is_dir():
            return []

        candidates = sorted(session_dir.glob("successful-subscriptions.*"))
        for cand in reversed(candidates):
            if cand.suffix == ".bak":
                continue
            try:
                lines = cand.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                self.logger.warning("_load_migrated_domains: falha lendo %s: %s",
                                    cand, exc)
                continue
            domains: list[str] = []
            for ln in lines:
                stripped = ln.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if ":" in stripped:
                    continue
                candidate = stripped.lower()
                if self._FQDN_RE.match(candidate):
                    domains.append(candidate)
            if domains:
                self.logger.debug(
                    "_load_migrated_domains: %d domínio(s) de %s",
                    len(domains), cand.name,
                )
                return domains

        for jsonfile in ("subscriptions-status.json", "subscriptions-report.json"):
            path = session_dir / jsonfile
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                self.logger.warning("fix_docroot: falha lendo %s: %s", path, exc)
                continue
            if isinstance(data, dict):
                ok_states = {"completed", "success", "successful", "done", "ok"}
                domains = [
                    name for name, info in data.items()
                    if isinstance(info, dict) and (
                        str(info.get("status", "")).lower() in ok_states
                        or str(info.get("state", "")).lower() in ok_states
                    )
                ]
                if not domains:
                    domains = list(data.keys())
                if domains:
                    self.logger.debug("fix_docroot: %d domínios carregados de %s",
                                      len(domains), path.name)
                    return domains
        return []

    _OWNER_HEADER_RE = re.compile(
        r"^\s*(Customer|Reseller|Plan)\s*:\s*(.+?)\s*$", re.IGNORECASE
    )
    _LOGIN_SLUG_RE = re.compile(r"[^a-z0-9]+")

    def _load_migrated_owners(self) -> dict[str, list[str]]:
        """Parseia successful-subscriptions.<ts> mais recente, retorna
        {customer_name: [domains]} apenas para domínios sob bloco `Customer:`.
        Domínios sob `Reseller:`/`Plan:`/Admin não entram (ficam em
        cl_id=0 — comportamento padrão). Vazio = nada para reatribuir."""
        session_dir = self.sessions_dir / self.session_name
        if not session_dir.is_dir():
            return {}
        candidates = sorted(session_dir.glob("successful-subscriptions.*"))
        candidates = [c for c in candidates if c.suffix != ".bak"]
        if not candidates:
            return {}
        cand = candidates[-1]
        try:
            lines = cand.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            self.logger.warning(
                "_load_migrated_owners: falha lendo %s: %s", cand, exc
            )
            return {}
        owners: dict[str, list[str]] = {}
        current: str | None = None
        for raw in lines:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = self._OWNER_HEADER_RE.match(raw)
            if m:
                kind = m.group(1).lower()
                current = m.group(2).strip() if kind == "customer" else None
                continue
            candidate = stripped.lower()
            if not self._FQDN_RE.match(candidate):
                continue
            if current is not None:
                owners.setdefault(current, []).append(candidate)
        return owners

    def _slugify_login(self, name: str) -> str:
        """Login Plesk: lowercase [a-z0-9_], começa com letra, max 32 chars."""
        s = self._LOGIN_SLUG_RE.sub("_", name.lower()).strip("_")
        if not s:
            s = "customer"
        if not s[0].isalpha():
            s = f"c_{s}"
        return s[:32]

    @staticmethod
    def _csv_quote(v: str) -> str:
        if any(c in v for c in (',', '"', '\n', '\r')):
            return '"' + v.replace('"', '""') + '"'
        return v

    def ensure_plesk_migrator_installed(self) -> None:
        self.logger.info("Fase: ensure_plesk_migrator_installed")
        if not self.plesk_extension_bin:
            if self.dry_run:
                self.logger.info(
                    "[DRY-RUN] binário 'extension' não localizado; assumindo "
                    "panel-migrator ausente e simulando install."
                )
                self.plesk_extension_bin = pathlib.Path(
                    _EXTENSION_BIN_CANDIDATES[0]
                )
            else:
                raise PreflightError(
                    "Binário 'extension' do Plesk não localizado — este host "
                    "não parece ser um servidor Plesk Obsidian. Veja log de "
                    "auto-discovery."
                )
        if not self.plesk_bin:
            if self.dry_run:
                self.plesk_bin = pathlib.Path(_PLESK_BIN_CANDIDATES[0])
            else:
                raise PreflightError(
                    "Binário 'plesk' não localizado — necessário para "
                    "invocar 'plesk installer'."
                )

        result = self._run(
            [str(self.plesk_extension_bin), "--list"], check=False
        )
        if "panel-migrator" in (result.stdout or "").lower():
            self.logger.info("panel-migrator já instalado.")
            self._discover_paths()
            return

        self.logger.info(
            "panel-migrator ausente. Instalando via plesk installer…"
        )
        self._run(
            [
                str(self.plesk_bin), "installer",
                "--select-release-current",
                "--install-component", "panel-migrator",
            ],
            timeout=TIMEOUT_INSTALL,
            log_to=self.log_dir / "install.log",
        )

        if self.dry_run:
            self.logger.info("[DRY-RUN] verificação pós-install pulada")
            return

        check = self._run(
            [str(self.plesk_extension_bin), "--list"], check=False
        )
        if "panel-migrator" not in (check.stdout or "").lower():
            raise PreflightError(
                "Instalação aparente OK mas panel-migrator ainda não aparece "
                "em 'extension --list'."
            )

        # Re-discovery: agora plesk-migrator e diretório do módulo existem.
        self._discover_paths()
        if not self.plesk_migrator_bin:
            raise PreflightError(
                "Install reportou OK mas binário plesk-migrator não foi "
                "localizado após auto-discovery. Verifique o log de install."
            )

        # Sanity pós-install: `plesk-migrator help` deve responder OK,
        # confirmando que o binário não está corrompido ou faltando libs.
        help_result = self._run(
            [str(self.plesk_migrator_bin), "help"], check=False
        )
        if help_result.returncode != 0:
            raise PreflightError(
                f"plesk-migrator help retornou rc={help_result.returncode} "
                "após install — install pode estar corrompido. Recomendado "
                "reinstalar via `plesk installer --reinstall-patch "
                "--install-component panel-migrator`."
            )
        self.logger.info("✓ plesk-migrator operacional (help rc=0)")

    def generate_config_ini(self) -> pathlib.Path:
        self.logger.info("Fase: generate_config_ini")
        src = self.config["source"]
        dst = self.config["dest"]

        # `source-servers` em [GLOBAL] aponta para o NOME da seção que descreve
        # cada servidor de origem (docs Plesk). Mantemos o link explícito numa
        # variável para que renomear a seção não exija atualizar dois lugares.
        SOURCE_SECTION = "cpanel"

        cfg = configparser.ConfigParser()
        cfg["GLOBAL"] = {
            "source-type": "cpanel",
            "source-servers": SOURCE_SECTION,
            "target-type": "plesk",
        }
        cfg["plesk"] = {
            "ip": dst["host"],
            "os": "unix",
        }
        source_server = {
            "ip": src["host"],
            "os": "unix",
            "ssh-password": src["ssh_password"],
        }
        if int(src.get("ssh_port", 22)) != 22:
            source_server["ssh-port"] = str(src["ssh_port"])
        if src.get("postgres_password"):
            source_server["postgres-password"] = src["postgres_password"]
        cfg[SOURCE_SECTION] = source_server

        config_path = self.conf_dir / "config.ini"

        if self.dry_run:
            self.logger.info(
                "[DRY-RUN] config.ini que seria escrito em %s:", config_path
            )
            import io
            buf = io.StringIO()
            cfg.write(buf)
            for line in buf.getvalue().splitlines():
                self.logger.info("    %s", self._mask(line))
            return config_path

        try:
            self.conf_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise PhaseExecutionError(
                f"Sem permissão para criar {self.conf_dir}: {exc}"
            )

        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass

        fd = os.open(
            str(config_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(fd, "w") as fh:
                cfg.write(fh)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.chmod(config_path, 0o600)
        self.logger.info("config.ini criado em %s (chmod 600)", config_path)
        return config_path

    def preflight_checks(self) -> None:
        self.logger.info("Fase: preflight_checks")
        if self.dry_run:
            self.logger.info(
                "[DRY-RUN] plesk-migrator check pulado — depende de config.ini "
                "e migration-list reais (não escritos em dry-run)."
            )
            return
        self._require_plesk_migrator_bin()
        self._require_runtime_state()
        try:
            self._run(
                [str(self.plesk_migrator_bin), "check"],
                timeout=TIMEOUT_CHECK,
                log_to=self.log_dir / "preflight.log",
            )
        except subprocess.CalledProcessError as exc:
            raise PreflightError(
                f"plesk-migrator check falhou (rc={exc.returncode}). "
                f"Veja {self.log_dir / 'preflight.log'}"
            )

    def generate_migration_list(self) -> pathlib.Path:
        self.logger.info("Fase: generate_migration_list")
        self._require_plesk_migrator_bin()
        session_dir = self.sessions_dir / self.session_name
        migration_list = session_dir / "migration-list"

        if migration_list.exists():
            if self.resume:
                ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
                backup = migration_list.with_suffix(f".pre-resume.{ts}.bak")
                if not self.dry_run:
                    shutil.copy2(migration_list, backup)
                self.logger.warning(
                    "Resume: migration-list existente preservada "
                    "(backup: %s). Pulando geração.", backup,
                )
                return migration_list
            if not self.force_regenerate:
                raise PhaseExecutionError(
                    f"migration-list já existe em {migration_list}. "
                    "Use --resume para retomar OU "
                    "--force-regenerate para sobrescrever."
                )

        if self.force_regenerate and migration_list.exists() and not self.dry_run:
            backup = migration_list.with_suffix(".pre-regenerate.bak")
            shutil.copy2(migration_list, backup)
            self.logger.info("Backup pré-regenerate em %s", backup)

        self._run(
            [str(self.plesk_migrator_bin), "generate-migration-list"],
            timeout=TIMEOUT_GENERATE_LIST,
            log_to=self.log_dir / "generate-migration-list.log",
        )

        if self.dry_run:
            return migration_list

        if not migration_list.exists():
            raise PhaseExecutionError(
                f"migration-list não foi criado em {migration_list} "
                "após generate-migration-list."
            )

        with migration_list.open("r", encoding="utf-8", errors="replace") as fh:
            line_count = sum(1 for _ in fh)
        if line_count == 0:
            raise PhaseExecutionError(
                f"migration-list em {migration_list} foi gerada vazia. "
                "Verifique conexão SSH, credenciais e se há contas válidas "
                "na origem."
            )
        self.logger.info(
            "migration-list gerada (%d linhas) em %s",
            line_count, migration_list,
        )
        return migration_list

    def sanitize_list(self, *, apply_renames: bool = False) -> None:
        """Detecta hostnames na migration-list cujo primeiro label é reservado
        pelo Plesk (webmail, mail, ftp, etc). Em modo apply_renames, reescreve
        migration-list trocando pelo rename configurado em
        migration.reserved_renames (fallback DEFAULT_RESERVED_RENAME).
        Sempre grava report em <log_dir>/reserved-subdomains-report.csv.
        Idempotente: já renomeados não casam mais com lista reservada."""
        self.logger.info("Fase: sanitize_list (apply_renames=%s)", apply_renames)
        if not self.sessions_dir:
            self.logger.warning("sanitize_list: sessions_dir indefinido — skip")
            return
        migration_list = self.sessions_dir / self.session_name / "migration-list"
        if not migration_list.is_file():
            self.logger.info("sanitize_list: migration-list inexistente — skip "
                             "(rode --only-phase list primeiro)")
            return

        migration_cfg = self.config.get("migration") or {}
        rename_map: dict[str, str] = dict(DEFAULT_RESERVED_RENAME)
        user_map = migration_cfg.get("reserved_renames") or {}
        if isinstance(user_map, dict):
            rename_map.update({str(k): str(v) for k, v in user_map.items()})

        try:
            content = migration_list.read_text(encoding="utf-8")
        except OSError as exc:
            self.logger.warning("sanitize_list: falha lendo %s: %s",
                                migration_list, exc)
            return
        lines = content.splitlines(keepends=True)

        host_re = re.compile(
            r"^(?P<indent>\s*)(?P<host>[a-z0-9][a-z0-9.-]*\.[a-z0-9.-]+)"
            r"(?P<rest>\s*)$",
            re.IGNORECASE,
        )

        renames: list[tuple[str, str, str]] = []  # (original, proposed, line_excerpt)
        out_lines: list[str] = []
        for raw in lines:
            m = host_re.match(raw)
            if not m or ":" in raw:
                out_lines.append(raw)
                continue
            host = m.group("host").lower()
            first_label = host.split(".", 1)[0]
            if first_label not in RESERVED_PLESK_SUBDOMAINS:
                out_lines.append(raw)
                continue
            replacement_label = rename_map.get(first_label)
            if not replacement_label:
                out_lines.append(raw)
                continue
            new_host = f"{replacement_label}.{host.split('.', 1)[1]}"
            renames.append((host, new_host, raw.rstrip()))
            if apply_renames:
                out_lines.append(
                    f"{m.group('indent')}{new_host}{m.group('rest')}"
                )
            else:
                out_lines.append(raw)

        # Grava report (sempre)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            report = self.log_dir / "reserved-subdomains-report.csv"
            new_file = not report.exists()
            with report.open("a", encoding="utf-8") as fh:
                if new_file:
                    fh.write("timestamp,original,proposed,reason\n")
                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                for original, proposed, _excerpt in renames:
                    fh.write(f"{ts},{original},{proposed},reserved-plesk-subdomain\n")
        except OSError as exc:
            self.logger.warning("sanitize_list: falha gravando report: %s", exc)

        if not renames:
            self.logger.info("sanitize_list: 0 subdomains reservados encontrados.")
            return

        self.logger.warning(
            "sanitize_list: %d subdomain(s) reservado(s) detectado(s):",
            len(renames),
        )
        for original, proposed, _ in renames:
            self.logger.warning("  %s → %s", original, proposed)

        if not apply_renames:
            self.logger.warning(
                "sanitize_list: --rename-reserved-subdomains não passado — "
                "migration-list intocada. Aplicação cliente pode quebrar se "
                "referenciar URLs/hostnames. Para aplicar: --rename-reserved-subdomains."
            )
            return

        if self.dry_run:
            self.logger.info(
                "[DRY-RUN] sanitize_list: escreveria %d rename(s) em %s",
                len(renames), migration_list,
            )
            return

        # Backup + escrita
        ts_compact = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = migration_list.with_suffix(
            migration_list.suffix + f".pre-sanitize.{ts_compact}.bak"
        )
        try:
            shutil.copy2(migration_list, backup)
            migration_list.write_text("".join(out_lines), encoding="utf-8")
        except OSError as exc:
            raise PhaseExecutionError(
                f"sanitize_list: falha gravando migration-list: {exc}"
            ) from exc

        self.logger.info(
            "sanitize_list: %d rename(s) aplicado(s); backup em %s",
            len(renames), backup,
        )

    def filter_migration_list(
        self,
        allowlist: list[str] | None = None,
        denylist: list[str] | None = None,
    ) -> None:
        # Filtro local foi desabilitado: ver _validate_config. A validação
        # já garante allowlist/denylist vazios; este método existe apenas
        # para manter a fase no pipeline (no-op informativo).
        self.logger.info(
            "Fase: filter_migration_list — desabilitada (filtro local pode "
            "corromper migration-list estruturada). No-op."
        )

    def transfer_accounts(
        self,
        *,
        skip_web: bool = False,
        skip_mail: bool = False,
        skip_db: bool = False,
        start_from: str | None = None,
    ) -> None:
        self.logger.info("Fase: transfer_accounts")
        self._require_plesk_migrator_bin()
        self._require_runtime_state()
        cmd = [str(self.plesk_migrator_bin), "transfer-accounts"]
        if skip_web:
            cmd.append("--skip-copy-web-content")
        if skip_mail:
            cmd.append("--skip-copy-mail-content")
        if skip_db:
            cmd.append("--skip-copy-db-content")
        if start_from:
            cmd.extend(["--start-from", start_from])
            self.logger.info(
                "transfer-accounts retomando a partir de step: %s",
                start_from,
            )
        # EXTEND: --migration-list-file <path>
        # EXTEND: --skip-services-checks
        self._run(
            cmd,
            timeout=TIMEOUT_TRANSFER,
            log_to=self.log_dir / "transfer-accounts.log",
        )

    def fix_limits(self) -> None:
        """Zera Limits.* (mbox_quota, max_box, max_subdom, max_db, etc) das
        subscriptions migradas. Sem isso, Plesk recusa criação de mailboxes
        novas com 'available: 0'. Aplica via SQL direto (sem CLI estável
        nesta versão Plesk para subscription unplanned). Roda `plesk repair
        db -y` ao final para invalidar cache de limits.

        Idempotente: UPDATE para -1 em valores já -1 = no-op."""
        self.logger.info("Fase: fix_limits")
        if not self.plesk_bin:
            self.logger.warning("fix_limits: binário 'plesk' não localizado — skip")
            return
        domains = self._load_migrated_domains()
        if not domains:
            self.logger.warning("fix_limits: 0 domínios — skip")
            return

        targets = (
            "mbox_quota", "max_box", "max_subdom", "max_subftp_users",
            "max_db", "max_maillists", "max_resp", "max_traffic",
            "max_unlim_db_users", "disk_space", "max_site_builder",
            "max_wu", "max_dom_aliases", "max_webapps",
        )

        updated = 0
        for dom in domains:
            for lim in targets:
                sql = (
                    f"UPDATE Limits SET value='-1' "
                    f"WHERE id=(SELECT limits_id FROM domains "
                    f"WHERE name='{self._sql_escape(dom)}') "
                    f"AND limit_name='{self._sql_escape(lim)}';"
                )
                self._run_plesk_db(
                    sql, log_to=self.log_dir / "fix-limits.log"
                )
            updated += 1
            self.logger.info("fix_limits: %s — Limits.* SET -1", dom)

        if updated and not self.dry_run:
            try:
                self._run(
                    [str(self.plesk_bin), "repair", "db", "-y"],
                    timeout=TIMEOUT_FIX_LIMITS,
                    log_to=self.log_dir / "fix-limits.log",
                    check=False,
                )
            except (PhaseExecutionError, subprocess.CalledProcessError) as exc:
                self.logger.warning(
                    "fix_limits: `plesk repair db` falhou (não-fatal): %s", exc
                )

        self.logger.info("fix_limits: %d subscription(s) atualizada(s)", updated)

    @staticmethod
    def _parse_reserved_subdomain_failures(
        session_dir: pathlib.Path,
    ) -> set[str]:
        """Parse the newest accounts_report_tree.* in `session_dir` and return
        the set of first-label segments of subdomains that plesk-migrator
        failed to create with the message:
          "sites of subscription were not created - they do not exist on
           target panel: '<host>'"
        Returns the set of `host.split('.', 1)[0]` for every match (e.g.
        {"webmail"} when webmail.example.com failed).

        Missing session dir, no matching report, and unreadable files all
        yield set(). Filenames sorted lexicographically — the timestamped
        suffix is wide enough to keep that aligned with chronological order
        for plesk-migrator output."""
        if not session_dir.is_dir():
            return set()
        candidates = sorted(
            p for p in session_dir.glob("accounts_report_tree.*")
            if p.is_file() and ".json" not in p.suffixes
        )
        if not candidates:
            return set()
        latest = candidates[-1]
        try:
            text = latest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set()
        pattern = re.compile(
            r"sites of subscription were not created[^:]*:\s+'([^']+)'"
        )
        labels: set[str] = set()
        for host in pattern.findall(text):
            first = host.split(".", 1)[0].lower()
            if first:
                labels.add(first)
        return labels

    def _domain_exists_in_plesk(self, domain: str) -> bool:
        """True iff `domain` has a row in psa.domains. Uses `plesk db -Nse`
        via _run_plesk_db. In dry_run returns True (don't block recovery
        logic during planning). Any PhaseExecutionError from the SQL path
        is swallowed and treated as not-exists, since the caller wants a
        conservative bool, not a halt."""
        if self.dry_run:
            return True
        sql = (
            "SELECT COUNT(*) FROM domains WHERE name='"
            f"{self._sql_escape(domain)}'"
        )
        try:
            out = self._run_plesk_db(sql, fetch=True)
        except PhaseExecutionError:
            return False
        return out.strip() == "1"

    def _subscription_only_reserved_failures(
        self, domain: str, session_dir: pathlib.Path,
    ) -> bool:
        """Classify a `failed-subscriptions` entry as recoverable.

        Returns True iff:
          1. The newest accounts_report_tree.* contains at least one
             "sites of subscription were not created" failure.
          2. ALL such failures are first-label members of
             RESERVED_PLESK_SUBDOMAINS (webmail, mail, ftp, ...).
          3. `domain` already exists in psa.domains (subscription was
             created by plesk-migrator before the reserved subdomain hit
             the wall — so we have working hosting, just not the blocked
             subdomain that Plesk would refuse anyway).

        Used by retransfer_failed to skip retrying domains that will never
        succeed (Plesk reserves these subdomain names) but whose parent
        subscription is already operational."""
        labels = self._parse_reserved_subdomain_failures(session_dir)
        if not labels:
            return False
        if not labels.issubset(set(RESERVED_PLESK_SUBDOMAINS)):
            return False
        return self._domain_exists_in_plesk(domain)

    def retransfer_failed(
        self, *, max_attempts: int = MAX_RETRANSFER_ATTEMPTS,
    ) -> None:
        """Re-roda plesk-migrator transfer-accounts contra failed-subscriptions
        mais recente até 0 falhas, max_attempts esgotado, ou mesma lista 2x
        consecutivas (progresso estagnado). Sem failed → no-op imediato.
        Idempotente."""
        self.logger.info(
            "Fase: retransfer_failed (max_attempts=%d)", max_attempts,
        )
        self._require_plesk_migrator_bin()
        session_dir = self.sessions_dir / self.session_name
        if not session_dir.is_dir():
            self.logger.warning("retransfer_failed: session dir ausente — skip")
            return

        previous_set: set[str] | None = None
        for attempt in range(1, max_attempts + 1):
            failed_files = [
                f for f in sorted(session_dir.glob("failed-subscriptions.*"))
                if f.suffix != ".bak"
            ]
            if not failed_files:
                self.logger.info("retransfer_failed: 0 falhas pendentes — done")
                return
            latest = failed_files[-1]
            current_set = self._read_failed_set(latest)
            if not current_set:
                self.logger.info(
                    "retransfer_failed: %s sem domínios parseáveis — done",
                    latest.name,
                )
                return
            if previous_set is not None and previous_set == current_set:
                recoverable = {
                    dom for dom in current_set
                    if self._subscription_only_reserved_failures(dom, session_dir)
                }
                unrecoverable = current_set - recoverable
                if recoverable:
                    self.logger.warning(
                        "retransfer_failed: %d subscription(s) com falha "
                        "apenas em subdomains reservados — Plesk gerencia "
                        "webmail nativo — aceita como partial success: %s",
                        len(recoverable), sorted(recoverable),
                    )
                if unrecoverable:
                    self.logger.error(
                        "retransfer_failed: %d subscription(s) com falhas "
                        "reais em 2 iterações consecutivas — aborta loop. "
                        "Inspecione: %s",
                        len(unrecoverable), sorted(unrecoverable),
                    )
                    raise PhaseExecutionError(
                        "retransfer_failed: progresso estagnado"
                    )
                return
            self.logger.info(
                "retransfer_failed: tentativa %d/%d — %d subscription(s) "
                "em %s",
                attempt, max_attempts, len(current_set), latest.name,
            )
            try:
                self._run(
                    [str(self.plesk_migrator_bin), "transfer-accounts",
                     "--migration-list-file", str(latest)],
                    timeout=TIMEOUT_RETRANSFER,
                    log_to=self.log_dir / f"retransfer-attempt-{attempt}.log",
                    check=False,
                )
            except (PhaseExecutionError, subprocess.CalledProcessError) as exc:
                self.logger.warning(
                    "retransfer_failed: tentativa %d falhou (continua loop): %s",
                    attempt, exc,
                )
            previous_set = current_set

        # max_attempts reached without zero-failures. Apply the same
        # recoverable/unrecoverable classification as the stagnation path —
        # exhaustion alone should not silently continue when the remaining
        # failures are real.
        final_failed_files = [
            f for f in sorted(session_dir.glob("failed-subscriptions.*"))
            if f.suffix != ".bak"
        ]
        final_set: set[str] = (
            self._read_failed_set(final_failed_files[-1])
            if final_failed_files else set()
        )
        recoverable = {
            dom for dom in final_set
            if self._subscription_only_reserved_failures(dom, session_dir)
        }
        unrecoverable = final_set - recoverable
        if recoverable:
            self.logger.warning(
                "retransfer_failed: %d tentativa(s) esgotada(s); %d "
                "subscription(s) com falha apenas em subdomains "
                "reservados aceita(s) como partial success: %s",
                max_attempts, len(recoverable), sorted(recoverable),
            )
        if unrecoverable:
            self.logger.error(
                "retransfer_failed: %d tentativa(s) esgotada(s). Falhas "
                "reais em %s: %s",
                max_attempts, session_dir, sorted(unrecoverable),
            )
            raise PhaseExecutionError(
                "retransfer_failed: tentativas esgotadas com falhas reais"
            )
        if not recoverable:
            self.logger.warning(
                "retransfer_failed: %d tentativa(s) esgotada(s). Falhas "
                "em %s", max_attempts, session_dir,
            )

    def copy_web_content(self) -> None:
        self.logger.info("Fase: copy_web_content")
        self._require_plesk_migrator_bin()
        self._require_runtime_state()
        self._run(
            [str(self.plesk_migrator_bin), "copy-web-content"],
            timeout=TIMEOUT_COPY_CONTENT,
            log_to=self.log_dir / "copy-web.log",
        )

    def copy_mail_content(self) -> None:
        self.logger.info("Fase: copy_mail_content")
        self._require_plesk_migrator_bin()
        self._require_runtime_state()
        self._run(
            [str(self.plesk_migrator_bin), "copy-mail-content"],
            timeout=TIMEOUT_COPY_CONTENT,
            log_to=self.log_dir / "copy-mail.log",
        )

    @staticmethod
    def _dir_manifest(path: pathlib.Path) -> tuple[int, int, str]:
        """Stat-only fingerprint of a directory tree.

        Returns (file_count, total_bytes, md5_hex). md5_hex hashes
        '\n'.join(sorted "relpath:size") — content bytes are NOT read,
        so it is fast even on multi-GB web trees and stable across rsync
        (mtime/atime ignored). Two paths with identical manifest_hash hold
        the same files (by name + size). Missing path or stat errors yield
        (0, 0, md5("")). followlinks=False to avoid infinite loops on
        symlinked vhosts."""
        entries: list[str] = []
        count = 0
        total = 0
        if path.is_dir():
            for root, dirs, files in os.walk(path, followlinks=False):
                dirs.sort()
                for fname in sorted(files):
                    fp = pathlib.Path(root) / fname
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    rel = fp.relative_to(path)
                    entries.append(f"{rel}:{st.st_size}")
                    count += 1
                    total += st.st_size
        digest = hashlib.md5("\n".join(entries).encode("utf-8")).hexdigest()
        return count, total, digest

    def _remote_dir_manifest(
        self, remote_path: str,
    ) -> tuple[int, int, str]:
        """Cross-server analogue of _dir_manifest.

        Runs over SSH on the cPanel source host (host/port/password from
        self.config["source"]). Pipeline:

          find <path> -type f -printf '%P:%s\n' | sort -u > /tmp/m.$$
          COUNT=$(wc -l < /tmp/m.$$)
          TOTAL=$(awk -F: '{s+=$NF} END{print s+0}' /tmp/m.$$)
          MD5=$(md5sum /tmp/m.$$ | awk '{print $1}')
          # but md5sum hashes the file bytes — we want md5 over the
          # joined-by-newlines body, identical to _dir_manifest. So we
          # hash the file directly: md5sum < /tmp/m.$$ → that's the same
          # because each entry is one line "p:s\n", joined by newlines.
          # However _dir_manifest uses "\n".join (NO trailing \n). To
          # match, strip the trailing newline server-side before md5sum.

        Output format:
          COUNT=<n>\nTOTAL=<b>\nMD5=<hex>\n

        Returns (0, 0, "") on any failure (ssh missing, sshpass missing,
        non-zero rc, malformed output, dry_run). Never raises — caller
        treats divergence as a warning only."""
        if self.dry_run:
            return 0, 0, ""
        src = self.config.get("source") or {}
        host = src.get("host")
        password = src.get("ssh_password")
        port = int(src.get("ssh_port", 22))
        if not host or not password:
            return 0, 0, ""
        # Shell pipeline. `printf '%s' "$body"` strips the trailing newline
        # so md5 matches _dir_manifest's "\n".join semantics exactly.
        remote_cmd = (
            f"set -e; "
            f"body=$(find {shlex.quote(remote_path)} -type f "
            f"-printf '%P:%s\\n' 2>/dev/null | sort -u); "
            f"count=$(printf '%s\\n' \"$body\" | grep -c '^' || true); "
            f"if [ -z \"$body\" ]; then count=0; fi; "
            f"total=$(printf '%s\\n' \"$body\" | awk -F: '{{s+=$NF}} "
            f"END{{print s+0}}'); "
            f"md5=$(printf '%s' \"$body\" | md5sum | awk '{{print $1}}'); "
            f"echo \"COUNT=$count\"; echo \"TOTAL=$total\"; echo \"MD5=$md5\""
        )
        argv = [
            "sshpass", "-p", str(password),
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=15",
            "-o", "BatchMode=no",
            "-p", str(port),
            f"root@{host}",
            remote_cmd,
        ]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=180, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.logger.warning(
                "_remote_dir_manifest: ssh falhou: %s", exc,
            )
            return 0, 0, ""
        if proc.returncode != 0:
            self.logger.warning(
                "_remote_dir_manifest: rc=%d stderr=%s",
                proc.returncode, proc.stderr.strip()[:200],
            )
            return 0, 0, ""
        count = total = 0
        digest = ""
        for line in proc.stdout.splitlines():
            if line.startswith("COUNT="):
                try:
                    count = int(line.split("=", 1)[1])
                except ValueError:
                    return 0, 0, ""
            elif line.startswith("TOTAL="):
                try:
                    total = int(line.split("=", 1)[1])
                except ValueError:
                    return 0, 0, ""
            elif line.startswith("MD5="):
                digest = line.split("=", 1)[1].strip()
        if not digest:
            return 0, 0, ""
        return count, total, digest

    @staticmethod
    def _pick_docroot(
        manifests: dict[str, tuple[int, int, str]],
    ) -> str | None:
        """Decide which candidate `<vhost>/<name>` should be www-root.

        Input: {candidate_name: (file_count, total_bytes, manifest_hash)}.
        Returns the candidate name to point www-root at, or None when no
        action is needed:
          - all candidates empty
          - httpdocs already has content (canonical wins, even if another
            candidate also has content — we don't move a working site)
          - the richest non-canonical candidate has the same manifest_hash
            as httpdocs (symlinked / hardlinked / prior partial fix)

        Tie-break on total_bytes picks the first key in insertion order,
        which matches DOCROOT_CANDIDATES ordering."""
        httpdocs = manifests.get("httpdocs", (0, 0, ""))
        rich = {k: v for k, v in manifests.items() if v[0] > 0}
        if not rich:
            return None
        if httpdocs[0] > 0:
            return None
        # httpdocs is empty; pick the heaviest non-httpdocs candidate
        best_name = max(
            (k for k in rich if k != "httpdocs"),
            key=lambda k: rich[k][1],
            default=None,
        )
        if best_name is None:
            return None
        if rich[best_name][2] == httpdocs[2]:
            return None
        return best_name

    def fix_docroot(self) -> None:
        """Ajusta www-root das subscriptions migradas detectando onde
        plesk-migrator depositou o conteúdo. Escaneia DOCROOT_CANDIDATES
        em /var/www/vhosts/<domain>/, gera manifest stat-only por path
        (count + bytes + md5 de filename:size), e via _pick_docroot
        decide se ajusta.

        Idempotente:
          - httpdocs já populado → skip
          - tudo vazio → skip (vhost ainda não recebeu conteúdo)
          - melhor candidato hash-igual a httpdocs (symlink/hardlink) → skip
          - caso contrário → `plesk bin subscription -u <dom> -www-root <path>`

        Log detalhado em <log_dir>/fix-docroot.log: por domínio, todos os
        candidatos escaneados (count/bytes/hash truncado) + decisão."""
        self.logger.info("Fase: fix_docroot")
        if not self.plesk_bin:
            raise PhaseExecutionError(
                "fix_docroot: binário 'plesk' não localizado. "
                "Necessário para `plesk bin subscription`."
            )

        domains = self._load_migrated_domains()
        if not domains:
            self.logger.warning(
                "fix_docroot: nenhuma subscription migrada encontrada em %s — skip",
                self.sessions_dir / self.session_name,
            )
            return

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("fix_docroot: log_dir mkdir falhou: %s", exc)
        report = self.log_dir / "fix-docroot.log"

        vhosts_root = pathlib.Path("/var/www/vhosts")
        for domain in domains:
            vhost = vhosts_root / domain
            if not vhost.is_dir():
                self.logger.warning(
                    "fix_docroot: vhost %s ausente — skip", vhost
                )
                continue

            manifests: dict[str, tuple[int, int, str]] = {}
            for name in DOCROOT_CANDIDATES:
                manifests[name] = self._dir_manifest(vhost / name)

            scan_summary = ", ".join(
                f"{n}={c}f/{b}B/{h[:8]}"
                for n, (c, b, h) in manifests.items()
            )
            self.logger.info("fix_docroot: %s scan: %s", domain, scan_summary)

            try:
                with report.open("a", encoding="utf-8") as fh:
                    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                    fh.write(f"# {ts} {domain}\n")
                    for n, (c, b, h) in manifests.items():
                        fh.write(f"  {n}: count={c} bytes={b} hash={h}\n")
            except OSError as exc:
                self.logger.warning(
                    "fix_docroot: falha escrevendo report: %s", exc
                )

            choice = self._pick_docroot(manifests)
            if choice is None:
                self.logger.info(
                    "fix_docroot: %s nada a ajustar — skip", domain
                )
                continue

            target = vhost / choice
            self.logger.info(
                "fix_docroot: %s → apontando www-root para %s "
                "(%d arquivos, %d bytes)",
                domain, target, manifests[choice][0], manifests[choice][1],
            )
            if self.dry_run:
                self.logger.info(
                    "[DRY-RUN] %s bin subscription -u %s -www-root %s",
                    self.plesk_bin, domain, target,
                )
                continue

            self._run(
                [str(self.plesk_bin), "bin", "subscription",
                 "-u", domain, "-www-root", str(target)],
                timeout=TIMEOUT_FIX_DOCROOT,
                log_to=report,
            )

    def fix_mailpath(self, *, apply: bool = False) -> None:
        """Audita onde plesk-migrator depositou Maildirs vs onde o Plesk
        canonical lê (/var/qmail/mailnames/<dom>/<user>/Maildir/{cur,new,tmp}).

        Modo audit (default): só relata mismatches em <log_dir>/fix-mailpath.log.

        Modo apply (apply=True via --apply-mailpath-fix): rsync -a do path
        alternativo populado para canonical, depois `plesk repair mail -domain
        <dom>` (best-effort). Idempotente: skipa se canonical já tem conteúdo.
        Não remove origem (operador inspeciona antes de limpar)."""
        self.logger.info("Fase: fix_mailpath (apply=%s)", apply)
        if not self.plesk_bin:
            self.logger.warning(
                "fix_mailpath: binário 'plesk' não localizado — skip"
            )
            return

        domains = self._load_migrated_domains()
        if not domains:
            self.logger.warning(
                "fix_mailpath: nenhuma subscription migrada encontrada — skip"
            )
            return

        report_lines: list[str] = []
        mismatches = 0
        ok = 0
        empty = 0

        def _has_content(p: pathlib.Path) -> bool:
            for sub in ("cur", "new", "tmp"):
                d = p / sub
                if not d.is_dir():
                    continue
                try:
                    if any(d.iterdir()):
                        return True
                except OSError:
                    continue
            return False

        for domain in domains:
            accounts = self._list_mail_accounts(domain)
            if not accounts:
                self.logger.info("fix_mailpath: %s sem contas listadas — skip", domain)
                continue
            for full in accounts:
                user = full.split("@", 1)[0]
                canonical = pathlib.Path(
                    f"/var/qmail/mailnames/{domain}/{user}/Maildir"
                )
                alternatives = [
                    pathlib.Path(f"/var/qmail/mailnames/{domain}/{user}"),
                    pathlib.Path(f"/var/www/vhosts/{domain}/mail/{domain}/{user}"),
                    pathlib.Path(f"/var/www/vhosts/{domain}/mail/{user}"),
                ]
                if canonical.is_dir() and _has_content(canonical):
                    ok += 1
                    continue
                alt_found = next(
                    (a for a in alternatives
                     if a.is_dir() and _has_content(a) and a != canonical),
                    None,
                )
                if alt_found:
                    mismatches += 1
                    msg = (
                        f"MISMATCH: {full} → canonical vazio ({canonical}); "
                        f"conteúdo em {alt_found}"
                    )
                    self.logger.warning("fix_mailpath: %s", msg)
                    report_lines.append(msg)
                    if apply and not self.dry_run:
                        try:
                            canonical.mkdir(parents=True, exist_ok=True)
                            self._run(
                                ["rsync", "-a",
                                 f"{alt_found}/", f"{canonical}/"],
                                timeout=TIMEOUT_FIX_MAILPATH,
                                log_to=self.log_dir / "fix-mailpath.log",
                                check=False,
                            )
                            report_lines.append(
                                f"  APPLIED: rsync {alt_found}/ → {canonical}/"
                            )
                        except (PhaseExecutionError, subprocess.CalledProcessError,
                                OSError) as exc:
                            self.logger.error(
                                "fix_mailpath: rsync %s → %s falhou: %s",
                                alt_found, canonical, exc,
                            )
                            report_lines.append(
                                f"  FAILED rsync: {alt_found}/ → {canonical}/: {exc}"
                            )
                    elif apply and self.dry_run:
                        self.logger.info(
                            "[DRY-RUN] rsync -a %s/ %s/",
                            alt_found, canonical,
                        )
                else:
                    empty += 1
                    self.logger.debug(
                        "fix_mailpath: %s sem mensagens em qualquer path", full
                    )

        summary = (
            f"fix_mailpath summary: ok={ok} mismatch={mismatches} empty={empty} "
            f"total_audited={ok + mismatches + empty}"
        )
        self.logger.info(summary)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            report = self.log_dir / "fix-mailpath.log"
            with report.open("a", encoding="utf-8") as fh:
                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                fh.write(f"# {ts}\n{summary}\n")
                for line in report_lines:
                    fh.write(f"{line}\n")
                fh.write("\n")
        except OSError as exc:
            self.logger.warning("fix_mailpath: falha escrevendo report: %s", exc)

        if mismatches > 0:
            if apply and not self.dry_run and self.plesk_bin:
                self.logger.info("fix_mailpath: rodando `plesk repair mail` por domínio")
                for dom in domains:
                    try:
                        self._run(
                            [str(self.plesk_bin), "repair", "mail",
                             "-domain", dom, "-y"],
                            timeout=TIMEOUT_FIX_MAILPATH,
                            log_to=self.log_dir / "fix-mailpath.log",
                            check=False,
                        )
                    except (PhaseExecutionError, subprocess.CalledProcessError) as exc:
                        self.logger.warning(
                            "fix_mailpath: plesk repair mail %s falhou: %s",
                            dom, exc,
                        )
            else:
                self.logger.warning(
                    "fix_mailpath: %d caixa(s) com Maildir em path não-canônico. "
                    "Inspecione %s/fix-mailpath.log; para aplicar correção "
                    "automática: --apply-mailpath-fix.",
                    mismatches, self.log_dir,
                )

    def check_mail_passwords(self, *, reset: bool = False) -> None:
        """Audita contas com password NULL/vazio em psa.accounts. Se reset=True,
        gera nova senha por conta (secrets.token_urlsafe(16)) e aplica via
        `plesk bin mail -u <addr> -passwd <pwd>`. Resultado em CSV chmod 600."""
        self.logger.info("Fase: check_mail_passwords (reset=%s)", reset)
        if not self.plesk_bin:
            self.logger.warning(
                "check_mail_passwords: binário 'plesk' não localizado — skip"
            )
            return

        sql = (
            "SELECT CONCAT(m.mail_name, '@', d.name) AS addr "
            "FROM mail m "
            "JOIN domains d ON m.dom_id=d.id "
            "LEFT JOIN accounts a ON m.account_id=a.id "
            "WHERE a.password IS NULL OR a.password='' "
            "ORDER BY d.name, m.mail_name;"
        )
        if self.dry_run:
            self.logger.info(
                "[DRY-RUN] %s db -Nse \"%s\"", self.plesk_bin, sql.replace('"', '\\"')
            )
            return

        try:
            proc = subprocess.run(
                [str(self.plesk_bin), "db", "-Nse", sql],
                capture_output=True, text=True,
                timeout=TIMEOUT_CHECK_MAIL_PASSWORDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PhaseExecutionError(
                f"check_mail_passwords: falha chamando `plesk db`: {exc}"
            ) from exc
        if proc.returncode != 0:
            raise PhaseExecutionError(
                f"check_mail_passwords: plesk db rc={proc.returncode} "
                f"stderr={proc.stderr.strip()[:300]}"
            )

        addresses = [ln.strip() for ln in proc.stdout.splitlines()
                     if ln.strip() and "@" in ln]
        if not addresses:
            self.logger.info("check_mail_passwords: 0 contas sem senha.")
            return

        self.logger.warning(
            "check_mail_passwords: %d conta(s) sem senha em psa.accounts.",
            len(addresses),
        )
        for addr in addresses[:20]:
            self.logger.warning("  sem senha: %s", addr)
        if len(addresses) > 20:
            self.logger.warning("  ... e mais %d conta(s).", len(addresses) - 20)

        if not reset:
            self.logger.warning(
                "check_mail_passwords: rode com --reset-mail-passwords para "
                "gerar senhas novas (CSV em %s/mail-password-reset.csv).",
                self.log_dir,
            )
            return

        self.log_dir.mkdir(parents=True, exist_ok=True)
        csv_path = self.log_dir / "mail-password-reset.csv"
        new_file = not csv_path.exists()
        try:
            csv_path.touch(mode=0o600, exist_ok=True)
            os.chmod(csv_path, 0o600)
        except OSError as exc:
            raise PhaseExecutionError(
                f"check_mail_passwords: não consegui criar {csv_path}: {exc}"
            ) from exc

        reset_count = 0
        with csv_path.open("a", encoding="utf-8") as fh:
            if new_file:
                fh.write("timestamp,email,new_password\n")
            for addr in addresses:
                while True:
                    new_pwd = secrets.token_urlsafe(16)
                    if new_pwd[0] not in "-_":
                        break
                self.sensitive_values.append(new_pwd)
                try:
                    self._run(
                        [str(self.plesk_bin), "bin", "mail",
                         "-u", addr, "-passwd", new_pwd],
                        timeout=60,
                        log_to=self.log_dir / "check-mail-passwords.log",
                    )
                except (PhaseExecutionError, subprocess.CalledProcessError) as exc:
                    self.logger.error(
                        "check_mail_passwords: falha resetando %s: %s", addr, exc
                    )
                    continue
                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                fh.write(f"{ts},{addr},{new_pwd}\n")
                reset_count += 1

        try:
            os.chmod(csv_path, 0o600)
        except OSError:
            pass
        self.logger.warning(
            "check_mail_passwords: %d/%d senha(s) resetada(s). CSV: %s "
            "(distribua via canal seguro fora-de-banda).",
            reset_count, len(addresses), csv_path,
        )

    def fix_mail_quota(self) -> None:
        """Zera mbox_quota individual de contas mail migradas. Plesk-migrator
        preserva quota cPanel literal (`0` = ilimitado lá, = bloqueio aqui).
        Sem isso contas recebem 0 bytes mesmo após criação. Idempotente:
        `AND mbox_quota=0` evita re-aplicar."""
        self.logger.info("Fase: fix_mail_quota")
        if not self.plesk_bin:
            self.logger.warning("fix_mail_quota: binário 'plesk' não localizado — skip")
            return
        domains = self._load_migrated_domains()
        if not domains:
            self.logger.warning("fix_mail_quota: 0 domínios — skip")
            return
        placeholders = ",".join(
            f"'{self._sql_escape(d)}'" for d in domains
        )
        sql = (
            f"UPDATE mail SET mbox_quota=-1 "
            f"WHERE dom_id IN ("
            f"SELECT id FROM domains WHERE name IN ({placeholders})) "
            f"AND mbox_quota=0;"
        )
        self._run_plesk_db(
            sql, log_to=self.log_dir / "fix-mail-quota.log"
        )
        self.logger.info(
            "fix_mail_quota: mbox_quota=-1 aplicado para %d domínio(s)",
            len(domains),
        )

    def fix_ftp_renames(self) -> None:
        """Audita renames automáticos que Plesk fez em logins FTP cPanel-style
        (`user@domain` → `user_domain`). Grava CSV
        <log_dir>/ftp-renames.csv com mapeamento para cliente reconfigurar
        aplicações. Audit-only — rename já feito pelo Plesk."""
        self.logger.info("Fase: fix_ftp_renames")
        session_dir = self.sessions_dir / self.session_name
        if not session_dir.is_dir():
            self.logger.warning("fix_ftp_renames: session dir ausente — skip")
            return

        reports = sorted(session_dir.glob("accounts_report_tree.*"))
        reports = [r for r in reports if r.suffix not in (".bak", ".json")]
        if not reports:
            self.logger.info("fix_ftp_renames: nenhum accounts_report_tree.* — skip")
            return
        report_path = reports[-1]
        try:
            content = report_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.logger.warning("fix_ftp_renames: falha lendo %s: %s",
                                report_path, exc)
            return

        rename_re = re.compile(
            r"Login of FTP user '([^']+)' does not conform to Plesk rules\. "
            r"It was changed to '([^']+)'",
            re.IGNORECASE,
        )
        matches = rename_re.findall(content)
        if not matches:
            self.logger.info("fix_ftp_renames: 0 renames detectados em %s",
                             report_path.name)
            return

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            csv_path = self.log_dir / "ftp-renames.csv"
            existing_keys: set[tuple[str, str]] = set()
            if csv_path.exists():
                for ln in csv_path.read_text(encoding="utf-8").splitlines()[1:]:
                    parts = ln.split(",", 3)
                    if len(parts) >= 3:
                        existing_keys.add((parts[1], parts[2]))
            new_file = not csv_path.exists()
            written = 0
            with csv_path.open("a", encoding="utf-8") as fh:
                if new_file:
                    fh.write("timestamp,original_login,new_login,domain\n")
                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                for original, new in matches:
                    if (original, new) in existing_keys:
                        continue
                    domain_part = original.split("@", 1)[1] if "@" in original else ""
                    fh.write(f"{ts},{original},{new},{domain_part}\n")
                    written += 1
                    self.logger.warning(
                        "fix_ftp_renames: %s → %s (cliente precisa reconfigurar)",
                        original, new,
                    )
            self.logger.info(
                "fix_ftp_renames: %d rename(s) novos gravado(s); CSV em %s",
                written, csv_path,
            )
        except OSError as exc:
            self.logger.warning("fix_ftp_renames: falha gravando CSV: %s", exc)

    def fix_dns_conflicts(self, *, apply: bool = False) -> None:
        """Remove DNS records cPanel-only que conflitam com Plesk
        (cpcontacts/cpanel/whm/webdisk). Modo default audit (lista no log);
        com apply=True DELETE direto via SQL. Idempotente."""
        self.logger.info("Fase: fix_dns_conflicts (apply=%s)", apply)
        if not self.plesk_bin:
            self.logger.warning(
                "fix_dns_conflicts: binário 'plesk' não localizado — skip"
            )
            return
        domains = self._load_migrated_domains()
        if not domains:
            self.logger.warning("fix_dns_conflicts: 0 domínios — skip")
            return

        # Pattern POSIX REGEXP MariaDB: ^(cpcontacts|cpanel|whm|webdisk)\.
        prefixes = "|".join(CPANEL_ONLY_DNS_HOSTS)
        for dom in domains:
            select_sql = (
                f"SELECT id, host, type FROM dns_recs "
                f"WHERE domain_id=(SELECT id FROM domains "
                f"WHERE name='{self._sql_escape(dom)}') "
                f"AND host REGEXP '^({prefixes})\\\\.';"
            )
            try:
                stdout = self._run_plesk_db(select_sql, fetch=True)
            except PhaseExecutionError as exc:
                self.logger.warning(
                    "fix_dns_conflicts: falha consultando %s: %s", dom, exc
                )
                continue
            hits = [ln for ln in stdout.splitlines() if ln.strip()]
            if not hits:
                continue
            self.logger.warning(
                "fix_dns_conflicts: %s — %d record(s) cPanel-only:",
                dom, len(hits),
            )
            for h in hits[:10]:
                self.logger.warning("  %s", h)
            if not apply:
                continue
            delete_sql = (
                f"DELETE FROM dns_recs "
                f"WHERE domain_id=(SELECT id FROM domains "
                f"WHERE name='{self._sql_escape(dom)}') "
                f"AND host REGEXP '^({prefixes})\\\\.';"
            )
            self._run_plesk_db(
                delete_sql, log_to=self.log_dir / "fix-dns-conflicts.log"
            )
            if not self.dry_run:
                self.logger.info(
                    "fix_dns_conflicts: %s — %d record(s) removidos",
                    dom, len(hits),
                )

        if not apply:
            self.logger.warning(
                "fix_dns_conflicts: --apply-dns-cleanup não passado — "
                "nada removido (audit-only). Inspect ftp-renames/log."
            )

    def fix_owner(self, *, apply: bool = False) -> None:
        """Reatribui owner de subscriptions migradas que ficaram em Admin
        (cl_id=0) quando o bloco `Customer:` da successful-subscriptions
        indica dono cPanel. Audit default lista mismatches em
        <log_dir>/owner-mismatches.csv. Com apply=True cria customer
        ausente (login slugificado, senha urlsafe(16)) e reassigna via
        `plesk bin subscription --change-owner <dom> -owner <login>`. Senhas em
        <log_dir>/owner-fix.csv chmod 600 — distribua fora-de-banda."""
        self.logger.info("Fase: fix_owner (apply=%s)", apply)
        if not self.plesk_bin:
            self.logger.warning(
                "fix_owner: binário 'plesk' não localizado — skip"
            )
            return

        owners_map = self._load_migrated_owners()
        if not owners_map:
            self.logger.info(
                "fix_owner: nenhum bloco `Customer:` em "
                "successful-subscriptions — nada para reatribuir."
            )
            return

        domain_to_expected: dict[str, str] = {}
        for cust, doms in owners_map.items():
            for d in doms:
                domain_to_expected[d] = cust

        placeholders = ",".join(
            f"'{self._sql_escape(d)}'" for d in domain_to_expected
        )
        sql = (
            f"SELECT d.name, COALESCE(c.login, '__admin__') "
            f"FROM domains d "
            f"LEFT JOIN clients c ON d.cl_id=c.id "
            f"WHERE d.name IN ({placeholders});"
        )
        try:
            stdout = self._run_plesk_db(sql, fetch=True)
        except PhaseExecutionError as exc:
            self.logger.warning("fix_owner: query falhou — %s", exc)
            return

        current_owners: dict[str, str] = {}
        for ln in stdout.splitlines():
            parts = ln.strip().split("\t")
            if len(parts) >= 2:
                current_owners[parts[0].lower()] = parts[1].strip()

        mismatches: list[tuple[str, str, str]] = []
        for dom, expected_cust in domain_to_expected.items():
            cur = current_owners.get(dom, "__missing__")
            if cur in ("__admin__", "__missing__"):
                mismatches.append((dom, expected_cust, cur))

        if not mismatches:
            self.logger.info(
                "fix_owner: 0 mismatches — todos domínios já têm Customer."
            )
            return

        self.logger.warning(
            "fix_owner: %d domínio(s) sem Customer — esperado Customer "
            "do bloco successful-subscriptions.",
            len(mismatches),
        )
        for dom, cust, cur in mismatches[:10]:
            self.logger.warning(
                "  %s → esperado=%s (atual=%s)", dom, cust, cur
            )
        if len(mismatches) > 10:
            self.logger.warning("  ... e mais %d.", len(mismatches) - 10)

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            audit_csv = self.log_dir / "owner-mismatches.csv"
            new_audit = not audit_csv.exists()
            with audit_csv.open("a", encoding="utf-8") as fh:
                if new_audit:
                    fh.write("timestamp,domain,expected_customer,"
                             "proposed_login,current_owner\n")
                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                for dom, cust, cur in mismatches:
                    login = self._slugify_login(cust)
                    fh.write(
                        f"{ts},{dom},{self._csv_quote(cust)},"
                        f"{login},{cur}\n"
                    )
        except OSError as exc:
            self.logger.warning("fix_owner: falha gravando audit CSV: %s", exc)

        if not apply:
            self.logger.warning(
                "fix_owner: rode com --apply-owner-fix para criar "
                "customers ausentes e reassign owner."
            )
            return

        by_customer: dict[str, list[str]] = {}
        for dom, cust, _cur in mismatches:
            by_customer.setdefault(cust, []).append(dom)

        secrets_csv = self.log_dir / "owner-fix.csv"
        new_secrets = not secrets_csv.exists()
        try:
            secrets_csv.touch(mode=0o600, exist_ok=True)
            os.chmod(secrets_csv, 0o600)
        except OSError as exc:
            raise PhaseExecutionError(
                f"fix_owner: não consegui criar {secrets_csv}: {exc}"
            ) from exc

        created = 0
        reassigned = 0
        with secrets_csv.open("a", encoding="utf-8") as fh:
            if new_secrets:
                fh.write("timestamp,customer_name,login,password,email,"
                         "domains\n")
            for cust, doms in by_customer.items():
                login = self._slugify_login(cust)
                email = f"{login}@example.invalid"
                password = ""

                exists = False
                if not self.dry_run:
                    try:
                        check = subprocess.run(
                            [str(self.plesk_bin), "bin", "customer",
                             "--info", login],
                            capture_output=True, text=True,
                            timeout=60, check=False,
                        )
                        exists = check.returncode == 0
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        self.logger.error(
                            "fix_owner: falha verificando customer %s: %s",
                            login, exc,
                        )
                        continue

                if not exists:
                    while True:
                        password = secrets.token_urlsafe(16)
                        if password[0] not in "-_":
                            break
                    self.sensitive_values.append(password)
                    try:
                        self._run(
                            [str(self.plesk_bin), "bin", "customer",
                             "--create", login,
                             "-name", cust,
                             "-passwd", password,
                             "-email", email],
                            timeout=TIMEOUT_FIX_OWNER,
                            log_to=self.log_dir / "fix-owner.log",
                        )
                        created += 1
                    except (PhaseExecutionError,
                            subprocess.CalledProcessError) as exc:
                        self.logger.error(
                            "fix_owner: falha criando customer %s (%s): %s",
                            login, cust, exc,
                        )
                        continue

                ok_doms: list[str] = []
                for dom in doms:
                    try:
                        self._run(
                            [str(self.plesk_bin), "bin", "subscription",
                             "--change-owner", dom, "-owner", login],
                            timeout=120,
                            log_to=self.log_dir / "fix-owner.log",
                        )
                        ok_doms.append(dom)
                        reassigned += 1
                    except (PhaseExecutionError,
                            subprocess.CalledProcessError) as exc:
                        self.logger.error(
                            "fix_owner: falha reassign %s → %s: %s",
                            dom, login, exc,
                        )

                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                pwd_field = password if password else "(customer-pre-existente)"
                fh.write(
                    f"{ts},{self._csv_quote(cust)},{login},{pwd_field},"
                    f"{email},{';'.join(ok_doms)}\n"
                )

        try:
            os.chmod(secrets_csv, 0o600)
        except OSError:
            pass

        self.logger.warning(
            "fix_owner: %d customer(s) criado(s), %d subscription(s) "
            "reassign — CSV: %s (distribua senhas via canal seguro "
            "fora-de-banda).",
            created, reassigned, secrets_csv,
        )

    def copy_db_content(self) -> None:
        self.logger.info("Fase: copy_db_content")
        self._require_plesk_migrator_bin()
        self._require_runtime_state()
        self._run(
            [str(self.plesk_migrator_bin), "copy-db-content"],
            timeout=TIMEOUT_COPY_CONTENT,
            log_to=self.log_dir / "copy-db.log",
        )

    def test_all(self) -> None:
        self.logger.info("Fase: test_all")
        self._require_plesk_migrator_bin()
        self._require_runtime_state()
        self._run(
            [str(self.plesk_migrator_bin), "test-all"],
            timeout=TIMEOUT_TEST_ALL,
            log_to=self.log_dir / "test-all.log",
        )

    def cleanup_config_ini(self) -> None:
        self.logger.info("Fase: cleanup_config_ini")
        if not self.cleanup_config:
            self.logger.info("cleanup_config=False — pulando.")
            return
        config_path = self.conf_dir / "config.ini"
        if self.dry_run:
            self.logger.info("[DRY-RUN] apagaria %s", config_path)
            return
        try:
            config_path.unlink(missing_ok=True)
            self.logger.info(
                "config.ini removido — senha cPanel não persiste em disco."
            )
        except OSError as exc:
            self.logger.warning("Falha ao remover %s: %s", config_path, exc)

    # ------------------------------------------------------------------
    # Pipeline (§9 spec)
    # ------------------------------------------------------------------

    def _session_fingerprint_path(self) -> pathlib.Path:
        return self.sessions_dir / self.session_name / ".orchestrator-fingerprint"

    def _compute_config_fingerprint(self) -> str:
        payload = json.dumps(
            self.config, sort_keys=True, ensure_ascii=False, default=str
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _validate_resume_fingerprint(self) -> None:
        fp_file = self._session_fingerprint_path()
        current = self._compute_config_fingerprint()
        if not fp_file.exists():
            self.logger.warning(
                "Resume sem fingerprint prévio em %s. "
                "Sessão antiga (criada antes do suporte a --resume): "
                "prosseguindo SEM validação de integridade do YAML. "
                "Garante que o YAML não foi alterado desde a primeira execução.",
                fp_file,
            )
            if not self.dry_run:
                fp_file.parent.mkdir(parents=True, exist_ok=True)
                fp_file.write_text(current)
            return
        saved = fp_file.read_text().strip()
        if saved != current:
            raise PhaseExecutionError(
                f"Config YAML diverge do fingerprint da sessão ({fp_file}). "
                f"Resume bloqueado: re-executar pode corromper estado parcial. "
                f"Resolva: (a) restaure YAML para versão original, OU "
                f"(b) descarte sessão "
                f"(rm -rf {self.sessions_dir / self.session_name}) "
                f"e refaça do zero."
            )
        self.logger.info("Resume: fingerprint do YAML confere (%s)", fp_file)

    def _write_resume_fingerprint(self) -> None:
        fp_file = self._session_fingerprint_path()
        try:
            fp_file.parent.mkdir(parents=True, exist_ok=True)
            fp_file.write_text(self._compute_config_fingerprint())
        except OSError as exc:
            self.logger.debug(
                "Não foi possível escrever fingerprint em %s: %s", fp_file, exc
            )

    def run_all(
        self,
        *,
        skip_install: bool = False,
        only_phase: str | None = None,
        skip_web_content: bool = False,
        skip_mail_content: bool = False,
        skip_db_content: bool = False,
        skip_fix_docroot: bool = False,
        skip_fix_mailpath: bool = False,
        skip_check_mail_passwords: bool = False,
        reset_mail_passwords: bool = False,
        skip_sanitize_list: bool = False,
        rename_reserved_subdomains: bool = False,
        skip_fix_limits: bool = False,
        skip_retransfer_failed: bool = False,
        max_retransfer_attempts: int = MAX_RETRANSFER_ATTEMPTS,
        skip_fix_mail_quota: bool = False,
        skip_fix_ftp_renames: bool = False,
        skip_fix_dns_conflicts: bool = False,
        apply_dns_cleanup: bool = False,
        apply_mailpath_fix: bool = False,
        skip_fix_owner: bool = False,
        apply_owner_fix: bool = False,
        start_from: str | None = None,
    ) -> None:
        behavior = self.config.get("behavior") or {}
        behavior_skip = behavior.get("skip") or {}

        skip_install = skip_install or behavior.get("skip_install", False)
        skip_web_content = (
            skip_web_content or behavior_skip.get("web_content", False)
        )
        skip_mail_content = (
            skip_mail_content or behavior_skip.get("mail_content", False)
        )
        skip_db_content = (
            skip_db_content or behavior_skip.get("db_content", False)
        )
        skip_fix_docroot = (
            skip_fix_docroot or behavior_skip.get("fix_docroot", False)
        )
        skip_fix_mailpath = (
            skip_fix_mailpath or behavior_skip.get("fix_mailpath", False)
        )
        skip_check_mail_passwords = (
            skip_check_mail_passwords
            or behavior_skip.get("check_mail_passwords", False)
        )
        reset_mail_passwords = (
            reset_mail_passwords
            or (behavior.get("reset_mail_passwords", False))
        )
        skip_sanitize_list = (
            skip_sanitize_list or behavior_skip.get("sanitize_list", False)
        )
        rename_reserved_subdomains = (
            rename_reserved_subdomains
            or behavior.get("rename_reserved_subdomains", False)
        )
        skip_fix_limits = (
            skip_fix_limits or behavior_skip.get("fix_limits", False)
        )
        skip_retransfer_failed = (
            skip_retransfer_failed
            or behavior_skip.get("retransfer_failed", False)
        )
        skip_fix_mail_quota = (
            skip_fix_mail_quota or behavior_skip.get("fix_mail_quota", False)
        )
        skip_fix_ftp_renames = (
            skip_fix_ftp_renames or behavior_skip.get("fix_ftp_renames", False)
        )
        skip_fix_dns_conflicts = (
            skip_fix_dns_conflicts
            or behavior_skip.get("fix_dns_conflicts", False)
        )
        apply_dns_cleanup = (
            apply_dns_cleanup or behavior.get("apply_dns_cleanup", False)
        )
        apply_mailpath_fix = (
            apply_mailpath_fix or behavior.get("apply_mailpath_fix", False)
        )
        skip_fix_owner = (
            skip_fix_owner or behavior_skip.get("fix_owner", False)
        )
        apply_owner_fix = (
            apply_owner_fix or behavior.get("apply_owner_fix", False)
        )
        start_from = start_from or self.start_from or behavior.get("start_from")

        if self.resume:
            self._validate_resume_fingerprint()
        elif not self.dry_run:
            self._write_resume_fingerprint()

        migration_cfg = self.config.get("migration") or {}
        has_filter = bool(
            migration_cfg.get("allowlist") or migration_cfg.get("denylist")
        )

        # Ordem: sanity-check primeiro (aborta cedo se não-Plesk ou não-root);
        # preflight roda DEPOIS de list/filter porque `plesk-migrator check`
        # valida a migration-list atual e deve ser re-executado sempre que
        # ela mudar (docs Plesk CLI guide).
        phases: list[tuple[str, Callable[[], None], bool]] = [
            ("sanity-check", self.sanity_check, True),
            ("install", self.ensure_plesk_migrator_installed, not skip_install),
            ("config", self.generate_config_ini, True),
            ("list", self.generate_migration_list, True),
            ("sanitize-list",
             lambda: self.sanitize_list(
                 apply_renames=rename_reserved_subdomains,
             ),
             not skip_sanitize_list),
            ("filter",
             lambda: self.filter_migration_list(
                 migration_cfg.get("allowlist"),
                 migration_cfg.get("denylist"),
             ),
             has_filter),
            ("preflight", self.preflight_checks, True),
            ("transfer",
             lambda: self.transfer_accounts(
                 skip_web=skip_web_content,
                 skip_mail=skip_mail_content,
                 skip_db=skip_db_content,
                 start_from=start_from,
             ),
             True),
            ("fix-limits", self.fix_limits, not skip_fix_limits),
            ("retransfer-failed",
             lambda: self.retransfer_failed(
                 max_attempts=max_retransfer_attempts,
             ),
             not skip_retransfer_failed),
            ("fix-owner",
             lambda: self.fix_owner(apply=apply_owner_fix),
             not skip_fix_owner),
            ("copy-web", self.copy_web_content, not skip_web_content),
            ("copy-mail", self.copy_mail_content, not skip_mail_content),
            ("fix-mailpath",
             lambda: self.fix_mailpath(apply=apply_mailpath_fix),
             not skip_fix_mailpath),
            ("check-mail-passwords",
             lambda: self.check_mail_passwords(reset=reset_mail_passwords),
             not skip_check_mail_passwords),
            ("fix-mail-quota", self.fix_mail_quota, not skip_fix_mail_quota),
            ("fix-ftp-renames", self.fix_ftp_renames, not skip_fix_ftp_renames),
            ("fix-dns-conflicts",
             lambda: self.fix_dns_conflicts(apply=apply_dns_cleanup),
             not skip_fix_dns_conflicts),
            ("copy-db", self.copy_db_content, not skip_db_content),
            ("fix-docroot", self.fix_docroot, not skip_fix_docroot),
            ("test", self.test_all, True),
            ("cleanup-config", self.cleanup_config_ini, self.cleanup_config),
        ]

        self._acquire_lock()
        self._install_signal_handlers()
        try:
            if only_phase and only_phase != "all":
                matched = False
                for name, fn, _enabled in phases:
                    if name == only_phase:
                        self.logger.info("=== Executando fase única: %s ===", name)
                        fn()
                        matched = True
                        break
                if not matched:
                    raise ValidationError(
                        f"Fase desconhecida: {only_phase}. "
                        f"Opções: {PHASES_ORDER + ['all']}"
                    )
            else:
                for name, fn, enabled in phases:
                    if not enabled:
                        self.logger.info("--- Pulando fase: %s ---", name)
                        continue
                    self.logger.info("=== Fase: %s ===", name)
                    fn()
            self.logger.info("Pipeline concluído com sucesso.")
        finally:
            self._release_lock()


# ---------------------------------------------------------------------------
# CLI (§10 spec)
# ---------------------------------------------------------------------------

def _wizard_confirm(prompt: str, *, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"{prompt} {suffix}: ").strip().lower()
    except EOFError:
        return default
    except KeyboardInterrupt:
        raise SystemExit(130)
    if not raw:
        return default
    return raw in ("y", "yes", "s", "sim")


def _wizard_prompt(
    label: str,
    *,
    default: str | None = None,
    secret: bool = False,
    validator: Callable[[str], tuple[bool, str]] | None = None,
    allow_empty: bool = False,
) -> str:
    while True:
        if default is not None and not secret:
            shown = f"{label} [{default}]: "
        else:
            shown = f"{label}: "
        try:
            raw = getpass.getpass(shown) if secret else input(shown)
        except EOFError:
            sys.stderr.write("\nAbortado (EOF).\n")
            raise SystemExit(130)
        except KeyboardInterrupt:
            sys.stderr.write("\nAbortado (Ctrl-C).\n")
            raise SystemExit(130)
        if not secret:
            raw = raw.strip()
        if not raw and default is not None:
            raw = default
        if not raw:
            if allow_empty:
                return ""
            print("  Valor vazio não aceito; tente de novo.")
            continue
        if validator is not None:
            ok, msg = validator(raw)
            if not ok:
                print(f"  {msg}")
                continue
        return raw


def _wizard_prompt_secret_confirmed(label: str, *, hint: str = "") -> str:
    if hint:
        print(f"  {hint}")
    while True:
        try:
            pwd1 = getpass.getpass(f"{label}: ")
            pwd2 = getpass.getpass(f"{label} (confirme): ")
        except EOFError:
            sys.stderr.write("\nAbortado (EOF).\n")
            raise SystemExit(130)
        except KeyboardInterrupt:
            sys.stderr.write("\nAbortado (Ctrl-C).\n")
            raise SystemExit(130)
        if not pwd1:
            print("  Senha vazia não aceita; tente de novo.")
            continue
        if pwd1 != pwd2:
            print("  Senhas não conferem; tente de novo.")
            continue
        return pwd1


def _wizard_validate_hostname(value: str) -> tuple[bool, str]:
    if len(value) > 253:
        return False, "Hostname acima de 253 caracteres"
    if not re.match(r"^[A-Za-z0-9._:\-]+$", value):
        return False, "Hostname só pode conter letras, dígitos, '.', '-', '_', ':'"
    return True, ""


def _wizard_validate_port(value: str) -> tuple[bool, str]:
    try:
        port = int(value)
    except ValueError:
        return False, "Porta deve ser inteiro"
    if not (1 <= port <= 65535):
        return False, "Porta fora do range 1..65535"
    return True, ""


def _wizard_validate_log_dir(value: str) -> tuple[bool, str]:
    p = pathlib.Path(value)
    if p.exists():
        if not p.is_dir():
            return False, f"{value} existe e não é diretório"
        if not os.access(value, os.W_OK):
            return False, f"{value} não é gravável"
        return True, ""
    parent = p.parent
    if not parent.exists():
        return False, f"Diretório pai {parent} não existe"
    if not os.access(str(parent), os.W_OK):
        return False, f"Diretório pai {parent} não é gravável"
    return True, ""


def _run_init_wizard(args: argparse.Namespace) -> int:
    """Wizard interativo: coleta campos mínimos, gera YAML chmod 600.

    Retorna 0 (sucesso), 1 (erro), 130 (cancelado).
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.stderr.write(
            "ERRO: --init requer terminal interativo (stdin/stdout TTY).\n"
        )
        return 1

    out_path = pathlib.Path(
        args.config_out or args.config or "/etc/plesk-migration.yaml"
    )

    print("=" * 64)
    print("  Plesk Migrator Orchestrator — wizard de configuração")
    print("=" * 64)
    print(f"Arquivo de saída: {out_path}")
    if os.geteuid() != 0 and str(out_path).startswith("/etc/"):
        print("AVISO: você não é root; escrita em /etc/ pode falhar.")
    print()

    backup_path: pathlib.Path | None = None
    if out_path.exists():
        if not _wizard_confirm(
            f"{out_path} já existe — sobrescrever?", default=False
        ):
            print("Cancelado pelo usuário.")
            return 0
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = pathlib.Path(f"{out_path}.bak.{ts}")
        print(f"Backup automático → {backup_path}")
        print()

    print("[1/3] Origem cPanel")
    src_host = _wizard_prompt(
        "  IP/hostname do servidor cPanel",
        validator=_wizard_validate_hostname,
    )
    src_port = _wizard_prompt(
        "  Porta SSH", default="22", validator=_wizard_validate_port,
    )
    print()
    src_password = _wizard_prompt_secret_confirmed(
        "  Senha root SSH do cPanel",
        hint=(
            "Cole ou digite a senha — caracteres NÃO aparecem na tela. "
            "Aceita qualquer caracter (espaços, !@#$%, UTF-8)."
        ),
    )
    src_pg = _wizard_prompt(
        "  Senha PostgreSQL origem (Enter para pular)",
        default="", secret=True, allow_empty=True,
    )

    print("\n[2/3] Destino Plesk")
    dst_host = _wizard_prompt(
        "  IP/hostname do destino", default="127.0.0.1",
        validator=_wizard_validate_hostname,
    )

    print("\n[3/3] Paths (Enter aceita default)")
    log_dir = _wizard_prompt(
        "  Diretório de log",
        default="/var/log/plesk-migration-orchestrator",
        validator=_wizard_validate_log_dir,
    )

    data: dict = {
        "source": {
            "host": src_host,
            "ssh_port": int(src_port),
            "ssh_password": src_password,
        },
        "dest": {"host": dst_host},
        "paths": {"log_dir": log_dir},
    }
    if src_pg:
        data["source"]["postgres_password"] = src_pg

    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        f"# Plesk Migrator Orchestrator — gerado por --init em {now}\n"
        f"# chmod 600 (contém senha em texto plano)\n"
        f"# Campos avançados (migration.*, behavior.*, paths.plesk_bin):\n"
        f"# ver config.example.yaml na raiz do repositório.\n"
        f"#\n"
        f"# Próximo passo (dry-run, não-destrutivo):\n"
        f"#   sudo ./run.sh --config {out_path} --dry-run --skip-install\n"
        f"\n"
    )
    body = yaml.safe_dump(
        data, default_flow_style=False, sort_keys=False, allow_unicode=True,
    )

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"ERRO: diretório pai inacessível: {exc}\n")
        return 1

    # Grava atômico para evitar TOCTOU em /tmp e similares: cria arquivo
    # novo via mkstemp (mode 600 garantido por umask + fchmod ANTES do
    # write), depois rename. Symlink racing em out_path não atinge a senha.
    payload = (header + body).encode("utf-8")
    old_umask = os.umask(0o077)
    tmp_fd = -1
    tmp_name = ""
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=out_path.name + ".",
            suffix=".tmp",
            dir=str(out_path.parent),
        )
        os.fchmod(tmp_fd, 0o600)
        os.write(tmp_fd, payload)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = -1
        if backup_path is not None:
            try:
                os.replace(str(out_path), str(backup_path))
            except OSError as exc:
                sys.stderr.write(f"ERRO: backup falhou: {exc}\n")
                try: os.unlink(tmp_name)
                except OSError: pass
                return 1
        os.replace(tmp_name, str(out_path))
        tmp_name = ""
    except OSError as exc:
        sys.stderr.write(f"ERRO: gravação falhou: {exc}\n")
        if tmp_fd >= 0:
            try: os.close(tmp_fd)
            except OSError: pass
        if tmp_name:
            try: os.unlink(tmp_name)
            except OSError: pass
        return 1
    finally:
        os.umask(old_umask)

    print(f"\n✓ Gravado: {out_path} (chmod 600)")

    try:
        cfg = _load_config(str(out_path))
    except ValidationError as exc:
        sys.stderr.write(f"ERRO: YAML gerado falhou no parse: {exc}\n")
        return 1
    # Reusa _validate_config (não-instanciado): stub minimal só com .config.
    # Evita side-effects do __init__ (logger, log dir, discovery).
    class _StubForValidation:
        pass
    stub = _StubForValidation()
    stub.config = cfg
    try:
        PleskMigrationOrchestrator._validate_config(stub)
    except ValidationError as exc:
        sys.stderr.write(
            f"ERRO: validação completa falhou: {exc}\n"
            f"O YAML foi gravado em {out_path} mas precisa ser corrigido "
            f"antes de rodar a migração.\n"
        )
        return 1

    print()
    print("Próximos passos:")
    print(f"  sudo ./run.sh --config {out_path} --dry-run --skip-install")
    print(f"  sudo ./run.sh --config {out_path}   # migração real")
    print()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plesk_migrator_orchestrator",
        description=(
            "Orquestra migração cPanel → Plesk Obsidian via panel-migrator CLI."
        ),
    )
    parser.add_argument(
        "--config", required=False, default=None,
        help="Caminho do YAML de configuração (ver config.example.yaml). "
             "Não exigido em modo --init.",
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Wizard interativo: pergunta valores e gera YAML chmod 600. "
             "Saída em --config-out (default /etc/plesk-migration.yaml).",
    )
    parser.add_argument(
        "--config-out", default=None,
        help="Caminho de saída do YAML gerado pelo --init "
             "(default /etc/plesk-migration.yaml).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Loga comandos sem executá-los (apenas leitura roda)")
    parser.add_argument("--skip-install", action="store_true",
                        help="Pula auto-install da extensão panel-migrator")
    parser.add_argument("--force-regenerate", action="store_true",
                        help="Sobrescreve migration-list existente")
    parser.add_argument("--cleanup-config", action="store_true",
                        help="Apaga config.ini ao final (remove senha do disco)")
    parser.add_argument(
        "--only-phase",
        choices=PHASES_ORDER + ["all"],
        default="all",
        help="Executa apenas a fase indicada (default: all)",
    )
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG em stdout (arquivo já é DEBUG)")
    parser.add_argument("--skip-web-content", action="store_true",
                        help="Pula copy-web-content + flag em transfer-accounts")
    parser.add_argument("--skip-mail-content", action="store_true",
                        help="Pula copy-mail-content + flag em transfer-accounts")
    parser.add_argument("--skip-db-content", action="store_true",
                        help="Pula copy-db-content + flag em transfer-accounts")
    parser.add_argument("--skip-fix-docroot", action="store_true",
                        help="Pula ajuste de www-root pós copy-web (fase fix-docroot)")
    parser.add_argument("--skip-fix-mailpath", action="store_true",
                        help="Pula auditoria de path de Maildir pós copy-mail "
                             "(fase fix-mailpath)")
    parser.add_argument("--skip-check-mail-passwords", action="store_true",
                        help="Pula auditoria de senhas vazias em psa.accounts "
                             "(fase check-mail-passwords)")
    parser.add_argument("--reset-mail-passwords", action="store_true",
                        help="Em check-mail-passwords, gera senha nova para "
                             "cada conta sem senha e grava CSV chmod 600 em "
                             "<log_dir>/mail-password-reset.csv. Distribuir "
                             "via canal seguro fora-de-banda.")
    parser.add_argument("--skip-sanitize-list", action="store_true",
                        help="Pula auditoria de subdomains reservados Plesk "
                             "(fase sanitize-list)")
    parser.add_argument("--rename-reserved-subdomains", action="store_true",
                        help="Em sanitize-list, reescreve migration-list "
                             "renomeando webmail.<dom>/mail.<dom>/etc para "
                             "alternativas (correio/email/...). Backup .bak.")
    parser.add_argument("--skip-fix-limits", action="store_true",
                        help="Pula UPDATE Limits.* (fase fix-limits)")
    parser.add_argument("--skip-retransfer-failed", action="store_true",
                        help="Pula auto-retry de failed-subscriptions "
                             "(fase retransfer-failed)")
    parser.add_argument("--max-retransfer-attempts", type=int, default=None,
                        help=f"Máximo de tentativas em retransfer-failed "
                             f"(default: {MAX_RETRANSFER_ATTEMPTS})")
    parser.add_argument("--skip-fix-mail-quota", action="store_true",
                        help="Pula UPDATE mail.mbox_quota (fase fix-mail-quota)")
    parser.add_argument("--skip-fix-ftp-renames", action="store_true",
                        help="Pula auditoria de FTP renames (fase fix-ftp-renames)")
    parser.add_argument("--skip-fix-dns-conflicts", action="store_true",
                        help="Pula detecção de DNS cPanel-only "
                             "(fase fix-dns-conflicts)")
    parser.add_argument("--apply-dns-cleanup", action="store_true",
                        help="Em fix-dns-conflicts, DELETE registros DNS "
                             "cPanel-only (cpcontacts/cpanel/whm/webdisk).")
    parser.add_argument("--apply-mailpath-fix", action="store_true",
                        help="Em fix-mailpath, rsync Maildirs em path "
                             "alternativo para canonical Plesk + "
                             "`plesk repair mail`.")
    parser.add_argument("--skip-fix-owner", action="store_true",
                        help="Pula reatribuição de owner pós-transfer "
                             "(fase fix-owner)")
    parser.add_argument("--apply-owner-fix", action="store_true",
                        help="Em fix-owner, cria customer ausente "
                             "(login slugificado, senha urlsafe(16) em "
                             "<log_dir>/owner-fix.csv chmod 600) e "
                             "reassigna subscription via "
                             "`plesk bin subscription --change-owner <dom> -owner <login>`. "
                             "Distribua senhas via canal seguro fora-de-banda.")
    parser.add_argument(
        "--resume", action="store_true",
        help="Retoma sessão existente: pula generate-migration-list se já "
             "existir (backup automático), valida fingerprint do YAML. "
             "Conflita com --force-regenerate.",
    )
    parser.add_argument(
        "--start-from", metavar="STEP", default=None,
        help="Passa --start-from <STEP> ao 'plesk-migrator transfer-accounts'. "
             "Steps variam por versão; consulte "
             "'plesk-migrator transfer-accounts --help'. "
             "Comuns: copy-database, copy-web-content, copy-mail-content, "
             "deploy-database, deploy-domain, restore-hosting.",
    )
    return parser


def _load_config(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        raise ValidationError(f"Config não encontrado: {path}")
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValidationError(f"YAML inválido em {path}: {exc}")
    if data is None:
        raise ValidationError(f"Config vazio: {path}")
    if not isinstance(data, dict):
        raise ValidationError(f"Config raiz precisa ser mapping em {path}")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.init:
        return _run_init_wizard(args)

    if not args.config:
        sys.stderr.write(
            "ERRO: --config é obrigatório (ou use --init para wizard "
            "interativo).\n"
        )
        return 1

    try:
        config = _load_config(args.config)
    except ValidationError as exc:
        sys.stderr.write(f"ERRO de configuração: {exc}\n")
        return 1

    # CLI sobrescreve behavior.* do YAML quando flag é passada
    behavior = config.setdefault("behavior", {})
    dry_run = args.dry_run or behavior.get("dry_run", False)
    force_regenerate = (
        args.force_regenerate or behavior.get("force_regenerate", False)
    )
    cleanup_config = (
        args.cleanup_config or behavior.get("cleanup_config", False)
    )
    resume = args.resume or behavior.get("resume", False)
    start_from = args.start_from or behavior.get("start_from")

    if resume and force_regenerate:
        sys.stderr.write(
            "ERRO: --resume e --force-regenerate são mutuamente exclusivos\n"
        )
        return 1
    if start_from and args.only_phase not in (None, "all", "transfer"):
        sys.stderr.write(
            "ERRO: --start-from requer --only-phase transfer (ou all/default)\n"
        )
        return 1

    try:
        orchestrator = PleskMigrationOrchestrator(
            config,
            dry_run=dry_run,
            force_regenerate=force_regenerate,
            cleanup_config=cleanup_config,
            verbose=args.verbose,
            resume=resume,
            start_from=start_from,
        )
    except ValidationError as exc:
        sys.stderr.write(f"ERRO de configuração: {exc}\n")
        return 1

    try:
        orchestrator.run_all(
            skip_install=args.skip_install,
            only_phase=args.only_phase,
            skip_web_content=args.skip_web_content,
            skip_mail_content=args.skip_mail_content,
            skip_db_content=args.skip_db_content,
            skip_fix_docroot=args.skip_fix_docroot,
            skip_fix_mailpath=args.skip_fix_mailpath,
            skip_check_mail_passwords=args.skip_check_mail_passwords,
            reset_mail_passwords=args.reset_mail_passwords,
            skip_sanitize_list=args.skip_sanitize_list,
            rename_reserved_subdomains=args.rename_reserved_subdomains,
            skip_fix_limits=args.skip_fix_limits,
            skip_retransfer_failed=args.skip_retransfer_failed,
            max_retransfer_attempts=(
                args.max_retransfer_attempts
                if args.max_retransfer_attempts is not None
                else (config.get("migration") or {}).get(
                    "max_retransfer_attempts", MAX_RETRANSFER_ATTEMPTS
                )
            ),
            skip_fix_mail_quota=args.skip_fix_mail_quota,
            skip_fix_ftp_renames=args.skip_fix_ftp_renames,
            skip_fix_dns_conflicts=args.skip_fix_dns_conflicts,
            apply_dns_cleanup=args.apply_dns_cleanup,
            apply_mailpath_fix=args.apply_mailpath_fix,
            skip_fix_owner=args.skip_fix_owner,
            apply_owner_fix=args.apply_owner_fix,
            start_from=start_from,
        )
    except LockError as exc:
        sys.stderr.write(f"ERRO de lock: {exc}\n")
        return 4
    except PreflightError as exc:
        orchestrator.logger.error("Preflight falhou: %s", exc)
        return 2
    except (PhaseExecutionError, subprocess.CalledProcessError) as exc:
        orchestrator.logger.error("Fase falhou: %s", exc)
        return 3
    except ValidationError as exc:
        sys.stderr.write(f"ERRO de configuração: {exc}\n")
        return 1
    except KeyboardInterrupt:
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
