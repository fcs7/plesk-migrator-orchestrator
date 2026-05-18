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
import fcntl
import logging
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import threading
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
    "install",
    "config",
    "list",
    "filter",
    "preflight",
    "transfer",
    "copy-web",
    "copy-mail",
    "copy-db",
    "test",
    "cleanup",
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
    ) -> None:
        self.config = config or {}
        self.dry_run = dry_run
        self.force_regenerate = force_regenerate
        self.cleanup_config = cleanup_config
        self.verbose = verbose

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
                    "manualmente ou use plesk-migrator --migration-list-file."
                )
        cfg["migration"] = {"allowlist": [], "denylist": []}

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
                    "chave do YAML (auto-discovery resolve o caminho real)."
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
        for key in ("web_content", "mail_content", "db_content"):
            if key in skip and not isinstance(skip[key], bool):
                raise ValidationError(f"behavior.skip.{key} deve ser bool")
        for key in ("dry_run", "skip_install", "force_regenerate",
                    "cleanup_config"):
            if key in behavior and not isinstance(behavior[key], bool):
                raise ValidationError(f"behavior.{key} deve ser bool")

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
        """Resolve um binário: override YAML > candidatos fixos > $PATH."""
        if override:
            p = pathlib.Path(override)
            if p.exists():
                return p
            self.logger.warning(
                "paths.%s configurado para %s mas o arquivo não existe",
                label, override,
            )
            return p  # Devolve mesmo assim; falha clara depois ao executar.
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
                    timeout_fired.set()
                    self.logger.error(
                        "Timeout (%ss) excedido. Encerrando subprocess…",
                        timeout,
                    )
                    try:
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
            self.logger.debug("Lock liberado")

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._cleanup_subprocess)
        signal.signal(signal.SIGTERM, self._cleanup_subprocess)

    def _cleanup_subprocess(self, signum=None, frame=None) -> None:
        self.logger.warning(
            "Sinal %s recebido. Encerrando subprocess…", signum
        )
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

    def generate_config_ini(self) -> pathlib.Path:
        self.logger.info("Fase: generate_config_ini")
        src = self.config["source"]
        dst = self.config["dest"]

        cfg = configparser.ConfigParser()
        cfg["GLOBAL"] = {
            "source-type": "cpanel",
            "source-servers": "cpanel",
            "target-type": "plesk",
        }
        cfg["plesk"] = {
            "ip": dst["host"],
            "os": "unix",
        }
        cpanel_section = {
            "ip": src["host"],
            "os": "unix",
            "ssh-password": src["ssh_password"],
        }
        if int(src.get("ssh_port", 22)) != 22:
            cpanel_section["ssh-port"] = str(src["ssh_port"])
        if src.get("postgres_password"):
            cpanel_section["postgres-password"] = src["postgres_password"]
        cfg["cpanel"] = cpanel_section

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

        if migration_list.exists() and not self.force_regenerate:
            raise PhaseExecutionError(
                f"migration-list já existe em {migration_list}. "
                "Use --force-regenerate para sobrescrever."
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

        line_count = sum(
            1 for _ in migration_list.open("r", encoding="utf-8", errors="replace")
        )
        self.logger.info(
            "migration-list gerada (%d linhas) em %s",
            line_count, migration_list,
        )
        return migration_list

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
    ) -> None:
        self.logger.info("Fase: transfer_accounts")
        self._require_plesk_migrator_bin()
        cmd = [str(self.plesk_migrator_bin), "transfer-accounts"]
        if skip_web:
            cmd.append("--skip-copy-web-content")
        if skip_mail:
            cmd.append("--skip-copy-mail-content")
        if skip_db:
            cmd.append("--skip-copy-db-content")
        # EXTEND: --migration-list-file <path>
        # EXTEND: --skip-services-checks
        # EXTEND: --start-from <step>
        self._run(
            cmd,
            timeout=TIMEOUT_TRANSFER,
            log_to=self.log_dir / "transfer-accounts.log",
        )

    def copy_web_content(self) -> None:
        self.logger.info("Fase: copy_web_content")
        self._require_plesk_migrator_bin()
        self._run(
            [str(self.plesk_migrator_bin), "copy-web-content"],
            timeout=TIMEOUT_COPY_CONTENT,
            log_to=self.log_dir / "copy-web.log",
        )

    def copy_mail_content(self) -> None:
        self.logger.info("Fase: copy_mail_content")
        self._require_plesk_migrator_bin()
        self._run(
            [str(self.plesk_migrator_bin), "copy-mail-content"],
            timeout=TIMEOUT_COPY_CONTENT,
            log_to=self.log_dir / "copy-mail.log",
        )

    def copy_db_content(self) -> None:
        self.logger.info("Fase: copy_db_content")
        self._require_plesk_migrator_bin()
        self._run(
            [str(self.plesk_migrator_bin), "copy-db-content"],
            timeout=TIMEOUT_COPY_CONTENT,
            log_to=self.log_dir / "copy-db.log",
        )

    def test_all(self) -> None:
        self.logger.info("Fase: test_all")
        self._require_plesk_migrator_bin()
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

    def run_all(
        self,
        *,
        skip_install: bool = False,
        only_phase: str | None = None,
        skip_web_content: bool = False,
        skip_mail_content: bool = False,
        skip_db_content: bool = False,
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

        migration_cfg = self.config.get("migration") or {}
        has_filter = bool(
            migration_cfg.get("allowlist") or migration_cfg.get("denylist")
        )

        # Ordem: preflight roda DEPOIS de list/filter porque
        # `plesk-migrator check` valida a migration-list atual e deve ser
        # re-executado sempre que ela mudar (docs Plesk CLI guide).
        phases: list[tuple[str, callable, bool]] = [
            ("install", self.ensure_plesk_migrator_installed, not skip_install),
            ("config", self.generate_config_ini, True),
            ("list", self.generate_migration_list, True),
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
             ),
             True),
            ("copy-web", self.copy_web_content, not skip_web_content),
            ("copy-mail", self.copy_mail_content, not skip_mail_content),
            ("copy-db", self.copy_db_content, not skip_db_content),
            ("test", self.test_all, True),
            ("cleanup", self.cleanup_config_ini, self.cleanup_config),
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

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plesk_migrator_orchestrator",
        description=(
            "Orquestra migração cPanel → Plesk Obsidian via panel-migrator CLI."
        ),
    )
    parser.add_argument(
        "--config", required=True,
        help="Caminho do YAML de configuração (ver config.example.yaml)",
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

    try:
        orchestrator = PleskMigrationOrchestrator(
            config,
            dry_run=dry_run,
            force_regenerate=force_regenerate,
            cleanup_config=cleanup_config,
            verbose=args.verbose,
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
