"""Cobre nova fase `fix_htaccess` (audit-only por default, `--apply-htaccess-fix`
escreve transformações com backup).

Padrões reais confirmados em produção (Apache log mostra "Invalid command
'suPHP_ConfigPath'"):

  * `suPHP_*` (ConfigPath/Engine/etc) — módulo cPanel-only não carregado em
    Plesk → Apache 500. Comentar com prefixo `#FIX# ` na linha.
  * `AuthUserFile "/home/<user>/..."` — path absoluto cPanel inexistente
    em Plesk. Reescrever para `/var/www/vhosts/<parent>/.htpasswds/...`
    OU comentar (se arquivo destino não existir — operador resolve depois).
  * `php_value` / `php_admin_value` — Plesk PHP-FPM ignora; pode causar
    erro em PHP 8.x.
  * `cgi-sys`, `cpanelphp`, `FCGIWrapper`, `Action .* php-cgi`,
    `AddHandler .*(cpanel|cgi-script)` — handlers cPanel-only.

Idempotência: linhas já com prefixo `#FIX# ` não são re-processadas.
Backup: `.htaccess.pre-htaccess-fix-<ts>.bak`.
"""

from __future__ import annotations

import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from plesk_migrator_orchestrator import (
    PhaseExecutionError,
    PleskMigrationOrchestrator,
)


def _make_orch(td: pathlib.Path) -> PleskMigrationOrchestrator:
    orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
    orch.dry_run = False
    orch.logger = mock.MagicMock()
    orch.config = {
        "source": {"host": "cpanel.example.com", "ssh_port": 22,
                   "ssh_password": "secret"},
        "dest": {"host": "plesk.example.com"},
    }
    orch.sensitive_values = []
    orch.plesk_bin = pathlib.Path("/usr/sbin/plesk")
    orch.plesk_migrator_bin = pathlib.Path("/usr/local/psa/admin/sbin/plesk-migrator")
    orch.log_dir = td / "logs"
    orch.log_dir.mkdir(parents=True, exist_ok=True)
    orch.sessions_dir = td / "sessions"
    orch.session_name = "migration-session"
    (orch.sessions_dir / orch.session_name).mkdir(parents=True, exist_ok=True)
    return orch


class FixHtaccessAuditTest(unittest.TestCase):
    """Audit mode: enumera .htaccess + detecta padrões + grava CSV."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = pathlib.Path(self._td.name)
        self.orch = _make_orch(self.tmp)
        # Stub vhost root to inside tmp dir.
        self.vhosts_root = self.tmp / "vhosts"
        self.vhosts_root.mkdir()

    def _write_htaccess(self, rel_path: str, body: str) -> pathlib.Path:
        path = self.vhosts_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def _patch_vhosts(self):
        return patch.object(
            self.orch, "_vhosts_root_for_htaccess",
            return_value=self.vhosts_root,
        )

    def test_audit_detects_suphp_directive(self) -> None:
        self._write_htaccess(
            "opiniao.inf.br/public_html/.htaccess",
            "RewriteOptions inherit\n"
            "suPHP_ConfigPath /home/opiniaoi/public_html/\n"
            "RewriteEngine on\n",
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["opiniao.inf.br"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=False)

        csv = self.orch.log_dir / "htaccess-audit.csv"
        self.assertTrue(csv.exists())
        body = csv.read_text(encoding="utf-8")
        self.assertIn("suPHP_ConfigPath", body)
        self.assertIn("opiniao.inf.br", body)

    def test_audit_detects_authuserfile_home_path(self) -> None:
        self._write_htaccess(
            "opiniao.inf.br/datahq.opiniao.inf.br/.htaccess",
            'AuthType Basic\n'
            'AuthName "Protected"\n'
            'AuthUserFile "/home/opiniaoi/.htpasswds/datahq.opiniao.inf.br/passwd"\n'
            'Require valid-user\n',
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["opiniao.inf.br"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[
                              ("opiniao.inf.br", "datahq.opiniao.inf.br")
                          ]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=False)

        csv = self.orch.log_dir / "htaccess-audit.csv"
        body = csv.read_text(encoding="utf-8")
        self.assertIn("AuthUserFile", body)
        self.assertIn("/home/opiniaoi", body)

    def test_audit_skips_clean_htaccess(self) -> None:
        self._write_htaccess(
            "clean.example/public_html/.htaccess",
            "RewriteEngine on\n"
            "RewriteRule ^old$ /new [R=301,L]\n",
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["clean.example"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=False)
        csv = self.orch.log_dir / "htaccess-audit.csv"
        if csv.exists():
            body = csv.read_text(encoding="utf-8")
            # only header line if any
            self.assertNotIn("clean.example", body)

    def test_audit_does_not_modify_files(self) -> None:
        path = self._write_htaccess(
            "opiniao.inf.br/public_html/.htaccess",
            "suPHP_ConfigPath /home/opiniaoi/public_html/\n",
        )
        original = path.read_text(encoding="utf-8")
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["opiniao.inf.br"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=False)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        # No backup file created in audit mode.
        backups = list(path.parent.glob(".htaccess.pre-htaccess-fix-*.bak"))
        self.assertEqual(backups, [])


class FixHtaccessApplyTest(unittest.TestCase):
    """Apply mode: reescreve com backup + comenta linhas suspeitas."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = pathlib.Path(self._td.name)
        self.orch = _make_orch(self.tmp)
        self.vhosts_root = self.tmp / "vhosts"
        self.vhosts_root.mkdir()

    def _write_htaccess(self, rel_path: str, body: str) -> pathlib.Path:
        path = self.vhosts_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def _patch_vhosts(self):
        return patch.object(
            self.orch, "_vhosts_root_for_htaccess",
            return_value=self.vhosts_root,
        )

    def test_apply_comments_suphp_line_and_creates_backup(self) -> None:
        path = self._write_htaccess(
            "opiniao.inf.br/public_html/.htaccess",
            "RewriteOptions inherit\n"
            "suPHP_ConfigPath /home/opiniaoi/public_html/\n"
            "RewriteEngine on\n",
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["opiniao.inf.br"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=True)

        fixed = path.read_text(encoding="utf-8")
        # suPHP line must be commented out with `#FIX# `
        self.assertIn("#FIX# suPHP_ConfigPath", fixed)
        # Original RewriteEngine still in place
        self.assertIn("RewriteEngine on", fixed)
        # Backup created
        backups = list(path.parent.glob(".htaccess.pre-htaccess-fix-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertIn("suPHP_ConfigPath /home/", backups[0].read_text(encoding="utf-8"))

    def test_apply_comments_authuserfile_with_home_path(self) -> None:
        path = self._write_htaccess(
            "opiniao.inf.br/datahq.opiniao.inf.br/.htaccess",
            'AuthType Basic\n'
            'AuthName "Protected"\n'
            'AuthUserFile "/home/opiniaoi/.htpasswds/datahq.opiniao.inf.br/passwd"\n'
            'Require valid-user\n',
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["opiniao.inf.br"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[
                              ("opiniao.inf.br", "datahq.opiniao.inf.br")
                          ]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=True)

        fixed = path.read_text(encoding="utf-8")
        self.assertIn("#FIX# AuthUserFile", fixed)
        self.assertIn("Require valid-user", fixed)

    def test_apply_comments_php_value(self) -> None:
        path = self._write_htaccess(
            "site.example/public_html/.htaccess",
            "php_value upload_max_filesize 64M\n"
            "php_admin_value memory_limit 256M\n"
            "RewriteEngine on\n",
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["site.example"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=True)
        fixed = path.read_text(encoding="utf-8")
        self.assertIn("#FIX# php_value", fixed)
        self.assertIn("#FIX# php_admin_value", fixed)
        self.assertIn("RewriteEngine on", fixed)

    def test_apply_preserves_php_value_engine_off(self) -> None:
        """SEGURANÇA: `php_value engine off` é proteção contra execução de
        PHP em diretórios de upload (anti-RCE). NUNCA deve ser comentado."""
        path = self._write_htaccess(
            "site.example/public_html/uploads/.htaccess",
            "php_value engine off\n"
            "  php_admin_value engine off\n"
            "php_value upload_max_filesize 64M\n",
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["site.example"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=True)
        fixed = path.read_text(encoding="utf-8")
        # `engine off` lines preserved unmodified
        self.assertIn("php_value engine off", fixed)
        self.assertIn("php_admin_value engine off", fixed)
        self.assertNotIn("#FIX# php_value engine off", fixed)
        self.assertNotIn("#FIX# php_admin_value engine off", fixed)
        # Other php_value still commented out
        self.assertIn("#FIX# php_value upload_max_filesize", fixed)

    def test_apply_is_idempotent(self) -> None:
        """Re-rodar com apply em arquivo já fixado não duplica `#FIX# ` nem
        cria backup novo se nada mudar."""
        path = self._write_htaccess(
            "site.example/public_html/.htaccess",
            "#FIX# suPHP_ConfigPath /home/foo/public_html/\n"
            "RewriteEngine on\n",
        )
        original = path.read_text(encoding="utf-8")
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["site.example"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=True)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        backups = list(path.parent.glob(".htaccess.pre-htaccess-fix-*.bak"))
        # Backup só é criado se algo foi modificado.
        self.assertEqual(backups, [])

    def test_apply_writes_audit_csv(self) -> None:
        self._write_htaccess(
            "site.example/public_html/.htaccess",
            "suPHP_ConfigPath /home/foo/public_html/\n",
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["site.example"]), \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             self._patch_vhosts():
            self.orch.fix_htaccess(apply=True)
        csv = self.orch.log_dir / "htaccess-audit.csv"
        self.assertTrue(csv.exists())
        body = csv.read_text(encoding="utf-8")
        self.assertIn("suPHP_ConfigPath", body)


if __name__ == "__main__":
    unittest.main()
