"""Cobre extensão de `fix_docroot` para subdomains migrados (domains com
`parentDomainId != 0`).

Sem essa extensão, plesk-migrator deixa subdomains apontando para path
cPanel-style inexistente (ex.: `/var/www/vhosts/opiniao.inf.br/correio.opiniao.inf.br`)
e Apache devolve 403/404 — refletido no Apache N/N failed do test-all.

Cenários:
  * subdomain com docroot escolhido populado → `plesk bin subdomain
    -u <label> -webspace-name <parent> -www-root <relative>` é chamado.
  * canonical já populado e único candidate populado → skip (idempotente).
  * 0 subdomains no DB → noop sem chamadas.
  * `_run_plesk_db` falha → log warning, parent loop não é afetado.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock
from unittest.mock import MagicMock, patch

from plesk_migrator_orchestrator import PleskMigrationOrchestrator


def _make_orch(td: pathlib.Path) -> PleskMigrationOrchestrator:
    orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
    orch.dry_run = False
    orch.logger = mock.MagicMock()
    orch.config = {
        "source": {"host": "cpanel.example.com", "ssh_port": 22,
                   "ssh_password": "secret"},
        "dest": {"host": "plesk.example.com"},
    }
    orch.sensitive_values = []
    orch.plesk_bin = pathlib.Path("/usr/sbin/plesk")
    orch.plesk_migrator_bin = pathlib.Path("/usr/local/psa/admin/sbin/plesk-migrator")
    orch.log_dir = td / "logs"
    orch.log_dir.mkdir(parents=True, exist_ok=True)
    orch.sessions_dir = td / "sessions"
    orch.session_name = "migration-session"
    (orch.sessions_dir / orch.session_name).mkdir(parents=True, exist_ok=True)
    return orch


class LoadMigratedSubdomainsTest(unittest.TestCase):
    """Helper SQL que lista (parent, sub_full_name) a partir do DB Plesk."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = pathlib.Path(self._td.name)
        self.orch = _make_orch(self.tmp)

    def test_returns_parent_sub_pairs_from_db(self) -> None:
        parents = ["opiniao.inf.br", "exemplo.com"]
        # _run_plesk_db tabular output: parent\tsub (one per line)
        fake_out = (
            "opiniao.inf.br\twebmail.opiniao.inf.br\n"
            "opiniao.inf.br\tvivest.opiniao.inf.br\n"
            "exemplo.com\tapp.exemplo.com\n"
        )
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=parents), \
             patch.object(self.orch, "_run_plesk_db",
                          return_value=fake_out) as db_mock:
            pairs = self.orch._load_migrated_subdomains()
        self.assertEqual(pairs, [
            ("opiniao.inf.br", "webmail.opiniao.inf.br"),
            ("opiniao.inf.br", "vivest.opiniao.inf.br"),
            ("exemplo.com", "app.exemplo.com"),
        ])
        # Query must filter by parents IN (...) and parentDomainId != 0
        sql = db_mock.call_args.args[0]
        self.assertIn("parentDomainId", sql)
        self.assertIn("opiniao.inf.br", sql)
        self.assertIn("exemplo.com", sql)

    def test_returns_empty_when_no_parents(self) -> None:
        with patch.object(self.orch, "_load_migrated_domains", return_value=[]), \
             patch.object(self.orch, "_run_plesk_db") as db_mock:
            pairs = self.orch._load_migrated_subdomains()
        self.assertEqual(pairs, [])
        db_mock.assert_not_called()

    def test_returns_empty_on_db_error(self) -> None:
        from plesk_migrator_orchestrator import PhaseExecutionError
        with patch.object(self.orch, "_load_migrated_domains",
                          return_value=["opiniao.inf.br"]), \
             patch.object(self.orch, "_run_plesk_db",
                          side_effect=PhaseExecutionError("db down")):
            pairs = self.orch._load_migrated_subdomains()
        self.assertEqual(pairs, [])


class FixDocrootSubdomainsTest(unittest.TestCase):
    """fix_docroot estendido para subdomains."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = pathlib.Path(self._td.name)
        self.orch = _make_orch(self.tmp)
        # Stub: parents loop é noop nestes tests (testado em outros arquivos).
        self._stub_parent_loop = patch.object(
            self.orch, "_load_migrated_domains", return_value=[]
        )

    def test_applies_plesk_bin_subdomain_with_relative_path(self) -> None:
        """Subdomain com docroot escolhido → comando emitido com path
        RELATIVO (não absoluto — mesmo trap do parent)."""
        captured: list[list[str]] = []

        def fake_run(cmd, **_kw):
            captured.append(list(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        # Subdomain layout fake: conteúdo em <vhost>/webmail/ (label-only,
        # cPanel-style) — canonical <vhost>/webmail.opiniao.inf.br/ vazio.
        # _pick_subdomain_docroot deve apontar www-root para `webmail`.
        def fake_dir_manifest(path):
            s = str(path)
            if s.endswith("/webmail"):
                return (10, 5000, "deadbeef" * 8, "")
            return (0, 0, "", "")

        with self._stub_parent_loop, \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[("opiniao.inf.br",
                                         "webmail.opiniao.inf.br")]), \
             patch.object(self.orch, "_dir_manifest",
                          side_effect=fake_dir_manifest), \
             patch("pathlib.Path.is_dir", return_value=True), \
             patch.object(self.orch, "_run", side_effect=fake_run), \
             patch.object(self.orch, "_validate_docroot_match"):
            self.orch.fix_docroot()

        sub_cmds = [c for c in captured
                    if len(c) >= 2 and "subdomain" in c]
        self.assertTrue(sub_cmds, f"expected `plesk bin subdomain` call, got {captured!r}")
        argv = sub_cmds[0]
        self.assertIn("-u", argv)
        self.assertIn("-webspace-name", argv)
        self.assertEqual(argv[argv.index("-webspace-name") + 1], "opiniao.inf.br")
        # sub-label is "webmail" (first segment of webmail.opiniao.inf.br)
        self.assertEqual(argv[argv.index("-u") + 1], "webmail")
        self.assertIn("-www-root", argv)
        www_root = argv[argv.index("-www-root") + 1]
        self.assertNotIn(
            "/var/www/vhosts", www_root,
            f"-www-root must be RELATIVE (relative to subscription root), got {www_root!r}",
        )
        # Must point to the chosen candidate
        self.assertIn("webmail", www_root)

    def test_skip_when_no_candidates_populated(self) -> None:
        """Todos os candidates vazios → skip subdomain (nenhum comando)."""
        captured: list[list[str]] = []

        def fake_run(cmd, **_kw):
            captured.append(list(cmd))
            return MagicMock(returncode=0)

        with self._stub_parent_loop, \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[("foo.com", "sub.foo.com")]), \
             patch.object(self.orch, "_dir_manifest",
                          return_value=(0, 0, "", "")), \
             patch("pathlib.Path.is_dir", return_value=True), \
             patch.object(self.orch, "_run", side_effect=fake_run), \
             patch.object(self.orch, "_validate_docroot_match"):
            self.orch.fix_docroot()
        sub_cmds = [c for c in captured if len(c) >= 2 and "subdomain" in c]
        self.assertEqual(sub_cmds, [],
                         f"expected no subdomain command, got {sub_cmds!r}")

    def test_noop_when_no_subdomains(self) -> None:
        """0 subdomains → fix_docroot só roda parents loop (que stub aqui = vazio)."""
        with self._stub_parent_loop, \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[]), \
             patch.object(self.orch, "_run") as run_mock, \
             patch.object(self.orch, "_validate_docroot_match"):
            self.orch.fix_docroot()
        self.assertEqual(run_mock.call_count, 0)

    def test_skip_flag_disables_subdomain_loop(self) -> None:
        """Subdomain disable via attribute → loop é pulado mesmo com DB cheio."""
        # Attribute deve ser controlado externamente pelo dispatcher.
        self.orch.skip_fix_docroot_subdomains = True
        with self._stub_parent_loop, \
             patch.object(self.orch, "_load_migrated_subdomains") as load_mock, \
             patch.object(self.orch, "_run") as run_mock:
            self.orch.fix_docroot()
        load_mock.assert_not_called()
        self.assertEqual(run_mock.call_count, 0)

    def test_vhost_root_missing_warns_and_continues(self) -> None:
        """Parent vhost não existe → warn + skip, segue para próximo."""
        captured: list[list[str]] = []

        def fake_run(cmd, **_kw):
            captured.append(list(cmd))
            return MagicMock(returncode=0)

        is_dir_calls = {"count": 0}

        def fake_is_dir(self_path: pathlib.Path) -> bool:
            is_dir_calls["count"] += 1
            # vhost dir for "ghost.com" missing; "real.com" exists
            return "ghost.com" not in str(self_path)

        def fake_manifest(path):
            # Content lives at <vhost>/sub (label-only), not at the full
            # sub.real.com canonical Plesk dir. Forces a www-root rewrite.
            s = str(path)
            if s.endswith("/sub") and "real.com" in s:
                return (5, 100, "abc123" * 6, "")
            return (0, 0, "", "")

        with self._stub_parent_loop, \
             patch.object(self.orch, "_load_migrated_subdomains",
                          return_value=[
                              ("ghost.com", "x.ghost.com"),
                              ("real.com", "sub.real.com"),
                          ]), \
             patch.object(self.orch, "_dir_manifest", side_effect=fake_manifest), \
             patch.object(pathlib.Path, "is_dir", fake_is_dir), \
             patch.object(self.orch, "_run", side_effect=fake_run), \
             patch.object(self.orch, "_validate_docroot_match"):
            self.orch.fix_docroot()

        sub_cmds = [c for c in captured if "subdomain" in c]
        # only sub.real.com should have a command
        self.assertEqual(len(sub_cmds), 1)
        self.assertIn("real.com", " ".join(sub_cmds[0]))


if __name__ == "__main__":
    unittest.main()
