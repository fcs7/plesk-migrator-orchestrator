"""Tests for fix_ftp_passwords phase: reset FTP user passwords (cPanel crypt-MD5 -> Plesk SHA-512).

FTP users created by plesk-migrator from cPanel with crypt-MD5 hashes cannot login
to Plesk's Linux FTP server (expects SHA-512). fix_ftp_passwords audits sys_users
and optionally resets passwords via `plesk bin ftpuser -u <login> -passwd <pwd>`.

Mirror contract of check_mail_passwords:
- Audit-only (default): writes ftp-password-status.csv
- reset=True: generates secrets.token_urlsafe(16), calls plesk bin ftpuser,
  writes chmod-600 ftp-password-reset.csv, adds to sensitive_values.
"""
from __future__ import annotations

import csv
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator, PhaseExecutionError


class FixFtpPasswordsTests(unittest.TestCase):
    def _make_orch(self) -> PleskMigrationOrchestrator:
        """Create a mock orchestrator with minimal required state."""
        orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
        orch.dry_run = False
        orch.logger = mock.MagicMock()
        orch.plesk_bin = pathlib.Path("/usr/local/psa/bin/plesk")
        # Use unique temp dir per test to avoid cross-test pollution
        orch.log_dir = pathlib.Path(tempfile.mkdtemp(prefix="test-orch-logs-"))
        orch.sensitive_values = []
        return orch

    def test_audit_writes_status_csv_no_reset(self) -> None:
        """Audit-only mode: writes status CSV, does NOT invoke plesk bin ftpuser."""
        orch = self._make_orch()

        # Mock subprocess.run to return SQL output with FTP users (tab-separated: login\tdomain)
        sql_output = "strauss_opiniao.inf.br\topiniao.inf.br\nfhepoupex_opiniao.inf.br\topiniao.inf.br\n"
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sql_output, stderr=""
        )

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                return_value=completed,
            ) as run_mock:
                with mock.patch.object(orch, "_run") as run_method_mock:
                    orch.fix_ftp_passwords(reset=False)

        # Should call subprocess.run for SQL query
        self.assertEqual(run_mock.call_count, 1)

        # Should NOT call _run (plesk bin ftpuser)
        run_method_mock.assert_not_called()

        # Should write status CSV
        csv_path = orch.log_dir / "ftp-password-status.csv"
        self.assertTrue(csv_path.exists())

        # Verify CSV contents
        rows = []
        with csv_path.open("r") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["login"], "strauss_opiniao.inf.br")
        self.assertEqual(rows[1]["login"], "fhepoupex_opiniao.inf.br")
        self.assertIn("timestamp", rows[0])

    def test_reset_invokes_ftpuser_passwd_and_writes_chmod_600_csv(self) -> None:
        """Reset mode: generates passwords, calls plesk bin ftpuser, writes chmod-600 CSV."""
        orch = self._make_orch()

        # Mock subprocess.run to return SQL output (tab-separated: login\tdomain)
        sql_output = "strauss_opiniao.inf.br\topiniao.inf.br\n"
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sql_output, stderr=""
        )

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                return_value=completed,
            ):
                with mock.patch.object(orch, "_run") as run_method_mock:
                    orch.fix_ftp_passwords(reset=True)

        # Should call _run exactly once with plesk bin ftpuser command
        run_method_mock.assert_called_once()
        call_args = run_method_mock.call_args[0][0]

        # Verify command structure
        self.assertEqual(call_args[0], "/usr/local/psa/bin/plesk")
        self.assertEqual(call_args[1], "bin")
        self.assertEqual(call_args[2], "ftpuser")
        self.assertEqual(call_args[3], "-u")
        self.assertEqual(call_args[4], "strauss_opiniao.inf.br")
        self.assertEqual(call_args[5], "-passwd")

        # Password should be 22 chars (secrets.token_urlsafe(16) ≈ 22 chars)
        password = call_args[6]
        self.assertGreater(len(password), 20)
        self.assertLess(len(password), 30)

        # Password should NOT start with - or _
        self.assertNotIn(password[0], "-_")

        # Should write reset CSV
        csv_path = orch.log_dir / "ftp-password-reset.csv"
        self.assertTrue(csv_path.exists())

        # Verify CSV is chmod 600
        stat_info = csv_path.stat()
        file_mode = stat_info.st_mode & 0o777
        self.assertEqual(file_mode, 0o600)

        # Verify CSV contents
        rows = []
        with csv_path.open("r") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["login"], "strauss_opiniao.inf.br")
        self.assertEqual(rows[0]["new_password"], password)
        self.assertIn("timestamp", rows[0])

        # Password should be in sensitive_values
        self.assertIn(password, orch.sensitive_values)

    def test_no_users_is_noop(self) -> None:
        """Empty SQL result: no CSV written, no _run calls."""
        orch = self._make_orch()

        # Mock subprocess.run to return empty output
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                return_value=completed,
            ):
                with mock.patch.object(orch, "_run") as run_method_mock:
                    orch.fix_ftp_passwords(reset=True)

        # Should NOT call _run
        run_method_mock.assert_not_called()

        # Should NOT write status or reset CSV
        csv_path = orch.log_dir / "ftp-password-status.csv"
        reset_csv_path = orch.log_dir / "ftp-password-reset.csv"
        self.assertFalse(csv_path.exists())
        self.assertFalse(reset_csv_path.exists())

    def test_ftpuser_failure_logs_continues(self) -> None:
        """Per-user failure: logs error, continues with next user."""
        orch = self._make_orch()

        # Mock subprocess.run to return SQL output with 2 users (tab-separated)
        sql_output = "user1@opiniao.inf.br\topiniao.inf.br\nuser2@opiniao.inf.br\topiniao.inf.br\n"
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sql_output, stderr=""
        )

        # First user fails, second succeeds
        def run_side_effect(*args, **kwargs):
            cmd = args[0]
            if "user1" in " ".join(cmd):
                raise PhaseExecutionError("Mock failure for user1")
            # Return successfully for user2

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                return_value=completed,
            ):
                with mock.patch.object(orch, "_run", side_effect=run_side_effect):
                    # Should not raise, should continue
                    orch.fix_ftp_passwords(reset=True)

        # Should write reset CSV with user2 only
        csv_path = orch.log_dir / "ftp-password-reset.csv"
        self.assertTrue(csv_path.exists())

        rows = []
        with csv_path.open("r") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        # Only user2 should be in CSV (user1 failed)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["login"], "user2@opiniao.inf.br")

        # Logger should have been called with error for user1
        self.assertTrue(orch.logger.error.called)


if __name__ == "__main__":
    unittest.main()
