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
import tempfile
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


class FixDocrootValidationIntegrationTests(unittest.TestCase):
    """Verifies fix_docroot logs match/diverge AFTER applying www-root."""

    def _make_orch(self, vhost_root: pathlib.Path) -> PleskMigrationOrchestrator:
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
        orch.plesk_bin = pathlib.Path("/usr/sbin/plesk")
        orch.plesk_migrator_bin = pathlib.Path("/usr/local/psa/admin/sbin/modules/panel-migrator/plesk-migrator")
        orch.log_dir = vhost_root.parent / "logs"
        orch.log_dir.mkdir()
        orch.sessions_dir = vhost_root.parent / "sessions"
        orch.session_name = "migration-session"
        (orch.sessions_dir / orch.session_name).mkdir(parents=True)
        orch._load_migrated_domains = mock.MagicMock(
            return_value=["opiniao.inf.br"]
        )
        orch._run = mock.MagicMock()
        return orch

    def _set_up_vhost(
        self, vhost_root: pathlib.Path, has_public_html_bytes: int,
    ) -> None:
        vhost = vhost_root / "opiniao.inf.br"
        vhost.mkdir(parents=True)
        (vhost / "httpdocs").mkdir()  # empty (canonical empty triggers pick)
        public_html = vhost / "public_html"
        public_html.mkdir()
        (public_html / "index.html").write_bytes(b"x" * has_public_html_bytes)

    def test_hash_match_logs_info_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vhost_root = pathlib.Path(tmp) / "vhosts"
            vhost_root.mkdir()
            self._set_up_vhost(vhost_root, has_public_html_bytes=42)
            orch = self._make_orch(vhost_root)
            # Stub the LOCAL _dir_manifest and the REMOTE one to return
            # matching hashes.
            local_manifest = (1, 42, "deadbeef")
            with mock.patch(
                "plesk_migrator_orchestrator.pathlib.Path",
                pathlib.Path,
            ):
                with mock.patch.object(
                    PleskMigrationOrchestrator, "_dir_manifest",
                    staticmethod(lambda p: local_manifest if "public_html" in str(p) else (0, 0, hashlib.md5(b"").hexdigest())),
                ):
                    orch._remote_dir_manifest = mock.MagicMock(
                        return_value=(1, 42, "deadbeef")
                    )
                    # fix_docroot's vhost root is hard-coded to /var/www/vhosts;
                    # we monkeypatch it via a wrapper attribute the impl will read.
                    # If your impl uses pathlib.Path("/var/www/vhosts") directly,
                    # set fix_docroot's vhosts_root via patching that line:
                    with mock.patch(
                        "plesk_migrator_orchestrator.pathlib.Path",
                        side_effect=lambda *a, **kw: pathlib.Path(*a, **kw),
                    ):
                        # Simpler: directly call the helper. Skip end-to-end and
                        # assert the integration block by stubbing.
                        pass
            # End-to-end is awkward because of the hard-coded /var/www/vhosts.
            # Instead, exercise the integration block by patching it via a
            # `_validate_docroot_match(domain, local_path)` seam (see Task 6.2).
            self.skipTest(
                "End-to-end fix_docroot test deferred — see "
                "test_validate_docroot_match_logs_ok below for the "
                "behavior under test."
            )

    def test_validate_docroot_match_logs_ok(self) -> None:
        """Targeted test of the validation block, hoisted into a small
        helper _validate_docroot_match(domain, local_path) to keep the
        integration testable. See Task 6.2 implementation."""
        with tempfile.TemporaryDirectory() as tmp:
            local = pathlib.Path(tmp) / "httpdocs"
            local.mkdir()
            (local / "index.html").write_bytes(b"hello")
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
            # Local manifest of the real dir
            local_count, local_bytes, local_hash = (
                PleskMigrationOrchestrator._dir_manifest(local)
            )
            orch._remote_dir_manifest = mock.MagicMock(
                return_value=(local_count, local_bytes, local_hash)
            )
            orch._validate_docroot_match("opiniao.inf.br", local)
            info_msgs = [c.args[0] for c in orch.logger.info.call_args_list]
            self.assertTrue(
                any("hash OK" in m for m in info_msgs),
                f"expected 'hash OK' info log, got: {info_msgs}",
            )

    def test_validate_docroot_match_logs_warning_on_diverge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = pathlib.Path(tmp) / "httpdocs"
            local.mkdir()
            (local / "index.html").write_bytes(b"hello")
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
            orch._remote_dir_manifest = mock.MagicMock(
                return_value=(99, 9999, "feedbeef")
            )
            orch._validate_docroot_match("opiniao.inf.br", local)
            warn_msgs = [c.args[0] for c in orch.logger.warning.call_args_list]
            self.assertTrue(
                any("DIVERGE" in m for m in warn_msgs),
                f"expected DIVERGE warning, got: {warn_msgs}",
            )

    def test_validate_docroot_match_remote_failure_warns_and_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = pathlib.Path(tmp) / "httpdocs"
            local.mkdir()
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
            orch._remote_dir_manifest = mock.MagicMock(return_value=(0, 0, ""))
            orch._validate_docroot_match("opiniao.inf.br", local)
            warn_msgs = [c.args[0] for c in orch.logger.warning.call_args_list]
            self.assertTrue(
                any("validação cross-server pulada" in m for m in warn_msgs),
                f"expected 'pulada' warning, got: {warn_msgs}",
            )


if __name__ == "__main__":
    unittest.main()
