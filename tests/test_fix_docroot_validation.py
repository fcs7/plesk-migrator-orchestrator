"""Tests for cross-server manifest comparison in fix_docroot.

_remote_dir_manifest runs:
  find <path> -type f -printf '%P\t%s\n' | sort | md5sum
over SSH on the cPanel source host (sshpass + ssh) and returns
(count, total_bytes, md5_hex) using the same semantics as _dir_manifest
so the two are directly comparable."""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator


def _expected_md5(entries: list[tuple[str, int]]) -> str:
    body = "\n".join(f"{p}:{s}" for p, s in entries)
    return hashlib.md5(body.encode("utf-8")).hexdigest()


class RemoteDirManifestTests(unittest.TestCase):
    def _make_orch(self) -> PleskMigrationOrchestrator:
        orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
        orch.dry_run = False
        orch.logger = mock.MagicMock()
        orch.config = {
            "source": {
                "host": "cpanel.example.com",
                "ssh_port": 22,
                "ssh_password": "secret",
            },
        }
        return orch

    def test_parses_remote_output_into_count_bytes_md5(self) -> None:
        orch = self._make_orch()
        # Remote command emits:
        #   index.html\t5
        #   sub/a.txt\t2
        # then `sort | md5sum` produces "<md5>  -" plus we WRAP the find
        # output to also send count/total via tee'd format. Implementation
        # detail: the helper runs a small shell pipeline that prints
        # "COUNT=<n>\nTOTAL=<b>\nMD5=<hex>\n" so parsing is deterministic.
        remote_md5 = _expected_md5([("index.html", 5), ("sub/a.txt", 2)])
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=f"COUNT=2\nTOTAL=7\nMD5={remote_md5}\n",
            stderr="",
        )
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            return_value=completed,
        ) as run_mock:
            count, total, digest = orch._remote_dir_manifest(
                "/home/opiniaoi/public_html"
            )
        self.assertEqual(count, 2)
        self.assertEqual(total, 7)
        self.assertEqual(digest, remote_md5)
        # Verify the command actually invoked sshpass + ssh against the
        # configured host/port/password and ran the canonical pipeline.
        called_args = run_mock.call_args[0][0]
        self.assertEqual(called_args[0], "sshpass")
        self.assertIn("-p", called_args)
        self.assertIn("secret", called_args)
        self.assertIn("ssh", called_args)
        self.assertIn("cpanel.example.com", " ".join(called_args))
        self.assertIn("/home/opiniaoi/public_html", " ".join(called_args))

    def test_ssh_failure_returns_zero_zero_empty(self) -> None:
        orch = self._make_orch()
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            side_effect=OSError("sshpass missing"),
        ):
            count, total, digest = orch._remote_dir_manifest("/tmp/x")
        self.assertEqual((count, total, digest), (0, 0, ""))

    def test_nonzero_rc_returns_zero_zero_empty(self) -> None:
        orch = self._make_orch()
        completed = subprocess.CompletedProcess(
            args=[], returncode=255, stdout="", stderr="Permission denied",
        )
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            return_value=completed,
        ):
            count, total, digest = orch._remote_dir_manifest("/tmp/x")
        self.assertEqual((count, total, digest), (0, 0, ""))

    def test_dry_run_skips_ssh(self) -> None:
        orch = self._make_orch()
        orch.dry_run = True
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run"
        ) as run_mock:
            count, total, digest = orch._remote_dir_manifest("/tmp/x")
        run_mock.assert_not_called()
        self.assertEqual((count, total, digest), (0, 0, ""))

    def test_uses_custom_port(self) -> None:
        orch = self._make_orch()
        orch.config["source"]["ssh_port"] = 2222
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=f"COUNT=0\nTOTAL=0\nMD5={hashlib.md5(b'').hexdigest()}\n",
            stderr="",
        )
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            return_value=completed,
        ) as run_mock:
            orch._remote_dir_manifest("/tmp/x")
        args = run_mock.call_args[0][0]
        joined = " ".join(args)
        self.assertIn("-p 2222", joined)

    def test_malformed_output_returns_zero_zero_empty(self) -> None:
        orch = self._make_orch()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="garbage\n", stderr="",
        )
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            return_value=completed,
        ):
            count, total, digest = orch._remote_dir_manifest("/tmp/x")
        self.assertEqual((count, total, digest), (0, 0, ""))


if __name__ == "__main__":
    unittest.main()
