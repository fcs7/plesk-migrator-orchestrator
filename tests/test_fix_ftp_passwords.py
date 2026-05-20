"""Tests for fix_ftp_passwords phase: reset FTP subaccount passwords
(cPanel crypt-MD5 → Plesk SHA-512 incompatibility).

FTP subaccounts created by plesk-migrator from cPanel inherit crypt-MD5
hashes that fail Plesk Linux SHA-512 authentication (530 Login incorrect).
The renamed subaccounts (user@dom → user_dom) are listed in the session's
accounts_report_tree.*, which fix_ftp_passwords now parses as authoritative
source. Subscription main FTP user (created fresh by Plesk) is NOT affected
and intentionally skipped.

Contract mirrors check_mail_passwords:
- Audit-only (default): writes ftp-password-status.csv
- reset=True: generates secrets.token_urlsafe(16), calls plesk bin ftpuser,
  writes chmod-600 ftp-password-reset.csv, adds to sensitive_values.
"""
from __future__ import annotations

import csv
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator, PhaseExecutionError


def _report_line(original: str, new: str) -> str:
    return (
        f"  warning: Login of FTP user '{original}' does not conform to "
        f"Plesk rules. It was changed to '{new}'"
    )


class FixFtpPasswordsTests(unittest.TestCase):
    def _make_orch(self) -> PleskMigrationOrchestrator:
        orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
        orch.dry_run = False
        orch.logger = mock.MagicMock()
        orch.plesk_bin = pathlib.Path("/usr/local/psa/bin/plesk")
        base = pathlib.Path(tempfile.mkdtemp(prefix="test-fix-ftp-pw-"))
        orch.log_dir = base / "logs"
        orch.log_dir.mkdir(parents=True, exist_ok=True)
        orch.sessions_dir = base / "sessions"
        orch.session_name = "migration-session"
        (orch.sessions_dir / orch.session_name).mkdir(parents=True, exist_ok=True)
        orch.sensitive_values = []
        return orch

    def _write_report(
        self,
        orch: PleskMigrationOrchestrator,
        renames: list[tuple[str, str]],
        ts: str = "2026.05.20.17.00.00",
    ) -> None:
        session = orch.sessions_dir / orch.session_name
        body = "Detailed Migration Status\n" + "\n".join(
            _report_line(o, n) for o, n in renames
        ) + "\n"
        (session / f"accounts_report_tree.{ts}").write_text(body)

    def test_audit_writes_status_csv_no_reset(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("strauss@opiniao.inf.br", "strauss_opiniao.inf.br"),
            ("fhepoupex@opiniao.inf.br", "fhepoupex_opiniao.inf.br"),
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run") as run_mock:
                orch.fix_ftp_passwords(reset=False)

        run_mock.assert_not_called()
        csv_path = orch.log_dir / "ftp-password-status.csv"
        self.assertTrue(csv_path.exists())

        with csv_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        logins = {r["login"] for r in rows}
        self.assertEqual(
            logins,
            {"strauss_opiniao.inf.br", "fhepoupex_opiniao.inf.br"},
        )
        for r in rows:
            self.assertEqual(r["domain"], "opiniao.inf.br")
            self.assertIn("timestamp", r)

    def test_reset_invokes_ftpuser_passwd_and_writes_chmod_600_csv(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("strauss@opiniao.inf.br", "strauss_opiniao.inf.br"),
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run") as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_called_once()
        call_args = run_mock.call_args[0][0]
        self.assertEqual(call_args[0], "/usr/local/psa/bin/plesk")
        self.assertEqual(call_args[1], "bin")
        self.assertEqual(call_args[2], "ftpuser")
        self.assertEqual(call_args[3], "-u")
        self.assertEqual(call_args[4], "strauss_opiniao.inf.br")
        self.assertEqual(call_args[5], "-passwd")
        password = call_args[6]
        self.assertGreater(len(password), 20)
        self.assertLess(len(password), 30)
        self.assertNotIn(password[0], "-_")

        csv_path = orch.log_dir / "ftp-password-reset.csv"
        self.assertTrue(csv_path.exists())
        file_mode = csv_path.stat().st_mode & 0o777
        self.assertEqual(file_mode, 0o600)

        with csv_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["login"], "strauss_opiniao.inf.br")
        self.assertEqual(rows[0]["domain"], "opiniao.inf.br")
        self.assertEqual(rows[0]["new_password"], password)
        self.assertIn(password, orch.sensitive_values)

    def test_no_renames_in_report_is_noop(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run") as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_not_called()
        self.assertFalse(
            (orch.log_dir / "ftp-password-status.csv").exists()
        )
        self.assertFalse(
            (orch.log_dir / "ftp-password-reset.csv").exists()
        )

    def test_missing_report_is_noop(self) -> None:
        orch = self._make_orch()
        # no report written

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run") as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_not_called()
        self.assertFalse(
            (orch.log_dir / "ftp-password-reset.csv").exists()
        )

    def test_ftpuser_failure_logs_continues(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("user1@opiniao.inf.br", "user1_opiniao.inf.br"),
            ("user2@opiniao.inf.br", "user2_opiniao.inf.br"),
        ])

        def run_side_effect(*args, **kwargs):
            cmd = args[0]
            if "user1_opiniao.inf.br" in cmd:
                raise PhaseExecutionError("Mock failure user1")
            # user2 succeeds (returns None)

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run", side_effect=run_side_effect):
                orch.fix_ftp_passwords(reset=True)

        csv_path = orch.log_dir / "ftp-password-reset.csv"
        self.assertTrue(csv_path.exists())
        with csv_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["login"], "user2_opiniao.inf.br")
        self.assertTrue(orch.logger.error.called)

    def test_skips_logins_outside_migrated_domains(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("user@x.com", "user_x.com"),  # NOT in migrated
            ("user@opiniao.inf.br", "user_opiniao.inf.br"),  # in migrated
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run") as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_called_once()
        call_args = run_mock.call_args[0][0]
        self.assertEqual(call_args[4], "user_opiniao.inf.br")

    def test_dedup_duplicate_new_logins(self) -> None:
        orch = self._make_orch()
        # Same new_login appearing twice (e.g. retransfer report)
        self._write_report(orch, [
            ("user@opiniao.inf.br", "user_opiniao.inf.br"),
            ("user@opiniao.inf.br", "user_opiniao.inf.br"),
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run") as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_called_once()
        csv_path = orch.log_dir / "ftp-password-reset.csv"
        with csv_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)

    def test_dry_run_short_circuits_before_ftpuser_call(self) -> None:
        orch = self._make_orch()
        orch.dry_run = True
        self._write_report(orch, [
            ("user@opiniao.inf.br", "user_opiniao.inf.br"),
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run") as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_not_called()
        self.assertFalse(
            (orch.log_dir / "ftp-password-reset.csv").exists()
        )

    def test_uses_latest_report_when_multiple_exist(self) -> None:
        orch = self._make_orch()
        self._write_report(
            orch,
            [("old@opiniao.inf.br", "old_opiniao.inf.br")],
            ts="2026.05.19.10.00.00",
        )
        self._write_report(
            orch,
            [("new@opiniao.inf.br", "new_opiniao.inf.br")],
            ts="2026.05.20.17.00.00",
        )

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch.object(orch, "_run") as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_called_once()
        call_args = run_mock.call_args[0][0]
        self.assertEqual(call_args[4], "new_opiniao.inf.br")


if __name__ == "__main__":
    unittest.main()
