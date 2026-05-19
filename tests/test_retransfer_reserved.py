"""Tests for retransfer_failed reserved-subdomain tolerance.

Three new helpers exposed on PleskMigrationOrchestrator:
  _parse_reserved_subdomain_failures(session_dir) -> set[str]
  _domain_exists_in_plesk(domain) -> bool
  _subscription_only_reserved_failures(domain, session_dir) -> bool

And new branching inside retransfer_failed: stagnation + max_attempts paths
classify each failing domain as recoverable (subscription exists + only
reserved-subdomain errors) or unrecoverable (everything else); only the
unrecoverable set raises PhaseExecutionError."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator


class ParseReservedSubdomainFailuresTests(unittest.TestCase):
    def test_extracts_webmail_label_from_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            report = session / "accounts_report_tree.2026.05.19.14.27.47"
            report.write_text(
                "Detailed Migration Status\n"
                "error: The following sites of subscription were not created"
                " - they do not exist on target panel: 'webmail.opiniao.inf.br'\n"
            )
            labels = PleskMigrationOrchestrator._parse_reserved_subdomain_failures(session)
            self.assertEqual(labels, {"webmail"})

    def test_extracts_multiple_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            (session / "accounts_report_tree.2026.05.19.14.27.47").write_text(
                "sites of subscription were not created - they do not exist"
                " on target panel: 'webmail.a.com'\n"
                "sites of subscription were not created - they do not exist"
                " on target panel: 'mail.b.com'\n"
            )
            labels = PleskMigrationOrchestrator._parse_reserved_subdomain_failures(session)
            self.assertEqual(labels, {"webmail", "mail"})

    def test_no_matches_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            (session / "accounts_report_tree.2026.05.19.14.27.47").write_text(
                "Detailed Migration Status\nOperation finished successfully\n"
            )
            labels = PleskMigrationOrchestrator._parse_reserved_subdomain_failures(session)
            self.assertEqual(labels, set())

    def test_uses_latest_report_when_multiple_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            old = session / "accounts_report_tree.2026.05.19.14.06.20"
            new = session / "accounts_report_tree.2026.05.19.14.27.47"
            old.write_text("sites of subscription were not created"
                           " - they do not exist on target panel: 'mail.old.com'\n")
            new.write_text("sites of subscription were not created"
                           " - they do not exist on target panel: 'webmail.new.com'\n")
            labels = PleskMigrationOrchestrator._parse_reserved_subdomain_failures(session)
            self.assertEqual(labels, {"webmail"})

    def test_missing_session_dir_returns_empty(self) -> None:
        nonexistent = pathlib.Path("/nonexistent/session/xyz")
        labels = PleskMigrationOrchestrator._parse_reserved_subdomain_failures(nonexistent)
        self.assertEqual(labels, set())

    def test_session_without_report_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            labels = PleskMigrationOrchestrator._parse_reserved_subdomain_failures(
                pathlib.Path(tmp)
            )
            self.assertEqual(labels, set())


class DomainExistsInPleskTests(unittest.TestCase):
    def _make_orch(self) -> PleskMigrationOrchestrator:
        # Build minimal orchestrator via __new__ to bypass __init__ — we only
        # need the methods that touch dry_run, logger, and _run_plesk_db.
        orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
        orch.dry_run = False
        orch.logger = mock.MagicMock()
        orch.plesk_bin = pathlib.Path("/usr/sbin/plesk")
        return orch

    def test_returns_true_when_count_is_one(self) -> None:
        orch = self._make_orch()
        orch._run_plesk_db = mock.MagicMock(return_value="1\n")
        self.assertTrue(orch._domain_exists_in_plesk("opiniao.inf.br"))
        orch._run_plesk_db.assert_called_once()
        sql = orch._run_plesk_db.call_args[0][0]
        self.assertIn("opiniao.inf.br", sql)
        self.assertIn("SELECT COUNT", sql)

    def test_returns_false_when_count_is_zero(self) -> None:
        orch = self._make_orch()
        orch._run_plesk_db = mock.MagicMock(return_value="0\n")
        self.assertFalse(orch._domain_exists_in_plesk("missing.com"))

    def test_dry_run_returns_true_without_calling_db(self) -> None:
        orch = self._make_orch()
        orch.dry_run = True
        orch._run_plesk_db = mock.MagicMock()
        self.assertTrue(orch._domain_exists_in_plesk("anything.com"))
        orch._run_plesk_db.assert_not_called()

    def test_db_error_returns_false(self) -> None:
        from plesk_migrator_orchestrator import PhaseExecutionError
        orch = self._make_orch()
        orch._run_plesk_db = mock.MagicMock(
            side_effect=PhaseExecutionError("db down")
        )
        self.assertFalse(orch._domain_exists_in_plesk("opiniao.inf.br"))

    def test_escapes_single_quote_in_domain(self) -> None:
        orch = self._make_orch()
        orch._run_plesk_db = mock.MagicMock(return_value="0\n")
        orch._domain_exists_in_plesk("a'b.com")
        sql = orch._run_plesk_db.call_args[0][0]
        # Single quote must be escaped via _sql_escape (\').
        self.assertNotIn("'a'b.com'", sql)
        self.assertIn("a\\'b.com", sql)


class SubscriptionOnlyReservedFailuresTests(unittest.TestCase):
    def _make_orch(self) -> PleskMigrationOrchestrator:
        orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
        orch.dry_run = False
        orch.logger = mock.MagicMock()
        orch.plesk_bin = pathlib.Path("/usr/sbin/plesk")
        return orch

    def test_true_when_only_reserved_labels_and_domain_exists(self) -> None:
        orch = self._make_orch()
        orch._domain_exists_in_plesk = mock.MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            (session / "accounts_report_tree.2026.05.19.14.27.47").write_text(
                "sites of subscription were not created - they do not exist"
                " on target panel: 'webmail.opiniao.inf.br'\n"
            )
            self.assertTrue(
                orch._subscription_only_reserved_failures(
                    "opiniao.inf.br", session
                )
            )

    def test_false_when_label_not_reserved(self) -> None:
        orch = self._make_orch()
        orch._domain_exists_in_plesk = mock.MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            (session / "accounts_report_tree.2026.05.19.14.27.47").write_text(
                "sites of subscription were not created - they do not exist"
                " on target panel: 'random-subdomain.opiniao.inf.br'\n"
            )
            self.assertFalse(
                orch._subscription_only_reserved_failures(
                    "opiniao.inf.br", session
                )
            )

    def test_false_when_domain_missing(self) -> None:
        orch = self._make_orch()
        orch._domain_exists_in_plesk = mock.MagicMock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            (session / "accounts_report_tree.2026.05.19.14.27.47").write_text(
                "sites of subscription were not created - they do not exist"
                " on target panel: 'webmail.opiniao.inf.br'\n"
            )
            self.assertFalse(
                orch._subscription_only_reserved_failures(
                    "opiniao.inf.br", session
                )
            )

    def test_false_when_no_failures_parsed(self) -> None:
        orch = self._make_orch()
        orch._domain_exists_in_plesk = mock.MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            (session / "accounts_report_tree.2026.05.19.14.27.47").write_text(
                "Detailed Migration Status\n"
            )
            self.assertFalse(
                orch._subscription_only_reserved_failures(
                    "opiniao.inf.br", session
                )
            )

    def test_true_when_mixed_reserved_labels(self) -> None:
        orch = self._make_orch()
        orch._domain_exists_in_plesk = mock.MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp)
            (session / "accounts_report_tree.2026.05.19.14.27.47").write_text(
                "sites of subscription were not created - they do not exist"
                " on target panel: 'webmail.x.com'\n"
                "sites of subscription were not created - they do not exist"
                " on target panel: 'mail.x.com'\n"
            )
            self.assertTrue(
                orch._subscription_only_reserved_failures("x.com", session)
            )


if __name__ == "__main__":
    unittest.main()
