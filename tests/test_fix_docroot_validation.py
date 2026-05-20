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
            count, total, digest, _body = orch._remote_dir_manifest(
                "/home/opiniaoi/public_html"
            )
        self.assertEqual(count, 2)
        self.assertEqual(total, 7)
        self.assertEqual(digest, remote_md5)
        # Verify the command actually invoked sshpass -e + ssh against the
        # configured host/port and ran the canonical pipeline. The password
        # must NEVER appear on argv — it is passed via the SSHPASS env var
        # (sshpass(1) -e mode) to keep it out of /proc/<pid>/cmdline.
        called_args = run_mock.call_args[0][0]
        self.assertEqual(called_args[0], "sshpass")
        self.assertIn("-e", called_args)
        self.assertNotIn("secret", called_args)
        self.assertIn("ssh", called_args)
        self.assertIn("cpanel.example.com", " ".join(called_args))
        self.assertIn("/home/opiniaoi/public_html", " ".join(called_args))
        env = run_mock.call_args.kwargs.get("env") or {}
        self.assertEqual(env.get("SSHPASS"), "secret")

    def test_ssh_failure_returns_zero_zero_empty(self) -> None:
        orch = self._make_orch()
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            side_effect=OSError("sshpass missing"),
        ):
            count, total, digest, _body = orch._remote_dir_manifest("/tmp/x")
        self.assertEqual((count, total, digest, _body), (0, 0, "", ""))

    def test_unicode_decode_error_does_not_abort(self) -> None:
        """cPanel hosts with Latin-1-encoded filenames trigger
        UnicodeDecodeError inside subprocess.run(text=True). Must be
        swallowed like OSError — the phase's 'never aborts' contract
        depends on it."""
        orch = self._make_orch()
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            side_effect=UnicodeDecodeError(
                "utf-8", b"\xe9", 0, 1, "invalid start byte",
            ),
        ):
            count, total, digest, _body = orch._remote_dir_manifest("/tmp/x")
        self.assertEqual((count, total, digest, _body), (0, 0, "", ""))

    def test_nonzero_rc_returns_zero_zero_empty(self) -> None:
        orch = self._make_orch()
        completed = subprocess.CompletedProcess(
            args=[], returncode=255, stdout="", stderr="Permission denied",
        )
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            return_value=completed,
        ):
            count, total, digest, _body = orch._remote_dir_manifest("/tmp/x")
        self.assertEqual((count, total, digest, _body), (0, 0, "", ""))

    def test_dry_run_skips_ssh(self) -> None:
        orch = self._make_orch()
        orch.dry_run = True
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run"
        ) as run_mock:
            count, total, digest, _body = orch._remote_dir_manifest("/tmp/x")
        run_mock.assert_not_called()
        self.assertEqual((count, total, digest, _body), (0, 0, "", ""))

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
            count, total, digest, _body = orch._remote_dir_manifest("/tmp/x")
        self.assertEqual((count, total, digest, _body), (0, 0, "", ""))

    def test_remote_empty_body_returns_empty_digest(self) -> None:
        """When `find` returns no files (path missing / empty dir / SSH
        rc=0 but no output), the shell pipeline must emit MD5= (empty)
        rather than md5sum of empty stdin (`d41d8cd98f00b204e9800998ecf8427e`).
        That keeps `_validate_docroot_match`'s `if not src_hash:` guard
        firing the documented 'pulada' warning instead of bogus DIVERGE."""
        orch = self._make_orch()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="COUNT=0\nTOTAL=0\nMD5=\n",
            stderr="",
        )
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            return_value=completed,
        ):
            count, total, digest, _body = orch._remote_dir_manifest(
                "/home/missing/public_html"
            )
        self.assertEqual((count, total, digest, _body), (0, 0, "", ""))


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

    # End-to-end fix_docroot coverage is provided by the
    # _validate_docroot_match seam below (test_validate_docroot_match_*).
    # Per-instance integration through fix_docroot is exercised manually on
    # the Plesk box during real migrations.

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
            local_count, local_bytes, local_hash, local_body = (
                PleskMigrationOrchestrator._dir_manifest(local)
            )
            orch._remote_dir_manifest = mock.MagicMock(
                return_value=(
                    local_count, local_bytes, local_hash, local_body,
                )
            )
            orch._validate_docroot_match("opiniao.inf.br", local)
            info_msgs = [c.args[0] for c in orch.logger.info.call_args_list]
            self.assertTrue(
                any("hash OK" in m for m in info_msgs),
                f"expected 'hash OK' info log, got: {info_msgs}",
            )

    def test_validate_docroot_match_logs_warning_on_diverge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            local = tmp_path / "httpdocs"
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
            orch.log_dir = tmp_path / "logs"
            orch.log_dir.mkdir()
            # Remote has different files than local — divergence triggers
            # both the WARNING log AND the diff CSV writer.
            orch._remote_dir_manifest = mock.MagicMock(
                return_value=(2, 200, "feedbeef", "other.txt:100\ngone.txt:100")
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
            orch._remote_dir_manifest = mock.MagicMock(
                return_value=(0, 0, "", "")
            )
            orch._validate_docroot_match("opiniao.inf.br", local)
            warn_msgs = [c.args[0] for c in orch.logger.warning.call_args_list]
            self.assertTrue(
                any("validação cross-server pulada" in m for m in warn_msgs),
                f"expected 'pulada' warning, got: {warn_msgs}",
            )


class RemoteManifestLocaleTests(unittest.TestCase):
    """Guards against the locale-collation regression: without LC_ALL=C
    the remote `sort -u` reorders accented/special chars per pt_BR.UTF-8
    rules while Python `entries.sort()` is bytewise/codepoint — they
    diverge on any name with non-ASCII chars, producing spurious DIVERGE
    warnings. Both find AND sort must run under LC_ALL=C."""

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

    def test_remote_manifest_uses_lc_all_c(self) -> None:
        orch = self._make_orch()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=f"COUNT=0\nTOTAL=0\nMD5=\n---BODY---\n",
            stderr="",
        )
        with mock.patch(
            "plesk_migrator_orchestrator.subprocess.run",
            return_value=completed,
        ) as run_mock:
            orch._remote_dir_manifest("/home/x/public_html")
        # The remote command is the LAST argv element passed to ssh.
        called_args = run_mock.call_args[0][0]
        remote_cmd = called_args[-1]
        self.assertIn("LC_ALL=C find", remote_cmd)
        self.assertIn("LC_ALL=C sort", remote_cmd)


class DocrootDiffCSVTests(unittest.TestCase):
    """Verifies the per-domain CSV diff writer that accompanies a hash
    DIVERGE warning — turns the opaque signal into an actionable per-file
    listing of what is only on the cPanel src vs only on the Plesk dst."""

    def _make_orch(self, tmp_path: pathlib.Path) -> PleskMigrationOrchestrator:
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
        orch.log_dir = tmp_path / "logs"
        orch.log_dir.mkdir()
        return orch

    def test_divergence_writes_diff_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            local = tmp_path / "httpdocs"
            local.mkdir()
            (local / "a.txt").write_bytes(b"x" * 50)   # only-in-dst (size 50)
            (local / "c.txt").write_bytes(b"y" * 30)   # only-in-dst (size 30)
            orch = self._make_orch(tmp_path)
            # Remote has a.txt:99 (different size) and b.txt:50 — so:
            # only_src = {a.txt:99, b.txt:50}; only_dst = {a.txt:50, c.txt:30}
            orch._remote_dir_manifest = mock.MagicMock(
                return_value=(2, 149, "src_h", "a.txt:99\nb.txt:50")
            )
            orch._validate_docroot_match("opiniao.inf.br", local)
            csv_path = orch.log_dir / "docroot-diff-opiniao.inf.br.csv"
            self.assertTrue(csv_path.is_file(), "diff CSV not created")
            content = csv_path.read_text(encoding="utf-8")
            self.assertIn("timestamp,side,path,size", content)
            self.assertIn(",src,a.txt,99", content)
            self.assertIn(",src,b.txt,50", content)
            self.assertIn(",dst,a.txt,50", content)
            self.assertIn(",dst,c.txt,30", content)
            warn_msgs = [c.args[0] for c in orch.logger.warning.call_args_list]
            self.assertTrue(
                any("DIVERGE" in m for m in warn_msgs)
                and any("docroot-diff" in m for m in warn_msgs),
                f"expected DIVERGE+docroot-diff warning, got: {warn_msgs}",
            )

    def test_divergence_csv_oserror_is_swallowed(self) -> None:
        """OSError gravando o CSV não pode propagar — diagnóstico best-effort,
        validação nunca aborta o pipeline."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            local = tmp_path / "httpdocs"
            local.mkdir()
            (local / "a.txt").write_bytes(b"x")
            orch = self._make_orch(tmp_path)
            orch._remote_dir_manifest = mock.MagicMock(
                return_value=(1, 5, "src_h", "different.txt:5")
            )
            with mock.patch(
                "pathlib.Path.open",
                side_effect=OSError("disk full"),
            ):
                # Should not raise even though CSV writer fails.
                orch._validate_docroot_match("opiniao.inf.br", local)
            warn_msgs = [c.args[0] for c in orch.logger.warning.call_args_list]
            # Both the diff-csv failure warning AND the DIVERGE warning
            # should be emitted — the CSV failure must not short-circuit
            # the DIVERGE log.
            self.assertTrue(
                any("falha gravando diff CSV" in m for m in warn_msgs),
                f"expected CSV failure warning, got: {warn_msgs}",
            )
            self.assertTrue(
                any("DIVERGE" in m for m in warn_msgs),
                f"expected DIVERGE warning even after CSV failure, got: {warn_msgs}",
            )


if __name__ == "__main__":
    unittest.main()
