# fix-docroot relative path + fix-ftp-passwords Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Corrigir bug crítico em `fix_docroot` que produz docroots inexistentes (Apache 403/404) ao passar caminho absoluto a `plesk bin subscription -www-root` (que espera relativo), tornar `_validate_docroot_match` capaz de detectar esse bug ao resolver o user cPanel real via `/etc/userdomains`, e adicionar fase `fix-ftp-passwords` (mirror de `check-mail-passwords`) para resetar hashes cPanel incompatíveis em sub-FTP users.

**Architecture:** Edits cirúrgicos em `plesk_migrator_orchestrator.py` em 5 pontos: (1) `fix_docroot` argv ao `plesk bin subscription`; (2) novo helper `_resolve_cpanel_user` + chamada em `_validate_docroot_match`; (3) nova fase `fix_ftp_passwords` espelhando `check_mail_passwords`; (4) wiring CLI/PHASES_ORDER/_validate_config; (5) CLAUDE.md gotchas. Padrão: 3 testes novos em `tests/`, suite verde antes de commit. Deploy via `git push` + `git pull` no Plesk dest (191.7.26.24:2222) + comandos de recuperação manual.

**Tech Stack:** Python 3.8+ (`from __future__ import annotations`, PEP 604, `collections.abc`), `unittest` (sem pytest no dev local), `subprocess` + `sshpass -e` para SSH cross-server, `plesk db -Nse` para SQL Plesk, `plesk bin subscription/mail/ftpuser` CLI.

---

## File Structure

| Arquivo | Responsabilidade | Mudanças |
|---------|------------------|----------|
| `plesk_migrator_orchestrator.py` | Orquestrador completo | 5 patches (bug docroot, helper SSH user lookup, nova fase FTP, wiring, validation list) |
| `tests/test_fix_docroot_relative_path.py` | TDD bug docroot | NOVO — assert argv contém path relativo |
| `tests/test_resolve_cpanel_user.py` | TDD helper user lookup | NOVO — mock SSH `/etc/userdomains` |
| `tests/test_fix_ftp_passwords.py` | TDD nova fase FTP | NOVO — audit + reset paths |
| `CLAUDE.md` | Docs projeto | já atualizado nesta sessão (3 gotchas adicionadas — apenas cross-ref final) |

---

## Task 1: Bug crítico — `fix_docroot` passa path absoluto

**Files:**
- Modify: `plesk_migrator_orchestrator.py:2148-2156`
- Test: `tests/test_fix_docroot_relative_path.py` (NOVO)

**Why:** `plesk bin subscription --help` documenta `-www-root <path> (relative to the subscription root)`. Código atual passa `str(target)` que é absoluto (ex.: `/var/www/vhosts/dom/public_html`); Plesk concatena → `/var/www/vhosts/dom/var/www/vhosts/dom/public_html` (inexistente, Apache 403). Já reproduzido em produção: `plesk db -Nse "SELECT name,www_root FROM domains JOIN hosting USING(id-equivalent)"` retornou path duplicado para `opiniao.inf.br`.

- [ ] **Step 1: Write failing test**

Criar `tests/test_fix_docroot_relative_path.py`:

```python
"""Regression: `plesk bin subscription -www-root` espera path RELATIVO ao
vhost root. Passar absoluto produz `/var/www/vhosts/dom/var/www/vhosts/dom/x`
(Plesk concatena). Este teste fixa o contrato: o arg deve ser o nome do
diretório (`public_html`/`httpdocs`/etc), NÃO o path completo `vhost/choice`."""

from __future__ import annotations

import pathlib
import unittest
from unittest.mock import MagicMock, patch

from plesk_migrator_orchestrator import PleskMigratorOrchestrator


class FixDocrootRelativePathTest(unittest.TestCase):
    def _make_orchestrator(self, tmp: pathlib.Path) -> PleskMigratorOrchestrator:
        config_path = tmp / "config.yaml"
        config_path.write_text(
            "source:\n  host: 1.2.3.4\n  ssh_password: x\n"
            "dest:\n  host: 5.6.7.8\n"
            "migration:\n  allowlist: []\n  denylist: []\n",
            encoding="utf-8",
        )
        orch = PleskMigratorOrchestrator(
            config_path=str(config_path),
            dry_run=False, skip_install=True, verbose=False,
        )
        orch.plesk_bin = pathlib.Path("/usr/sbin/plesk")
        orch.log_dir = tmp / "logs"
        orch.log_dir.mkdir(parents=True, exist_ok=True)
        return orch

    def test_subscription_www_root_arg_is_relative_dirname(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            orch = self._make_orchestrator(tmp)

            vhost = tmp / "vhosts" / "example.com"
            (vhost / "public_html").mkdir(parents=True)
            (vhost / "public_html" / "index.php").write_text("ok")

            captured: list[list[str]] = []

            def fake_run(cmd, **_kwargs):
                captured.append(list(cmd))
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch.object(orch, "_load_migrated_domains",
                              return_value=["example.com"]), \
                 patch.object(orch, "_vhost_root_for",
                              return_value=vhost), \
                 patch.object(orch, "_run", side_effect=fake_run), \
                 patch.object(orch, "_validate_docroot_match"):
                orch.fix_docroot()

            sub_cmds = [c for c in captured
                        if len(c) >= 2 and c[1] == "bin"
                        and "subscription" in c]
            self.assertTrue(sub_cmds, "expected one `plesk bin subscription` call")
            argv = sub_cmds[0]
            self.assertIn("-www-root", argv)
            www_root_value = argv[argv.index("-www-root") + 1]
            self.assertEqual(
                www_root_value, "public_html",
                f"expected relative dirname 'public_html', got {www_root_value!r}",
            )
            self.assertNotIn(
                "/", www_root_value,
                f"-www-root deve ser relativo (sem '/'), got {www_root_value!r}",
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Verify it fails**

Run: `cd /home/fcs/Documents/ntweb/opiniao && python3 -m unittest tests.test_fix_docroot_relative_path -v`
Expected: FAIL — `AssertionError: expected relative dirname 'public_html', got '/tmp/.../vhosts/example.com/public_html'`

(Se falhar por `_vhost_root_for` ausente: o teste assume esse helper. Se não existir, substituir por `patch.object(orch, "_vhost_root", return_value=vhost)` ou monkey-patch direto de `pathlib.Path` — inspecionar linha 2103-2109 para descobrir como o vhost é resolvido. Atualmente:)

```python
# linha 2103-2109 atual:
for domain in domains:
    vhost = self._vhost_root() / domain
    if not vhost.is_dir():
        ...
```

Se `_vhost_root` for método sem args, ajustar mock:
`patch.object(orch, "_vhost_root", return_value=tmp / "vhosts")`.

- [ ] **Step 3: Apply fix**

Edit `plesk_migrator_orchestrator.py` linha 2148-2156:

```python
            if self.dry_run:
                self.logger.info(
                    "[DRY-RUN] %s bin subscription -u %s -www-root %s (abs=%s)",
                    self.plesk_bin, domain, choice, target,
                )
                continue

            self._run(
                [str(self.plesk_bin), "bin", "subscription",
                 "-u", domain, "-www-root", choice],
                timeout=TIMEOUT_FIX_DOCROOT,
                log_to=report,
            )
```

(Trocar `str(target)` → `choice`; log dry-run mostra ambos para auditoria.)

- [ ] **Step 4: Verify it passes**

Run: `python3 -m unittest tests.test_fix_docroot_relative_path -v`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS (~47 tests; 1 novo)

- [ ] **Step 6: Commit**

```bash
git add tests/test_fix_docroot_relative_path.py plesk_migrator_orchestrator.py
git commit -m "fix(fix-docroot): pass relative dirname to -www-root (was absolute, Plesk concatenated → docroot inexistente → Apache 403)"
```

---

## Task 2: `_resolve_cpanel_user` helper via `/etc/userdomains` + integração

**Files:**
- Modify: `plesk_migrator_orchestrator.py:1836-1885` (`_validate_docroot_match`)
- Add: novo método `_resolve_cpanel_user` próximo a `_remote_dir_manifest` (~linha 1913)
- Test: `tests/test_resolve_cpanel_user.py` (NOVO)

**Why:** Heurística atual `cpanel_user = domain.split(".", 1)[0]` quebra em renames cPanel-style (`opiniao` → `opiniaoi`). Quando heurística falha, `_remote_dir_manifest` retorna vazio → SSH validation "pulada" → o bug do path absoluto da Task 1 nunca seria detectado. `/etc/userdomains` é mapping canônico WHM `<domain>: <user>` (1:1).

- [ ] **Step 1: Write failing test**

Criar `tests/test_resolve_cpanel_user.py`:

```python
"""Verifica que `_resolve_cpanel_user` consulta /etc/userdomains via SSH
e cacheia por sessão. Degrada para None em falha de SSH ou linha ausente."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from plesk_migrator_orchestrator import PleskMigratorOrchestrator


class ResolveCpanelUserTest(unittest.TestCase):
    def _make_orchestrator(self) -> PleskMigratorOrchestrator:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        config_path = pathlib.Path(td.name) / "config.yaml"
        config_path.write_text(
            "source:\n  host: 1.2.3.4\n  ssh_password: pwd\n  ssh_port: 2222\n"
            "dest:\n  host: 5.6.7.8\n"
            "migration:\n  allowlist: []\n  denylist: []\n",
            encoding="utf-8",
        )
        return PleskMigratorOrchestrator(
            config_path=str(config_path),
            dry_run=False, skip_install=True, verbose=False,
        )

    def test_resolves_user_from_userdomains(self) -> None:
        orch = self._make_orchestrator()
        proc = MagicMock(returncode=0, stdout="opiniaoi\n", stderr="")
        with patch("subprocess.run", return_value=proc) as mock_run:
            user = orch._resolve_cpanel_user("opiniao.inf.br")
        self.assertEqual(user, "opiniaoi")
        argv = mock_run.call_args.args[0]
        self.assertIn("sshpass", argv)
        self.assertIn("-e", argv)
        self.assertTrue(
            any("userdomains" in part for part in argv),
            f"expected /etc/userdomains in remote cmd, got {argv!r}",
        )

    def test_cache_avoids_second_ssh_call(self) -> None:
        orch = self._make_orchestrator()
        proc = MagicMock(returncode=0, stdout="opiniaoi\n", stderr="")
        with patch("subprocess.run", return_value=proc) as mock_run:
            user1 = orch._resolve_cpanel_user("opiniao.inf.br")
            user2 = orch._resolve_cpanel_user("opiniao.inf.br")
        self.assertEqual(user1, "opiniaoi")
        self.assertEqual(user2, "opiniaoi")
        self.assertEqual(
            mock_run.call_count, 1,
            "second call should hit cache, not SSH",
        )

    def test_returns_none_on_empty_grep(self) -> None:
        orch = self._make_orchestrator()
        proc = MagicMock(returncode=0, stdout="\n", stderr="")
        with patch("subprocess.run", return_value=proc):
            user = orch._resolve_cpanel_user("desconhecido.com")
        self.assertIsNone(user)

    def test_returns_none_on_ssh_failure(self) -> None:
        orch = self._make_orchestrator()
        proc = MagicMock(returncode=255, stdout="", stderr="Permission denied")
        with patch("subprocess.run", return_value=proc):
            user = orch._resolve_cpanel_user("opiniao.inf.br")
        self.assertIsNone(user)

    def test_returns_none_on_oserror(self) -> None:
        orch = self._make_orchestrator()
        with patch("subprocess.run", side_effect=OSError("sshpass missing")):
            user = orch._resolve_cpanel_user("opiniao.inf.br")
        self.assertIsNone(user)

    def test_validate_uses_resolved_user_when_available(self) -> None:
        """Integração: _validate_docroot_match deve preferir user resolvido."""
        orch = self._make_orchestrator()
        captured_paths: list[str] = []

        def fake_remote_manifest(remote_path: str):
            captured_paths.append(remote_path)
            return (0, 0, "", "")  # vazio → 'pulada'

        with patch.object(orch, "_resolve_cpanel_user", return_value="opiniaoi"), \
             patch.object(orch, "_remote_dir_manifest",
                          side_effect=fake_remote_manifest):
            orch._validate_docroot_match(
                "opiniao.inf.br", pathlib.Path("/var/www/vhosts/opiniao.inf.br/public_html"),
            )
        self.assertEqual(
            captured_paths, ["/home/opiniaoi/public_html"],
            "should use resolved user, not heuristic 'opiniao'",
        )

    def test_validate_falls_back_to_heuristic(self) -> None:
        """Integração: lookup falha → degrada para heurística first-label."""
        orch = self._make_orchestrator()
        captured_paths: list[str] = []

        def fake_remote_manifest(remote_path: str):
            captured_paths.append(remote_path)
            return (0, 0, "", "")

        with patch.object(orch, "_resolve_cpanel_user", return_value=None), \
             patch.object(orch, "_remote_dir_manifest",
                          side_effect=fake_remote_manifest):
            orch._validate_docroot_match(
                "opiniao.inf.br", pathlib.Path("/var/www/vhosts/opiniao.inf.br/public_html"),
            )
        self.assertEqual(captured_paths, ["/home/opiniao/public_html"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Verify it fails**

Run: `python3 -m unittest tests.test_resolve_cpanel_user -v`
Expected: FAIL — `AttributeError: 'PleskMigratorOrchestrator' object has no attribute '_resolve_cpanel_user'`

- [ ] **Step 3: Add helper + init cache**

Inserir após `__init__` (procurar `def __init__` e adicionar a inicialização do cache; ou inicializar lazy no helper):

Adicionar método novo logo após `_remote_dir_manifest` (após linha 2027, antes de `_pick_docroot`):

```python
    def _resolve_cpanel_user(self, domain: str) -> str | None:
        """Resolve real cPanel username for `domain` via WHM canonical
        mapping `/etc/userdomains` (one line per domain: `<dom>: <user>`).

        Returns the username (stripped) or None on:
          - SSH failure (sshpass missing, host unreachable, rc != 0)
          - missing/empty grep result (domain not in /etc/userdomains)
          - dry_run mode (avoids networking in tests)

        Results cached per session in `self._cpanel_user_cache` keyed by
        domain. Empty result is also cached (`None`) to avoid retrying a
        failed lookup mid-pipeline.

        Caller should `or domain.split('.', 1)[0]` to fall back to the
        legacy first-label heuristic on None (backward compatibility)."""
        if self.dry_run:
            return None
        cache = getattr(self, "_cpanel_user_cache", None)
        if cache is None:
            cache = {}
            self._cpanel_user_cache = cache
        if domain in cache:
            return cache[domain]
        src = self.config.get("source") or {}
        host = src.get("host")
        password = src.get("ssh_password")
        port = int(src.get("ssh_port", 22))
        if not host or not password:
            cache[domain] = None
            return None
        remote_cmd = (
            f"grep -E '^{shlex.quote(domain)}:[[:space:]]' "
            f"/etc/userdomains | awk '{{print $2}}'"
        )
        argv = [
            "sshpass", "-e",
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=15",
            "-o", "BatchMode=no",
            "-p", str(port),
            f"root@{host}",
            remote_cmd,
        ]
        env = {**os.environ, "SSHPASS": str(password)}
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=30, check=False, env=env,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
            self.logger.debug(
                "_resolve_cpanel_user: ssh falhou (%s): %s", domain, exc,
            )
            cache[domain] = None
            return None
        if proc.returncode != 0:
            self.logger.debug(
                "_resolve_cpanel_user: rc=%d para %s", proc.returncode, domain,
            )
            cache[domain] = None
            return None
        user = proc.stdout.strip()
        cache[domain] = user if user else None
        return cache[domain]
```

Nota: `shlex` já está importado no topo do arquivo (usado em `_remote_dir_manifest`). Confirmar com `grep -n "^import shlex\|^from shlex" plesk_migrator_orchestrator.py`. Se ausente, adicionar `import shlex` no bloco de imports.

- [ ] **Step 4: Integrate into `_validate_docroot_match`**

Edit `plesk_migrator_orchestrator.py` linha 1846 (dentro de `_validate_docroot_match`):

```python
        cpanel_user = (
            self._resolve_cpanel_user(domain)
            or domain.split(".", 1)[0]
        )
        # /etc/userdomains is the canonical WHM mapping. Heuristic
        # first-label remains as fallback for hosts without /etc/userdomains
        # access (rare) or domains genuinely matching the heuristic.
```

(Substituir linhas atuais 1846-1848 contendo `cpanel_user = domain.split(".", 1)[0]` + comentário.)

- [ ] **Step 5: Verify test passes**

Run: `python3 -m unittest tests.test_resolve_cpanel_user -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Full suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — verificar que `tests.test_fix_docroot_validation` continua verde (sem regressão na assinatura de `_validate_docroot_match`).

- [ ] **Step 7: Commit**

```bash
git add tests/test_resolve_cpanel_user.py plesk_migrator_orchestrator.py
git commit -m "feat(_validate_docroot_match): resolve real cPanel user via /etc/userdomains (canonical WHM mapping, cached per session, falls back to first-label heuristic)"
```

---

## Task 3: Nova fase `fix_ftp_passwords` — método + helper SQL

**Files:**
- Modify: `plesk_migrator_orchestrator.py` (adicionar método após `check_mail_passwords` ~linha 2425)
- Test: `tests/test_fix_ftp_passwords.py` (NOVO)

**Why:** Sub-FTP users criados pelo plesk-migrator com hashes cPanel (crypt MD5) batem `530 Login incorrect` em Plesk Linux (espera SHA-512). Confirmado em produção: 5 users (strauss/poupex/fhepoupex/sabin/ntweb-migracao `_opiniao.inf.br`) falhando. Espelha `check_mail_passwords` — audit default, reset opt-in com CSV chmod 600.

- [ ] **Step 1: Write failing test**

Criar `tests/test_fix_ftp_passwords.py`:

```python
"""Cobre `fix_ftp_passwords`:
  - audit-only (default) enumera FTP users via SQL + escreve status CSV
  - --reset-ftp-passwords gera senha urlsafe(16), chama `plesk bin ftpuser
    -passwd`, grava ftp-password-reset.csv chmod 600, popula sensitive_values
  - 0 users → noop, sem CSV
  - falha em `plesk bin ftpuser` (rc != 0) → log error + continua
"""

from __future__ import annotations

import os
import pathlib
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from plesk_migrator_orchestrator import PleskMigratorOrchestrator


class FixFtpPasswordsTest(unittest.TestCase):
    def _make_orchestrator(self, log_dir: pathlib.Path) -> PleskMigratorOrchestrator:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        config_path = pathlib.Path(td.name) / "config.yaml"
        config_path.write_text(
            "source:\n  host: 1.2.3.4\n  ssh_password: x\n"
            "dest:\n  host: 5.6.7.8\n"
            "migration:\n  allowlist: []\n  denylist: []\n",
            encoding="utf-8",
        )
        orch = PleskMigratorOrchestrator(
            config_path=str(config_path),
            dry_run=False, skip_install=True, verbose=False,
        )
        orch.plesk_bin = pathlib.Path("/usr/sbin/plesk")
        orch.log_dir = log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        return orch

    def test_audit_writes_status_csv_no_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_dir = pathlib.Path(td)
            orch = self._make_orchestrator(log_dir)
            stdout = (
                "strauss_opiniao.inf.br\topiniao.inf.br\n"
                "sabin_opiniao.inf.br\topiniao.inf.br\n"
            )
            proc = MagicMock(returncode=0, stdout=stdout, stderr="")
            with patch.object(orch, "_load_migrated_domains",
                              return_value=["opiniao.inf.br"]), \
                 patch("subprocess.run", return_value=proc) as mock_run:
                orch.fix_ftp_passwords(reset=False)
            status_csv = log_dir / "ftp-password-status.csv"
            self.assertTrue(status_csv.exists(), "audit must write status CSV")
            content = status_csv.read_text(encoding="utf-8")
            self.assertIn("strauss_opiniao.inf.br", content)
            self.assertIn("sabin_opiniao.inf.br", content)
            self.assertNotIn(
                "ftpuser", str(mock_run.call_args_list),
                "audit must NOT invoke `plesk bin ftpuser`",
            )

    def test_reset_invokes_ftpuser_passwd_and_writes_chmod_600_csv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_dir = pathlib.Path(td)
            orch = self._make_orchestrator(log_dir)
            stdout = "strauss_opiniao.inf.br\topiniao.inf.br\n"
            sql_proc = MagicMock(returncode=0, stdout=stdout, stderr="")
            with patch.object(orch, "_load_migrated_domains",
                              return_value=["opiniao.inf.br"]), \
                 patch("subprocess.run", return_value=sql_proc), \
                 patch.object(orch, "_run",
                              return_value=MagicMock(returncode=0)) as mock_inner:
                orch.fix_ftp_passwords(reset=True)
            reset_csv = log_dir / "ftp-password-reset.csv"
            self.assertTrue(reset_csv.exists())
            mode = stat.S_IMODE(os.stat(reset_csv).st_mode)
            self.assertEqual(mode, 0o600, f"CSV perms = {oct(mode)}, want 0o600")
            content = reset_csv.read_text(encoding="utf-8")
            self.assertIn("strauss_opiniao.inf.br", content)
            self.assertIn("timestamp,login,domain,new_password", content)
            ran_argv = [args[0] for (args, _kw) in mock_inner.call_args_list]
            flat = [tuple(a) for a in ran_argv]
            self.assertTrue(
                any("ftpuser" in a and "-passwd" in a for a in flat),
                f"expected `plesk bin ftpuser ... -passwd ...`, got {flat!r}",
            )
            self.assertTrue(orch.sensitive_values, "new password must be masked in logs")

    def test_no_users_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_dir = pathlib.Path(td)
            orch = self._make_orchestrator(log_dir)
            proc = MagicMock(returncode=0, stdout="", stderr="")
            with patch.object(orch, "_load_migrated_domains",
                              return_value=["opiniao.inf.br"]), \
                 patch("subprocess.run", return_value=proc):
                orch.fix_ftp_passwords(reset=True)
            self.assertFalse((log_dir / "ftp-password-reset.csv").exists())
            self.assertFalse((log_dir / "ftp-password-status.csv").exists())

    def test_ftpuser_failure_logs_continues(self) -> None:
        from plesk_migrator_orchestrator import PhaseExecutionError
        with tempfile.TemporaryDirectory() as td:
            log_dir = pathlib.Path(td)
            orch = self._make_orchestrator(log_dir)
            stdout = (
                "strauss_opiniao.inf.br\topiniao.inf.br\n"
                "poupex_opiniao.inf.br\topiniao.inf.br\n"
            )
            sql_proc = MagicMock(returncode=0, stdout=stdout, stderr="")

            def fail_first_succeed_rest(cmd, **_kwargs):
                if "strauss_opiniao.inf.br" in cmd:
                    raise PhaseExecutionError("strauss broken")
                return MagicMock(returncode=0)

            with patch.object(orch, "_load_migrated_domains",
                              return_value=["opiniao.inf.br"]), \
                 patch("subprocess.run", return_value=sql_proc), \
                 patch.object(orch, "_run", side_effect=fail_first_succeed_rest):
                orch.fix_ftp_passwords(reset=True)
            content = (log_dir / "ftp-password-reset.csv").read_text(encoding="utf-8")
            self.assertIn("poupex_opiniao.inf.br", content)
            self.assertNotIn("strauss_opiniao.inf.br", content)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Verify it fails**

Run: `python3 -m unittest tests.test_fix_ftp_passwords -v`
Expected: FAIL — `AttributeError: 'PleskMigratorOrchestrator' object has no attribute 'fix_ftp_passwords'`

- [ ] **Step 3: Add `TIMEOUT_FIX_FTP_PASSWORDS` const**

Adicionar próximo aos outros timeouts (procurar `TIMEOUT_CHECK_MAIL_PASSWORDS`):

```python
TIMEOUT_FIX_FTP_PASSWORDS = 300
```

- [ ] **Step 4: Add `fix_ftp_passwords` method**

Inserir após `check_mail_passwords` (após linha 2424, antes de `def fix_mail_quota`):

```python
    def fix_ftp_passwords(self, *, reset: bool = False) -> None:
        """Enumera FTP users (incluindo sub-users `user_dom`) vinculados a
        domínios migrados. Hashes cPanel (crypt MD5) não validam em Plesk
        Linux (SHA-512) → `530 Login incorrect` mesmo com path/perm OK.

        Audit (default): grava <log_dir>/ftp-password-status.csv listando
        login, domínio, has_password (não tenta login real — impossível
        sem o cleartext).

        reset=True: para cada login, gera `secrets.token_urlsafe(16)`,
        aplica `plesk bin ftpuser -u <login> -passwd <pwd>`, grava
        <log_dir>/ftp-password-reset.csv chmod 600. Senhas em
        sensitive_values (masking nos logs). Distribuir CSV via canal
        seguro fora-de-banda."""
        self.logger.info("Fase: fix_ftp_passwords (reset=%s)", reset)
        if not self.plesk_bin:
            self.logger.warning(
                "fix_ftp_passwords: binário 'plesk' não localizado — skip"
            )
            return

        domains = self._load_migrated_domains()
        if not domains:
            self.logger.warning("fix_ftp_passwords: 0 domínios — skip")
            return

        placeholders = ",".join(f"'{self._sql_escape(d)}'" for d in domains)
        sql = (
            "SELECT su.login, d.name "
            "FROM sys_users su "
            "JOIN hosting h ON h.sys_user_id=su.id "
            "JOIN domains d ON d.id=h.dom_id "
            f"WHERE d.name IN ({placeholders}) "
            "ORDER BY d.name, su.login;"
        )
        if self.dry_run:
            self.logger.info(
                "[DRY-RUN] %s db -Nse \"%s\"",
                self.plesk_bin, sql.replace('"', '\\"'),
            )
            return

        try:
            proc = subprocess.run(
                [str(self.plesk_bin), "db", "-Nse", sql],
                capture_output=True, text=True,
                timeout=TIMEOUT_FIX_FTP_PASSWORDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PhaseExecutionError(
                f"fix_ftp_passwords: falha chamando `plesk db`: {exc}"
            ) from exc
        if proc.returncode != 0:
            raise PhaseExecutionError(
                f"fix_ftp_passwords: plesk db rc={proc.returncode} "
                f"stderr={proc.stderr.strip()[:300]}"
            )

        rows: list[tuple[str, str]] = []
        for ln in proc.stdout.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2 and parts[0].strip():
                rows.append((parts[0].strip(), parts[1].strip()))
        if not rows:
            self.logger.info("fix_ftp_passwords: 0 sub-FTP users — skip")
            return

        self.log_dir.mkdir(parents=True, exist_ok=True)

        if not reset:
            status_path = self.log_dir / "ftp-password-status.csv"
            try:
                with status_path.open("w", encoding="utf-8") as fh:
                    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                    fh.write("timestamp,login,domain\n")
                    for login, dom in rows:
                        fh.write(f"{ts},{login},{dom}\n")
            except OSError as exc:
                self.logger.warning(
                    "fix_ftp_passwords: falha gravando %s: %s",
                    status_path, exc,
                )
            self.logger.warning(
                "fix_ftp_passwords: %d sub-FTP user(s) listados em %s. "
                "Rode com --reset-ftp-passwords para gerar senhas novas.",
                len(rows), status_path,
            )
            return

        csv_path = self.log_dir / "ftp-password-reset.csv"
        new_file = not csv_path.exists()
        try:
            csv_path.touch(mode=0o600, exist_ok=True)
            os.chmod(csv_path, 0o600)
        except OSError as exc:
            raise PhaseExecutionError(
                f"fix_ftp_passwords: não consegui criar {csv_path}: {exc}"
            ) from exc

        reset_count = 0
        with csv_path.open("a", encoding="utf-8") as fh:
            if new_file:
                fh.write("timestamp,login,domain,new_password\n")
            for login, dom in rows:
                while True:
                    new_pwd = secrets.token_urlsafe(16)
                    if new_pwd[0] not in "-_":
                        break
                self.sensitive_values.append(new_pwd)
                try:
                    self._run(
                        [str(self.plesk_bin), "bin", "ftpuser",
                         "-u", login, "-passwd", new_pwd],
                        timeout=60,
                        log_to=self.log_dir / "fix-ftp-passwords.log",
                    )
                except (PhaseExecutionError, subprocess.CalledProcessError) as exc:
                    self.logger.error(
                        "fix_ftp_passwords: falha resetando %s: %s", login, exc,
                    )
                    continue
                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                fh.write(f"{ts},{login},{dom},{new_pwd}\n")
                reset_count += 1

        try:
            os.chmod(csv_path, 0o600)
        except OSError:
            pass
        self.logger.warning(
            "fix_ftp_passwords: %d/%d senha(s) resetada(s). CSV: %s "
            "(distribua via canal seguro fora-de-banda).",
            reset_count, len(rows), csv_path,
        )
```

- [ ] **Step 5: Verify test passes**

Run: `python3 -m unittest tests.test_fix_ftp_passwords -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add tests/test_fix_ftp_passwords.py plesk_migrator_orchestrator.py
git commit -m "feat(fix-ftp-passwords): new phase resetting sub-FTP user passwords (cPanel hashes incompatible with Plesk Linux SHA-512, mirrors check-mail-passwords contract)"
```

---

## Task 4: Wire `fix-ftp-passwords` into pipeline (CLI, PHASES_ORDER, dispatcher, validation)

**Files:**
- Modify: `plesk_migrator_orchestrator.py:155-165` (PHASES_ORDER)
- Modify: `plesk_migrator_orchestrator.py:2880-2935` (`run()` kwargs + behavior_skip)
- Modify: `plesk_migrator_orchestrator.py:2999-3035` (phase dispatcher list)
- Modify: `plesk_migrator_orchestrator.py:3380-3460` (argparse flags)
- Modify: `plesk_migrator_orchestrator.py:370-385` (`_validate_config` bool tuples)
- Modify: `plesk_migrator_orchestrator.py:3535+` (CLI → run mapping)

**Why:** Sem wiring, `fix_ftp_passwords` é método órfão. Pipeline real precisa do dispatch ordenado e flags CLI.

- [ ] **Step 1: Insert into PHASES_ORDER**

Edit linha ~159 (entre `check-mail-passwords` e `fix-mail-quota`):

```python
    "check-mail-passwords",
    "fix-ftp-passwords",
    "fix-mail-quota",
```

- [ ] **Step 2: Add `_validate_config` entries**

Edit linhas 372 e 384 (procurar `check_mail_passwords` na tupla `skip` e `reset_mail_passwords` na tupla bool):

```python
        for key in (
            "web_content", "mail_content", "db_content",
            "fix_docroot", "fix_mailpath", "check_mail_passwords",
            "fix_ftp_passwords",                    # NEW
            "sanitize_list", "fix_limits", "retransfer_failed",
            "fix_mail_quota", "fix_ftp_renames", "fix_dns_conflicts",
            "fix_owner",
        ):
```

```python
        for key in (
            "dry_run", "skip_install", "force_regenerate",
            "cleanup_config", "resume",
            "apply_owner_fix", "apply_dns_cleanup", "apply_mailpath_fix",
            "reset_mail_passwords", "rename_reserved_subdomains",
            "reset_ftp_passwords",                  # NEW
        ):
```

- [ ] **Step 3: Add `run()` kwargs + behavior mapping**

Edit linha ~2884 (lista de kwargs `def run(`) — adicionar após `reset_mail_passwords`:

```python
        skip_fix_ftp_passwords: bool = False,
        reset_ftp_passwords: bool = False,
```

E na seção de mapping (linha ~2918, após `reset_mail_passwords` mapping):

```python
        skip_fix_ftp_passwords = (
            skip_fix_ftp_passwords
            or behavior_skip.get("fix_ftp_passwords", False)
        )
        reset_ftp_passwords = (
            reset_ftp_passwords
            or behavior.get("reset_ftp_passwords", False)
        )
```

- [ ] **Step 4: Add to phase dispatcher list**

Edit linha ~3018 (entre `check-mail-passwords` e `fix-mail-quota` no dispatcher):

```python
            ("check-mail-passwords",
             lambda: self.check_mail_passwords(reset=reset_mail_passwords),
             not skip_check_mail_passwords),
            ("fix-ftp-passwords",
             lambda: self.fix_ftp_passwords(reset=reset_ftp_passwords),
             not skip_fix_ftp_passwords),
            ("fix-mail-quota", self.fix_mail_quota, not skip_fix_mail_quota),
```

- [ ] **Step 5: Add CLI flags**

Edit ~linha 3399 (após `--reset-mail-passwords`):

```python
    parser.add_argument("--skip-fix-ftp-passwords", action="store_true",
                        help="Pula auditoria/reset de senhas de sub-FTP users "
                             "(fase fix-ftp-passwords)")
    parser.add_argument("--reset-ftp-passwords", action="store_true",
                        help="Em fix-ftp-passwords, gera senha nova "
                             "(urlsafe(16)) para cada sub-FTP user listado e "
                             "grava CSV chmod 600 em "
                             "<log_dir>/ftp-password-reset.csv. Distribuir "
                             "via canal seguro fora-de-banda.")
```

- [ ] **Step 6: Wire CLI → run() at bottom**

Edit linha ~3537+ (procurar `skip_mail_content=args.skip_mail_content`):

```python
            skip_fix_ftp_passwords=args.skip_fix_ftp_passwords,
            reset_ftp_passwords=args.reset_ftp_passwords,
```

- [ ] **Step 7: Smoke test — pipeline doesn't crash**

Run: `python3 -m py_compile plesk_migrator_orchestrator.py`
Expected: silent (no syntax error)

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS (50+ tests)

- [ ] **Step 8: Commit**

```bash
git add plesk_migrator_orchestrator.py
git commit -m "feat(fix-ftp-passwords): wire CLI flags + PHASES_ORDER + dispatcher (--skip-fix-ftp-passwords, --reset-ftp-passwords)"
```

---

## Task 5: CLAUDE.md cross-ref final (já tem 3 gotchas adicionadas nesta sessão)

**Files:**
- Modify: `CLAUDE.md` (seção Armadilhas)

**Why:** O bullet `_validate_docroot_match` atual (linha 67) descreve heurística antiga; precisa atualizar para refletir `/etc/userdomains` lookup. E PHASES_ORDER definitivo agora inclui `fix-ftp-passwords`.

- [ ] **Step 1: Update bullet PHASES_ORDER**

Procurar bullet `**PHASES_ORDER definitivo**`:

```bash
grep -n "PHASES_ORDER definitivo" /home/fcs/Documents/ntweb/opiniao/CLAUDE.md
```

Edit substituindo a sequência:

```
...check-mail-passwords → fix-mail-quota → fix-ftp-renames...
```

por:

```
...check-mail-passwords → fix-ftp-passwords → fix-mail-quota → fix-ftp-renames...
```

- [ ] **Step 2: Update bullet `_validate_docroot_match` heurística final**

A linha 67 termina com:
```
Username cPanel inferido como `dom.split('.', 1)[0]` (heurística — operadores com mapeamento exótico estendem `_validate_docroot_match`).
```

Substituir por:
```
Username cPanel resolvido via `_resolve_cpanel_user` (SSH `grep /etc/userdomains` cacheado por sessão); fallback heurístico `dom.split('.', 1)[0]` apenas se lookup falhar (sshpass ausente, host inacessível, domínio fora do /etc/userdomains).
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): update PHASES_ORDER + _validate_docroot_match notes (fix-ftp-passwords inserted, /etc/userdomains canonical lookup)"
```

---

## Task 6: Deploy + recovery em produção (191.7.26.24)

**Files:** N/A (operações remotas)

**Why:** Código corrigido precisa chegar ao Plesk dest E o docroot já-quebrado de `opiniao.inf.br` precisa ser revertido antes de re-rodar `fix-docroot` para o caminho relativo correto.

- [ ] **Step 1: Push local → GitHub**

```bash
cd /home/fcs/Documents/ntweb/opiniao
git status                       # working tree clean esperado
git log --oneline -10            # confirmar 4 novos commits
git push origin feat/plesk-migrator-orchestrator
```

- [ ] **Step 2: Pull no remoto Plesk**

```bash
ssh -p 2222 root@191.7.26.24 'cd /root/plesk-migrator-orchestrator && git fetch && git checkout feat/plesk-migrator-orchestrator && git pull && git log --oneline -5'
```

Expected: 4 novos commits visíveis.

- [ ] **Step 3: Sanity check — sintaxe + suite no remoto**

```bash
ssh -p 2222 root@191.7.26.24 'cd /root/plesk-migrator-orchestrator && python3.8 -m py_compile plesk_migrator_orchestrator.py && python3 -m unittest discover -s tests -v 2>&1 | tail -20'
```

Expected: `OK` no final.

- [ ] **Step 4: Reverter docroot quebrado de opiniao.inf.br**

```bash
ssh -p 2222 root@191.7.26.24 'plesk bin subscription -u opiniao.inf.br -www-root httpdocs && plesk db -Nse "SELECT name, www_root FROM domains d JOIN hosting h ON d.id=h.dom_id WHERE d.name=\"opiniao.inf.br\""'
```

Expected: `/var/www/vhosts/opiniao.inf.br/httpdocs` (sem duplicação).

- [ ] **Step 5: Re-rodar fix-docroot corrigido**

```bash
ssh -p 2222 root@191.7.26.24 'cd /root/plesk-migrator-orchestrator && sudo ./run.sh --config /etc/plesk-migration.yaml --skip-install --resume --skip-mail-content --skip-fix-mailpath --only-phase fix-docroot 2>&1 | tail -30'
```

Expected: log mostra `fix-docroot: opiniao.inf.br → apontando www-root para public_html` E SSH validation `hash OK` (não mais `pulada`).

- [ ] **Step 6: Validar SQL pós-fix**

```bash
ssh -p 2222 root@191.7.26.24 'plesk db -Nse "SELECT name, www_root FROM domains d JOIN hosting h ON d.id=h.dom_id WHERE d.name=\"opiniao.inf.br\""'
```

Expected: `/var/www/vhosts/opiniao.inf.br/public_html` (sem duplicação).

- [ ] **Step 7: Re-rodar test-all + fix-ftp-passwords**

```bash
ssh -p 2222 root@191.7.26.24 'cd /root/plesk-migrator-orchestrator && sudo ./run.sh --config /etc/plesk-migration.yaml --skip-install --resume --skip-mail-content --skip-fix-mailpath --reset-ftp-passwords --only-phase fix-ftp-passwords 2>&1 | tail -20'
```

Expected: `5/5 senha(s) resetada(s)` + CSV gerado.

- [ ] **Step 8: Final test-all**

```bash
ssh -p 2222 root@191.7.26.24 'cd /root/plesk-migrator-orchestrator && sudo ./run.sh --config /etc/plesk-migration.yaml --skip-install --resume --skip-mail-content --skip-fix-mailpath --only-phase test 2>&1 | tail -20'
```

Expected: rc=0; Apache totals ~31/31 success (ou pelo menos majoria — alguns sites podem ter problemas próprios não-relacionados à migração).

- [ ] **Step 9: Curl smoke**

```bash
ssh -p 2222 root@191.7.26.24 'curl -sI -o /dev/null -w "%{http_code}\n" http://opiniao.inf.br/'
```

Expected: `200` ou `301` (redirect para `www.grupoopiniao.inf.br` per `.htaccess` visto antes). NÃO `403`.

- [ ] **Step 10: Recolher CSV de senhas + distribuir**

```bash
ssh -p 2222 root@191.7.26.24 'cat /var/log/plesk-migration-orchestrator/ftp-password-reset.csv'
```

Distribuir os 5 logins/senhas via canal seguro fora-de-banda (não Slack/email plain).

---

## Verification (end-to-end)

```bash
cd /home/fcs/Documents/ntweb/opiniao

# Sintaxe:
python3 -m py_compile plesk_migrator_orchestrator.py

# Suite completa local (50+ testes):
python3 -m unittest discover -s tests -v

# Específicos novos:
python3 -m unittest tests.test_fix_docroot_relative_path -v
python3 -m unittest tests.test_resolve_cpanel_user -v
python3 -m unittest tests.test_fix_ftp_passwords -v

# Remoto: smoke E2E (per Task 6 Step 6/8/9)
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Bug `-www-root` absoluto → Task 1
- [x] `_validate_docroot_match` heurística user → Task 2
- [x] Nova fase fix-ftp-passwords → Task 3 + 4
- [x] Skip mail import (user decisão) → coberto por `--skip-mail-content --skip-fix-mailpath` existentes; Task 6 usa ambos
- [x] Recovery em produção → Task 6
- [x] Docs → Task 5

**Type/method consistency:**
- `fix_ftp_passwords(*, reset: bool = False)` — assinatura idêntica em Task 3 método e Task 4 dispatcher (`reset=reset_ftp_passwords`)
- `_resolve_cpanel_user(domain: str) -> str | None` — usado em Task 2 Step 4 com `or` chain
- `TIMEOUT_FIX_FTP_PASSWORDS = 300` — definido em Task 3 Step 3, usado no método

**Placeholders scan:** Nenhum `TODO`/`TBD`/`implement later` no plano.

**Subdomain docroots:** intencionalmente fora de escopo (fase nova `fix-docroot-subdomains` seria projeto separado). Documentado em Task 6 Step 8 como possível observação pós-test.

---

## Critical files

- `/home/fcs/Documents/ntweb/opiniao/plesk_migrator_orchestrator.py` — todas mudanças código
- `/home/fcs/Documents/ntweb/opiniao/tests/test_fix_docroot_relative_path.py` — NOVO
- `/home/fcs/Documents/ntweb/opiniao/tests/test_resolve_cpanel_user.py` — NOVO
- `/home/fcs/Documents/ntweb/opiniao/tests/test_fix_ftp_passwords.py` — NOVO
- `/home/fcs/Documents/ntweb/opiniao/CLAUDE.md` — gotchas + PHASES_ORDER refresh
- Remoto: `/root/plesk-migrator-orchestrator/` + `/var/log/plesk-migration-orchestrator/` + `/usr/local/psa/var/modules/panel-migrator/sessions/migration-session/`
