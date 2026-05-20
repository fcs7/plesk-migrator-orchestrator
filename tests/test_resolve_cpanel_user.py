"""Verifica que `_resolve_cpanel_user` consulta /etc/userdomains via SSH
e cacheia por sessão. Degrada para None em falha de SSH ou linha ausente.
Integração com _validate_docroot_match: prefere user resolvido, cai para
heurística first-label se lookup falhar."""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator


class ResolveCpanelUserTest(unittest.TestCase):
    def _make_orch(self) -> PleskMigrationOrchestrator:
        orch = PleskMigrationOrchestrator.__new__(PleskMigrationOrchestrator)
        orch.dry_run = False
        orch.logger = mock.MagicMock()
        orch.config = {
            "source": {
                "host": "cpanel.example.com",
                "ssh_port": 2222,
                "ssh_password": "secret",
            },
        }
        return orch

    def test_resolves_user_from_userdomains(self) -> None:
        """_resolve_cpanel_user invoca sshpass com argv correto e retorna
        username de stdout."""
        orch = self._make_orch()
        with mock.patch("plesk_migrator_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="opiniaoi\n"
            )
            result = orch._resolve_cpanel_user("opiniao.inf.br")
            self.assertEqual(result, "opiniaoi")
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            argv = call_args[0][0]
            self.assertEqual(argv[0], "sshpass")
            self.assertEqual(argv[1], "-e")
            self.assertIn("ssh", argv)
            self.assertIn("root@cpanel.example.com", argv)

    def test_cache_avoids_second_ssh_call(self) -> None:
        """Chamada dupla de _resolve_cpanel_user reutiliza cache, invocando
        subprocess.run uma só vez."""
        orch = self._make_orch()
        with mock.patch("plesk_migrator_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="opiniaoi\n"
            )
            result1 = orch._resolve_cpanel_user("opiniao.inf.br")
            result2 = orch._resolve_cpanel_user("opiniao.inf.br")
            self.assertEqual(result1, "opiniaoi")
            self.assertEqual(result2, "opiniaoi")
            self.assertEqual(mock_run.call_count, 1)

    def test_returns_none_on_empty_grep(self) -> None:
        """Grep retorna linha vazia → _resolve_cpanel_user retorna None
        e cacheia (evita retry)."""
        orch = self._make_orch()
        with mock.patch("plesk_migrator_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="\n"
            )
            result = orch._resolve_cpanel_user("missing.example.com")
            self.assertIsNone(result)
            cached = orch._cpanel_user_cache.get("missing.example.com")
            self.assertIsNone(cached)

    def test_returns_none_on_ssh_failure(self) -> None:
        """SSH rc != 0 → _resolve_cpanel_user retorna None."""
        orch = self._make_orch()
        with mock.patch("plesk_migrator_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=255, stdout=""
            )
            result = orch._resolve_cpanel_user("opiniao.inf.br")
            self.assertIsNone(result)

    def test_returns_none_on_oserror(self) -> None:
        """OSError em subprocess.run (sshpass ausente, etc.) → None."""
        orch = self._make_orch()
        with mock.patch("plesk_migrator_orchestrator.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("sshpass not found")
            result = orch._resolve_cpanel_user("opiniao.inf.br")
            self.assertIsNone(result)

    def test_validate_uses_resolved_user_when_available(self) -> None:
        """_validate_docroot_match chama _remote_dir_manifest com path
        contendo user resolvido (opiniaoi) quando _resolve_cpanel_user
        retorna 'opiniaoi'."""
        orch = self._make_orch()
        orch._cpanel_user_cache = {"opiniao.inf.br": "opiniaoi"}

        with mock.patch.object(orch, "_remote_dir_manifest") as mock_remote:
            mock_remote.return_value = (0, 0, "", "")
            local_path = pathlib.Path("/var/www/vhosts/opiniao.inf.br/public_html")
            orch._validate_docroot_match("opiniao.inf.br", local_path)
            mock_remote.assert_called_once_with("/home/opiniaoi/public_html")

    def test_validate_falls_back_to_heuristic(self) -> None:
        """Quando _resolve_cpanel_user retorna None, _validate_docroot_match
        usa heurística first-label (opiniao) em _remote_dir_manifest."""
        orch = self._make_orch()
        orch._cpanel_user_cache = {"opiniao.inf.br": None}

        with mock.patch.object(orch, "_remote_dir_manifest") as mock_remote:
            mock_remote.return_value = (0, 0, "", "")
            local_path = pathlib.Path("/var/www/vhosts/opiniao.inf.br/public_html")
            orch._validate_docroot_match("opiniao.inf.br", local_path)
            mock_remote.assert_called_once_with("/home/opiniao/public_html")


if __name__ == "__main__":
    unittest.main()
