"""Test that fix_docroot passes relative dirname to -www-root, not absolute path.

Bug: prior code passed str(target) where target=/var/www/vhosts/example.com/public_html
(absolute). Plesk documentation says -www-root takes a path "relative to the
subscription root", so Plesk concatenated:
  /var/www/vhosts/example.com + /var/www/vhosts/example.com/public_html
  = /var/www/vhosts/example.com/var/www/vhosts/example.com/public_html
(inexistent, Apache 403 on all requests).

Fix: pass choice (relative dirname, e.g., "public_html") instead.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator


class FixDocrootRelativePathTest(unittest.TestCase):
    def _make_orch(self) -> PleskMigrationOrchestrator:
        """Construct PleskMigrationOrchestrator instance for testing."""
        orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
        orch.dry_run = False
        orch.logger = mock.MagicMock()
        orch.config = {"source": {"host": "cpanel.example.com"}}
        orch.plesk_bin = pathlib.Path("/usr/local/psa/bin/plesk")
        orch.log_dir = pathlib.Path("/tmp/test-logs")
        orch.sessions_dir = pathlib.Path("/tmp/sessions")
        orch.session_name = "test-session"
        return orch

    def test_fix_docroot_passes_relative_dirname(self) -> None:
        """Verify that fix_docroot passes relative dirname to -www-root."""
        orch = self._make_orch()

        # Mock _load_migrated_domains to return one test domain
        with mock.patch.object(
            orch, "_load_migrated_domains", return_value=["example.com"]
        ):
            # Mock vhost.is_dir() to return True
            with mock.patch("pathlib.Path.is_dir", return_value=True):
                # Mock _dir_manifest to return 4-tuples for each candidate.
                # httpdocs: empty (0 files, 0 bytes, empty hash)
                # public_html: has content (5 files, 100 bytes, non-empty hash)
                # www, web: empty
                def mock_dir_manifest(path: pathlib.Path) -> tuple[int, int, str, str]:
                    path_str = str(path)
                    if path_str.endswith("public_html"):
                        return (5, 100, "abc123def456", "file1:10\nfile2:90\n")
                    else:
                        # httpdocs, www, web are empty
                        return (0, 0, "", "")

                with mock.patch.object(orch, "_dir_manifest", side_effect=mock_dir_manifest):
                    # Mock _validate_docroot_match (cross-server validation)
                    with mock.patch.object(orch, "_validate_docroot_match"):
                        # Mock log_dir.mkdir (avoid filesystem side effects)
                        with mock.patch.object(pathlib.Path, "mkdir"):
                            # Mock log_dir.open for report writing
                            mock_report = mock.MagicMock()
                            with mock.patch.object(pathlib.Path, "open", return_value=mock_report):
                                # Capture the _run call to inspect argv
                                captured_argv = None

                                def capture_run(argv, **kwargs):
                                    nonlocal captured_argv
                                    captured_argv = argv

                                with mock.patch.object(orch, "_run", side_effect=capture_run):
                                    # Execute fix_docroot
                                    orch.fix_docroot()

                                    # Verify that _run was called with relative dirname
                                    # not absolute path
                                    self.assertIsNotNone(captured_argv)
                                    self.assertIn("-www-root", captured_argv)
                                    www_root_index = captured_argv.index("-www-root")
                                    www_root_value = captured_argv[www_root_index + 1]

                                    # The value should be "public_html" (relative),
                                    # NOT "/var/www/vhosts/example.com/public_html" (absolute)
                                    self.assertEqual(www_root_value, "public_html")
                                    self.assertFalse(
                                        www_root_value.startswith("/"),
                                        f"www-root value should be relative dirname, not absolute path. "
                                        f"Got: {www_root_value}"
                                    )


if __name__ == "__main__":
    unittest.main()
