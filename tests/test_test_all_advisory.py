"""Cobre `test_all` advisory: rc=1 com warnings de Apache/FTP/SSH/DB-password
não aborta o pipeline. Falha hard (Plesk inacessível, report ausente) ainda
levanta PhaseExecutionError.

Formato real de `test_all_report.<ts>` é texto hierárquico tree-style:

    Transferred Domains' Functional Issues
    |
    `- Client 'opiniaoi'
       `- Subscription 'opiniao.inf.br'
          |- Apache web site 'opiniao.inf.br'
          |  |- warning: The hosting checker cannot connect ...
          |  `- error: The HTTP status code of a web page has changed ...
          |     The status code returned by the source server (IP address X): 200
          |     The status code returned by the target server (IP address Y): 500

Classificação:
  * `ok`   — 0 `error:` linhas (só warnings).
  * `soft` — todas as `error:` linhas são categorias advisory (Apache HTTP
             status, FTP `530 Login incorrect`, SSH/DB `User password is
             encrypted`).
  * `hard` — pelo menos 1 erro não-mapeado, ou exit por sinal (rc<0), ou
             report ausente.
"""

from __future__ import annotations

import pathlib
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import MagicMock, patch

from plesk_migrator_orchestrator import (
    PhaseExecutionError,
    PleskMigrationOrchestrator,
)


def _make_orchestrator(td: pathlib.Path) -> PleskMigrationOrchestrator:
    """Build an orchestrator via __new__ + attribute injection (same pattern
    used by tests/test_fix_docroot_validation.py and test_fix_ftp_passwords.py)
    to bypass YAML validation and stay hermetic from real config files."""
    from unittest import mock as _mock
    orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
    orch.dry_run = False
    orch.logger = _mock.MagicMock()
    orch.config = {
        "source": {"host": "cpanel.example.com", "ssh_port": 22,
                   "ssh_password": "secret"},
        "dest": {"host": "plesk.example.com"},
    }
    orch.sensitive_values = []
    orch.plesk_migrator_bin = pathlib.Path("/usr/local/psa/admin/sbin/plesk-migrator")
    orch.log_dir = td / "logs"
    orch.log_dir.mkdir(parents=True, exist_ok=True)
    orch.sessions_dir = td / "sessions"
    orch.session_name = "migration-session"
    sess = orch.sessions_dir / orch.session_name
    sess.mkdir(parents=True, exist_ok=True)
    return orch


class ClassifyTestAllReportTest(unittest.TestCase):
    """Unit tests para `_classify_test_all_report`."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = pathlib.Path(self._td.name)
        self.orch = _make_orchestrator(self.tmp)
        self.session_dir = self.orch.sessions_dir / self.orch.session_name

    def _write_report(self, ts: str, body: str) -> pathlib.Path:
        path = self.session_dir / f"test_all_report.{ts}"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_no_errors_classifies_ok(self) -> None:
        self._write_report(
            "2026.05.22.10.00.00",
            """
            Transferred Domains' Functional Issues
            `- Client 'foo'
               `- Subscription 'bar.example'
                  `- Apache web site 'bar.example'
                     `- warning: The hosting checker cannot connect.
            """,
        )
        verdict, summary = self.orch._classify_test_all_report()
        self.assertEqual(verdict, "ok")
        self.assertEqual(summary["error_count"], 0)

    def test_all_advisory_errors_classifies_soft(self) -> None:
        self._write_report(
            "2026.05.22.10.05.00",
            """
            Transferred Domains' Functional Issues
            `- Client 'opiniaoi'
               `- Subscription 'opiniao.inf.br'
                  |- Apache web site 'opiniao.inf.br'
                  |  `- error: The HTTP status code of a web page has changed after the migration.
                  |     The status code returned by the source server (IP address 1): 200
                  |     The status code returned by the target server (IP address 2): 500
                  |- Apache web site 'vivest.opiniao.inf.br'
                  |  `- error: The HTTP status code of a web page has changed after the migration.
                  |     The status code returned by the source server (IP address 1): 200
                  |     The status code returned by the target server (IP address 2): 403
                  |- FTP user 'strauss_opiniao.inf.br'
                  |  `- error: 530 Login incorrect.
                  `- Database 'opiniao_db'
                     `- error: Cannot verify access. User password is encrypted.
            """,
        )
        verdict, summary = self.orch._classify_test_all_report()
        self.assertEqual(verdict, "soft", f"summary={summary!r}")
        self.assertGreaterEqual(summary["error_count"], 4)
        self.assertEqual(summary["unmapped_errors"], 0)

    def test_unmapped_error_classifies_hard(self) -> None:
        self._write_report(
            "2026.05.22.10.10.00",
            """
            Transferred Domains' Functional Issues
            `- Client 'foo'
               `- Subscription 'bar.example'
                  `- Database 'bar_db'
                     `- error: Schema migration failed: column 'foo' already exists.
            """,
        )
        verdict, summary = self.orch._classify_test_all_report()
        self.assertEqual(verdict, "hard")
        self.assertGreaterEqual(summary["unmapped_errors"], 1)

    def test_missing_report_classifies_hard(self) -> None:
        verdict, summary = self.orch._classify_test_all_report()
        self.assertEqual(verdict, "hard")
        self.assertIn("missing", summary.get("reason", ""))

    def test_picks_latest_timestamp_when_multiple(self) -> None:
        self._write_report(
            "2026.05.22.10.00.00",
            """
            Transferred Domains' Functional Issues
            `- Client 'foo'
               `- Database 'foo_db'
                  `- error: catastrophic.
            """,
        )
        # latest = soft only — should win because we pick by sort order
        self._write_report(
            "2026.05.22.11.00.00",
            """
            Transferred Domains' Functional Issues
            `- Client 'foo'
               `- Apache web site 'foo.example'
                  `- error: The HTTP status code of a web page has changed after the migration.
            """,
        )
        verdict, _ = self.orch._classify_test_all_report()
        self.assertEqual(verdict, "soft")


class TestAllAdvisoryFlowTest(unittest.TestCase):
    """Integration: `test_all` invokes plesk-migrator, then classifies."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = pathlib.Path(self._td.name)
        self.orch = _make_orchestrator(self.tmp)
        self.session_dir = self.orch.sessions_dir / self.orch.session_name

    def _seed_report(self, body: str) -> None:
        path = self.session_dir / "test_all_report.2026.05.22.12.00.00"
        path.write_text(textwrap.dedent(body), encoding="utf-8")

    def test_rc_zero_passes_silently(self) -> None:
        proc = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch.object(self.orch, "_require_plesk_migrator_bin"), \
             patch.object(self.orch, "_require_runtime_state"), \
             patch("subprocess.run", return_value=proc):
            self.orch.test_all()  # must not raise

    def test_rc_one_with_soft_errors_warns_does_not_raise(self) -> None:
        self._seed_report(
            """
            Transferred Domains' Functional Issues
            `- Client 'opiniaoi'
               `- Apache web site 'foo.example'
                  `- error: The HTTP status code of a web page has changed after the migration.
            """,
        )
        proc = MagicMock(returncode=1, stdout="warning output", stderr="")
        with patch.object(self.orch, "_require_plesk_migrator_bin"), \
             patch.object(self.orch, "_require_runtime_state"), \
             patch("subprocess.run", return_value=proc):
            self.orch.test_all()  # must NOT raise — advisory soft

    def test_rc_one_with_hard_error_raises(self) -> None:
        self._seed_report(
            """
            Transferred Domains' Functional Issues
            `- Client 'foo'
               `- Database 'foo_db'
                  `- error: Schema migration failed.
            """,
        )
        proc = MagicMock(returncode=1, stdout="", stderr="hard failure")
        with patch.object(self.orch, "_require_plesk_migrator_bin"), \
             patch.object(self.orch, "_require_runtime_state"), \
             patch("subprocess.run", return_value=proc):
            with self.assertRaises(PhaseExecutionError):
                self.orch.test_all()

    def test_rc_one_without_report_raises_hard(self) -> None:
        # No report file written.
        proc = MagicMock(returncode=1, stdout="", stderr="plesk down")
        with patch.object(self.orch, "_require_plesk_migrator_bin"), \
             patch.object(self.orch, "_require_runtime_state"), \
             patch("subprocess.run", return_value=proc):
            with self.assertRaises(PhaseExecutionError):
                self.orch.test_all()

    def test_subprocess_timeout_raises_hard(self) -> None:
        with patch.object(self.orch, "_require_plesk_migrator_bin"), \
             patch.object(self.orch, "_require_runtime_state"), \
             patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("plesk-migrator", 1)):
            with self.assertRaises(PhaseExecutionError):
                self.orch.test_all()

    def test_writes_summary_file(self) -> None:
        self._seed_report(
            """
            Transferred Domains' Functional Issues
            `- Client 'opiniaoi'
               `- Apache web site 'foo.example'
                  `- error: The HTTP status code of a web page has changed after the migration.
            """,
        )
        proc = MagicMock(returncode=1, stdout="", stderr="")
        with patch.object(self.orch, "_require_plesk_migrator_bin"), \
             patch.object(self.orch, "_require_runtime_state"), \
             patch("subprocess.run", return_value=proc):
            self.orch.test_all()
        summary_path = self.orch.log_dir / "test-all-summary.txt"
        self.assertTrue(summary_path.exists())
        body = summary_path.read_text(encoding="utf-8")
        self.assertIn("verdict=soft", body)


if __name__ == "__main__":
    unittest.main()
