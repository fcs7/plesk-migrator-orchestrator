"""Tests for fix_ftp_passwords phase: reset FTP subaccount passwords
(cPanel crypt-MD5 → Plesk SHA-512 incompatibility).

FTP subaccounts created by plesk-migrator from cPanel inherit crypt-MD5
hashes that fail Plesk Linux SHA-512 authentication (530 Login incorrect).
The renamed subaccounts (user@dom → user_dom) are listed in the session's
accounts_report_tree.*, which fix_ftp_passwords parses as authoritative
source. Subscription main FTP user (created fresh by Plesk) is NOT affected
and intentionally skipped.

Reset uses `plesk bin ftpsubaccount --update <login> -passwd "" -domain <dom>`
with password supplied via PSA_PASSWORD env (avoids /proc/<pid>/cmdline leak).
"""
from __future__ import annotations

import csv
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator


def _report_line(original: str, new: str) -> str:
    return (
        f"  warning: Login of FTP user '{original}' does not conform to "
        f"Plesk rules. It was changed to '{new}'"
    )


def _ok_proc() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail_proc(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr,
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
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run"
            ) as run_mock:
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

    def test_reset_invokes_ftpsubaccount_via_env_and_writes_chmod_600_csv(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("strauss@opiniao.inf.br", "strauss_opiniao.inf.br"),
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                return_value=_ok_proc(),
            ) as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_called_once()
        call_args = run_mock.call_args
        cmd = call_args[0][0]
        kwargs = call_args[1]
        # Argv: plesk bin ftpsubaccount --update <login> -passwd "" -domain <dom>
        self.assertEqual(cmd[0], "/usr/local/psa/bin/plesk")
        self.assertEqual(cmd[1], "bin")
        self.assertEqual(cmd[2], "ftpsubaccount")
        self.assertEqual(cmd[3], "--update")
        self.assertEqual(cmd[4], "strauss_opiniao.inf.br")
        self.assertEqual(cmd[5], "-passwd")
        self.assertEqual(cmd[6], "")  # empty — actual password in env
        self.assertIn("-domain", cmd)
        self.assertIn("opiniao.inf.br", cmd)

        # Env must carry PSA_PASSWORD (NOT in argv → no /proc leak)
        env = kwargs.get("env")
        self.assertIsNotNone(env)
        self.assertIn("PSA_PASSWORD", env)
        password = env["PSA_PASSWORD"]
        self.assertGreater(len(password), 20)
        self.assertLess(len(password), 30)
        self.assertNotIn(password[0], "-_")
        # Password must NOT appear in argv
        self.assertNotIn(password, cmd)

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
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run"
            ) as run_mock:
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
        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run"
            ) as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_not_called()
        self.assertFalse(
            (orch.log_dir / "ftp-password-reset.csv").exists()
        )

    def test_ftpsubaccount_nonzero_rc_logs_continues(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("user1@opiniao.inf.br", "user1_opiniao.inf.br"),
            ("user2@opiniao.inf.br", "user2_opiniao.inf.br"),
        ])

        def run_side_effect(cmd, **kwargs):
            if "user1_opiniao.inf.br" in cmd:
                return _fail_proc("ftpsubaccount: user not found")
            return _ok_proc()

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                side_effect=run_side_effect,
            ):
                orch.fix_ftp_passwords(reset=True)

        csv_path = orch.log_dir / "ftp-password-reset.csv"
        self.assertTrue(csv_path.exists())
        with csv_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["login"], "user2_opiniao.inf.br")
        self.assertTrue(orch.logger.error.called)

    def test_ftpsubaccount_timeout_logs_continues(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("user1@opiniao.inf.br", "user1_opiniao.inf.br"),
            ("user2@opiniao.inf.br", "user2_opiniao.inf.br"),
        ])

        calls = {"n": 0}
        def run_side_effect(cmd, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
            return _ok_proc()

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                side_effect=run_side_effect,
            ):
                orch.fix_ftp_passwords(reset=True)

        csv_path = orch.log_dir / "ftp-password-reset.csv"
        with csv_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["login"], "user2_opiniao.inf.br")
        self.assertTrue(orch.logger.error.called)

    def test_skips_logins_outside_migrated_domains(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("user@x.com", "user_x.com"),
            ("user@opiniao.inf.br", "user_opiniao.inf.br"),
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                return_value=_ok_proc(),
            ) as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[4], "user_opiniao.inf.br")

    def test_dedup_duplicate_new_logins(self) -> None:
        orch = self._make_orch()
        self._write_report(orch, [
            ("user@opiniao.inf.br", "user_opiniao.inf.br"),
            ("user@opiniao.inf.br", "user_opiniao.inf.br"),
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                return_value=_ok_proc(),
            ) as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_called_once()
        csv_path = orch.log_dir / "ftp-password-reset.csv"
        with csv_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)

    def test_dry_run_short_circuits_before_subprocess_call(self) -> None:
        orch = self._make_orch()
        orch.dry_run = True
        self._write_report(orch, [
            ("user@opiniao.inf.br", "user_opiniao.inf.br"),
        ])

        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["opiniao.inf.br"]
        ):
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run"
            ) as run_mock:
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
            with mock.patch(
                "plesk_migrator_orchestrator.subprocess.run",
                return_value=_ok_proc(),
            ) as run_mock:
                orch.fix_ftp_passwords(reset=True)

        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[4], "new_opiniao.inf.br")


if __name__ == "__main__":
    unittest.main()
