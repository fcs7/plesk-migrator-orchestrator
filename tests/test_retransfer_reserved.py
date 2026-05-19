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


if __name__ == "__main__":
    unittest.main()
