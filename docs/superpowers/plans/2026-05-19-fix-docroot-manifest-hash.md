# fix-docroot manifest-hash detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `fix_docroot` reliably find where plesk-migrator deposited web content (currently misses anything outside `public_html`/`httpdocs`) by scanning multiple candidate paths and ranking them via a fast stat-based manifest hash.

**Architecture:** Add a `@staticmethod _dir_manifest(path)` helper that returns `(file_count, total_bytes, md5_of_sorted("relpath:size"))`. `fix_docroot` scans a fixed list of candidate dirs per domain, builds a manifest per path, ignores empty ones, picks the richest non-canonical candidate by total_bytes, and updates `www-root` via the existing `plesk bin subscription -u <dom> -www-root <path>` call only when the chosen path differs from `httpdocs` and has unique content (different hash). Two paths with identical hashes are treated as the same content (covers symlinks / prior partial fixes).

**Tech Stack:** Python 3.8+, stdlib only (`hashlib`, `os`, `pathlib`), `unittest` for tests (no third-party test deps — repo has no test infra and target servers run CentOS/RHEL 8 with stock Python).

---

## File Structure

- **Modify:** `plesk_migrator_orchestrator.py`
  - Add module constant `DOCROOT_CANDIDATES` near other constants (~line 100)
  - Add `@staticmethod _dir_manifest(path)` inside `PleskMigrationOrchestrator` (immediately before `fix_docroot`, ~line 1640)
  - Replace body of `fix_docroot` (lines 1641–1723) with manifest-driven scan
- **Create:** `tests/__init__.py` (empty, enables package discovery)
- **Create:** `tests/test_dir_manifest.py` (unittest for helper)
- **Create:** `tests/test_fix_docroot_logic.py` (unittest for path-selection logic, isolated)

No tests for the full `fix_docroot` instance method — it requires a Plesk server. Coverage is split: helper + selection logic in unit tests; real `plesk bin subscription` call validated manually via dry-run on the live server.

---

### Task 1: Test scaffolding and failing test for empty directory

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_dir_manifest.py`

- [ ] **Step 1: Create empty `tests/__init__.py`**

```bash
touch tests/__init__.py
```

- [ ] **Step 2: Write the first failing test**

Create `tests/test_dir_manifest.py`:

```python
"""Tests for PleskMigrationOrchestrator._dir_manifest static helper.

The helper returns (file_count, total_bytes, manifest_hash) for a directory.
manifest_hash is MD5 of '\n'.join(sorted "relpath:size") so two trees with
identical names+sizes hash the same regardless of mtime or content bytes."""
from __future__ import annotations

import hashlib
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator


class DirManifestTests(unittest.TestCase):
    def test_empty_directory_returns_zero_counts_and_empty_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            count, total, digest = PleskMigrationOrchestrator._dir_manifest(
                pathlib.Path(tmp)
            )
            self.assertEqual(count, 0)
            self.assertEqual(total, 0)
            self.assertEqual(digest, hashlib.md5(b"").hexdigest())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test, verify it fails**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest tests.test_dir_manifest -v
```

Expected: FAIL with `AttributeError: type object 'PleskMigrationOrchestrator' has no attribute '_dir_manifest'`

---

### Task 2: Implement minimal `_dir_manifest` to pass empty-dir test

**Files:**
- Modify: `plesk_migrator_orchestrator.py` (insert before `fix_docroot` at line 1641)

- [ ] **Step 1: Add the static helper**

Insert immediately above the `def fix_docroot(self) -> None:` line (currently line 1641):

```python
    @staticmethod
    def _dir_manifest(path: pathlib.Path) -> tuple[int, int, str]:
        """Stat-only fingerprint of a directory tree.

        Returns (file_count, total_bytes, md5_hex). md5_hex hashes
        '\n'.join(sorted "relpath:size") — content bytes are NOT read,
        so it is fast even on multi-GB web trees and stable across rsync
        (mtime/atime ignored). Two paths with identical manifest_hash hold
        the same files (by name + size). Missing path or stat errors yield
        (0, 0, md5("")). followlinks=False to avoid infinite loops on
        symlinked vhosts."""
        entries: list[str] = []
        count = 0
        total = 0
        if path.is_dir():
            for root, dirs, files in os.walk(path, followlinks=False):
                dirs.sort()
                for fname in sorted(files):
                    fp = pathlib.Path(root) / fname
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    rel = fp.relative_to(path)
                    entries.append(f"{rel}:{st.st_size}")
                    count += 1
                    total += st.st_size
        digest = hashlib.md5("\n".join(entries).encode("utf-8")).hexdigest()
        return count, total, digest
```

- [ ] **Step 2: Run the empty-dir test, verify it passes**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest tests.test_dir_manifest -v
```

Expected: `OK` — 1 test passed.

- [ ] **Step 3: Commit**

```bash
cd /home/fcs/Documents/opiniao
git add plesk_migrator_orchestrator.py tests/__init__.py tests/test_dir_manifest.py
git commit -m "$(cat <<'EOF'
feat(fix-docroot): add _dir_manifest static helper

Returns (file_count, total_bytes, md5_of_sorted_relpath_size) for a path.
Stat-only — does not read file content — so it scales to multi-GB web
trees. Used by an upcoming fix_docroot rewrite to find where
plesk-migrator deposited content beyond public_html/httpdocs.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Test with populated directory

**Files:**
- Modify: `tests/test_dir_manifest.py`

- [ ] **Step 1: Add the failing test**

Append inside the `DirManifestTests` class:

```python
    def test_populated_directory_counts_files_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "index.html").write_text("hello")          # 5 bytes
            (root / "sub").mkdir()
            (root / "sub" / "a.txt").write_text("xy")          # 2 bytes
            (root / "sub" / "b.txt").write_bytes(b"\x00" * 10) # 10 bytes

            count, total, digest = PleskMigrationOrchestrator._dir_manifest(root)

            self.assertEqual(count, 3)
            self.assertEqual(total, 17)
            self.assertNotEqual(digest, hashlib.md5(b"").hexdigest())
```

- [ ] **Step 2: Run, verify it passes**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest tests.test_dir_manifest.DirManifestTests.test_populated_directory_counts_files_and_bytes -v
```

Expected: `OK`.

---

### Task 4: Test manifest stability — identical trees produce identical hashes

**Files:**
- Modify: `tests/test_dir_manifest.py`

- [ ] **Step 1: Add the test**

Append inside `DirManifestTests`:

```python
    def test_identical_trees_have_identical_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            a = root / "a"
            b = root / "b"
            for d in (a, b):
                d.mkdir()
                (d / "index.php").write_text("<?php echo 1;")  # 14 bytes
                (d / "wp-config.php").write_text("XXXXX")      # 5 bytes

            _, _, digest_a = PleskMigrationOrchestrator._dir_manifest(a)
            _, _, digest_b = PleskMigrationOrchestrator._dir_manifest(b)
            self.assertEqual(digest_a, digest_b)

    def test_different_size_breaks_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            a = root / "a"
            b = root / "b"
            a.mkdir(); b.mkdir()
            (a / "f.txt").write_text("hello")
            (b / "f.txt").write_text("helloworld")  # different size

            _, _, digest_a = PleskMigrationOrchestrator._dir_manifest(a)
            _, _, digest_b = PleskMigrationOrchestrator._dir_manifest(b)
            self.assertNotEqual(digest_a, digest_b)
```

- [ ] **Step 2: Run the two new tests, verify they pass**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest tests.test_dir_manifest -v
```

Expected: 4 tests `OK`.

---

### Task 5: Test that missing/non-dir paths are handled gracefully

**Files:**
- Modify: `tests/test_dir_manifest.py`

- [ ] **Step 1: Add the test**

Append inside `DirManifestTests`:

```python
    def test_missing_path_returns_zero(self) -> None:
        nonexistent = pathlib.Path("/nonexistent/path/that/does/not/exist/xyz")
        count, total, digest = PleskMigrationOrchestrator._dir_manifest(nonexistent)
        self.assertEqual(count, 0)
        self.assertEqual(total, 0)
        self.assertEqual(digest, hashlib.md5(b"").hexdigest())

    def test_file_path_not_dir_returns_zero(self) -> None:
        with tempfile.NamedTemporaryFile() as tmp:
            count, total, digest = PleskMigrationOrchestrator._dir_manifest(
                pathlib.Path(tmp.name)
            )
            self.assertEqual(count, 0)
            self.assertEqual(total, 0)
```

- [ ] **Step 2: Run, verify all 6 tests pass**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest tests.test_dir_manifest -v
```

Expected: 6 tests `OK`.

- [ ] **Step 3: Commit**

```bash
cd /home/fcs/Documents/opiniao
git add tests/test_dir_manifest.py
git commit -m "$(cat <<'EOF'
test(fix-docroot): full coverage for _dir_manifest helper

- empty dir → (0, 0, md5(""))
- populated dir → correct count + total_bytes
- identical trees → identical hash
- size diff → hash diff
- missing path / non-dir → (0, 0, md5(""))

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Add `DOCROOT_CANDIDATES` constant

**Files:**
- Modify: `plesk_migrator_orchestrator.py` (around line 100, after `TIMEOUT_FIX_OWNER`)

- [ ] **Step 1: Add the constant**

Locate the block ending with `TIMEOUT_FIX_OWNER = 1800` (currently line 99). Immediately after it, add:

```python
# Subpastas escaneadas por fix-docroot dentro de /var/www/vhosts/<domain>/.
# Ordem importa apenas para tie-break determinístico (mesmo total_bytes):
# httpdocs primeiro porque é o canonical Plesk; se rivaliza com outro, vence.
DOCROOT_CANDIDATES = ("httpdocs", "public_html", "www", "web")
```

- [ ] **Step 2: Verify byte-compile still succeeds**

```bash
cd /home/fcs/Documents/opiniao && python3 -m py_compile plesk_migrator_orchestrator.py && echo OK
```

Expected: `OK`.

---

### Task 7: Write failing test for new selection helper

**Files:**
- Create: `tests/test_fix_docroot_logic.py`

The selection logic deserves its own pure function so it stays testable without a Plesk server. Extract it as `@staticmethod _pick_docroot(manifests)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fix_docroot_logic.py`:

```python
"""Tests for _pick_docroot — pure selection logic for fix-docroot."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plesk_migrator_orchestrator import PleskMigrationOrchestrator


class PickDocrootTests(unittest.TestCase):
    """`_pick_docroot(manifests)` takes a dict
    {candidate_name: (count, total_bytes, md5)} and returns either:
      - None  -> no action (httpdocs already has content, or nothing
                 anywhere, or chosen path is hash-identical to httpdocs)
      - name  -> the candidate to point www-root at
    """

    def test_all_empty_returns_none(self) -> None:
        manifests = {
            "httpdocs":    (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "public_html": (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "www":         (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "web":         (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
        }
        self.assertIsNone(PleskMigrationOrchestrator._pick_docroot(manifests))

    def test_httpdocs_has_content_returns_none(self) -> None:
        manifests = {
            "httpdocs":    (10, 5000, "aaa"),
            "public_html": (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "www":         (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "web":         (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
        }
        self.assertIsNone(PleskMigrationOrchestrator._pick_docroot(manifests))

    def test_only_public_html_populated_returns_public_html(self) -> None:
        manifests = {
            "httpdocs":    (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "public_html": (42, 9_500_000, "bbb"),
            "www":         (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "web":         (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
        }
        self.assertEqual(
            PleskMigrationOrchestrator._pick_docroot(manifests), "public_html"
        )

    def test_multiple_populated_picks_largest_total_bytes(self) -> None:
        manifests = {
            "httpdocs":    (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "public_html": (5, 100, "bbb"),
            "www":         (3, 9_000_000, "ccc"),  # winner
            "web":         (2, 50, "ddd"),
        }
        self.assertEqual(
            PleskMigrationOrchestrator._pick_docroot(manifests), "www"
        )

    def test_chosen_path_hash_equal_to_httpdocs_returns_none(self) -> None:
        # httpdocs and public_html have same file listing (symlink, hardlinks,
        # or prior partial fix). Nothing to do.
        same_hash = "samehashsamehashsamehashsamehash"
        manifests = {
            "httpdocs":    (3, 1234, same_hash),
            "public_html": (3, 1234, same_hash),
            "www":         (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
            "web":         (0, 0, "d41d8cd98f00b204e9800998ecf8427e"),
        }
        # httpdocs already populated → returns None even though public_html
        # also has content.
        self.assertIsNone(PleskMigrationOrchestrator._pick_docroot(manifests))

    def test_httpdocs_empty_but_hash_matches_picked_returns_none(self) -> None:
        # Edge case: empty httpdocs (hash = md5("")) and chosen candidate
        # also empty would already filter out by count check, but make sure
        # the explicit guard works when manifests dict only has one rich
        # entry that happens to match the empty hash (theoretically impossible
        # with real files, but cheap to guard).
        empty_hash = "d41d8cd98f00b204e9800998ecf8427e"
        manifests = {
            "httpdocs":    (0, 0, empty_hash),
            "public_html": (0, 0, empty_hash),
            "www":         (0, 0, empty_hash),
            "web":         (0, 0, empty_hash),
        }
        self.assertIsNone(PleskMigrationOrchestrator._pick_docroot(manifests))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify failure**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest tests.test_fix_docroot_logic -v
```

Expected: FAIL with `AttributeError: ... has no attribute '_pick_docroot'`.

---

### Task 8: Implement `_pick_docroot`

**Files:**
- Modify: `plesk_migrator_orchestrator.py` (insert directly below `_dir_manifest`)

- [ ] **Step 1: Add the helper**

Insert immediately after the closing of `_dir_manifest` (before `def fix_docroot`):

```python
    @staticmethod
    def _pick_docroot(
        manifests: dict[str, tuple[int, int, str]],
    ) -> str | None:
        """Decide which candidate `<vhost>/<name>` should be www-root.

        Input: {candidate_name: (file_count, total_bytes, manifest_hash)}.
        Returns the candidate name to point www-root at, or None when no
        action is needed:
          - all candidates empty
          - httpdocs already has content (canonical wins, even if another
            candidate also has content — we don't move a working site)
          - the richest non-canonical candidate has the same manifest_hash
            as httpdocs (symlinked / hardlinked / prior partial fix)

        Tie-break on total_bytes picks the first key in insertion order,
        which matches DOCROOT_CANDIDATES ordering."""
        httpdocs = manifests.get("httpdocs", (0, 0, ""))
        rich = {k: v for k, v in manifests.items() if v[0] > 0}
        if not rich:
            return None
        if httpdocs[0] > 0:
            return None
        # httpdocs is empty; pick the heaviest non-httpdocs candidate
        best_name = max(
            (k for k in rich if k != "httpdocs"),
            key=lambda k: rich[k][1],
            default=None,
        )
        if best_name is None:
            return None
        if rich[best_name][2] == httpdocs[2]:
            return None
        return best_name
```

- [ ] **Step 2: Run selection-logic tests, verify all pass**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest tests.test_fix_docroot_logic -v
```

Expected: 6 tests `OK`.

- [ ] **Step 3: Run full test suite**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest discover tests -v
```

Expected: 12 tests `OK` (6 manifest + 6 selection).

- [ ] **Step 4: Commit**

```bash
cd /home/fcs/Documents/opiniao
git add plesk_migrator_orchestrator.py tests/test_fix_docroot_logic.py
git commit -m "$(cat <<'EOF'
feat(fix-docroot): add _pick_docroot selection logic

Pure function over manifest dict; no filesystem touch. Decides whether
to (a) skip — httpdocs already populated, or hash-equal to chosen
candidate — or (b) return the candidate name to point www-root at.
Picks the heaviest non-httpdocs candidate by total_bytes.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Rewrite `fix_docroot` to use the new helpers

**Files:**
- Modify: `plesk_migrator_orchestrator.py` (replace body of `fix_docroot`, currently lines 1641–1723 — exact range will shift after Task 8 inserts ~70 lines above; the function still starts at the next `def fix_docroot` after `_pick_docroot`)

- [ ] **Step 1: Replace the function body**

Locate `def fix_docroot(self) -> None:` and replace **the entire function body up to (but not including)** `def fix_mailpath` with:

```python
    def fix_docroot(self) -> None:
        """Ajusta www-root das subscriptions migradas detectando onde
        plesk-migrator depositou o conteúdo. Escaneia DOCROOT_CANDIDATES
        em /var/www/vhosts/<domain>/, gera manifest stat-only por path
        (count + bytes + md5 de filename:size), e via _pick_docroot
        decide se ajusta.

        Idempotente:
          - httpdocs já populado → skip
          - tudo vazio → skip (vhost ainda não recebeu conteúdo)
          - melhor candidato hash-igual a httpdocs (symlink/hardlink) → skip
          - caso contrário → `plesk bin subscription -u <dom> -www-root <path>`

        Log detalhado em <log_dir>/fix-docroot.log: por domínio, todos os
        candidatos escaneados (count/bytes/hash truncado) + decisão."""
        self.logger.info("Fase: fix_docroot")
        if not self.plesk_bin:
            raise PhaseExecutionError(
                "fix_docroot: binário 'plesk' não localizado. "
                "Necessário para `plesk bin subscription`."
            )

        domains = self._load_migrated_domains()
        if not domains:
            self.logger.warning(
                "fix_docroot: nenhuma subscription migrada encontrada em %s — skip",
                self.sessions_dir / self.session_name,
            )
            return

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("fix_docroot: log_dir mkdir falhou: %s", exc)
        report = self.log_dir / "fix-docroot.log"

        vhosts_root = pathlib.Path("/var/www/vhosts")
        for domain in domains:
            vhost = vhosts_root / domain
            if not vhost.is_dir():
                self.logger.warning(
                    "fix_docroot: vhost %s ausente — skip", vhost
                )
                continue

            manifests: dict[str, tuple[int, int, str]] = {}
            for name in DOCROOT_CANDIDATES:
                manifests[name] = self._dir_manifest(vhost / name)

            scan_summary = ", ".join(
                f"{n}={c}f/{b}B/{h[:8]}"
                for n, (c, b, h) in manifests.items()
            )
            self.logger.info("fix_docroot: %s scan: %s", domain, scan_summary)

            try:
                with report.open("a", encoding="utf-8") as fh:
                    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                    fh.write(f"# {ts} {domain}\n")
                    for n, (c, b, h) in manifests.items():
                        fh.write(f"  {n}: count={c} bytes={b} hash={h}\n")
            except OSError as exc:
                self.logger.warning(
                    "fix_docroot: falha escrevendo report: %s", exc
                )

            choice = self._pick_docroot(manifests)
            if choice is None:
                self.logger.info(
                    "fix_docroot: %s nada a ajustar — skip", domain
                )
                continue

            target = vhost / choice
            self.logger.info(
                "fix_docroot: %s → apontando www-root para %s "
                "(%d arquivos, %d bytes)",
                domain, target, manifests[choice][0], manifests[choice][1],
            )
            if self.dry_run:
                self.logger.info(
                    "[DRY-RUN] %s bin subscription -u %s -www-root %s",
                    self.plesk_bin, domain, target,
                )
                continue

            self._run(
                [str(self.plesk_bin), "bin", "subscription",
                 "-u", domain, "-www-root", str(target)],
                timeout=TIMEOUT_FIX_DOCROOT,
                log_to=report,
            )
```

- [ ] **Step 2: Byte-compile to catch syntax errors**

```bash
cd /home/fcs/Documents/opiniao && python3 -m py_compile plesk_migrator_orchestrator.py && echo OK
```

Expected: `OK`.

- [ ] **Step 3: Run full test suite — confirm refactor didn't break helpers**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest discover tests -v
```

Expected: 12 tests `OK`.

- [ ] **Step 4: Sanity-check `--help` still works**

```bash
cd /home/fcs/Documents/opiniao && python3 plesk_migrator_orchestrator.py --help 2>&1 | grep -E "fix-docroot|--skip-fix-docroot"
```

Expected: shows `--skip-fix-docroot` line and `fix-docroot` in `--only-phase` choices.

---

### Task 10: Smoke-test the new `fix_docroot` with a fabricated tree

**Files:** none (read-only smoke run)

The full instance method can't be unit-tested without a Plesk server, but we can exercise the scan + decision path against a fabricated tree on the dev machine by running the orchestrator in dry-run with a minimal config pointing at a fake sessions dir.

- [ ] **Step 1: Set up fake vhost tree under `$CLAUDE_JOB_DIR`**

```bash
JOB=$CLAUDE_JOB_DIR
mkdir -p "$JOB/vhosts/example.test/public_html" "$JOB/vhosts/example.test/httpdocs"
echo '<?php phpinfo();' > "$JOB/vhosts/example.test/public_html/index.php"
mkdir -p "$JOB/vhosts/example.test/public_html/wp-content"
echo "x" > "$JOB/vhosts/example.test/public_html/wp-content/dummy.txt"
```

- [ ] **Step 2: Drive the helpers via inline Python — verify selection picks `public_html`**

```bash
cd /home/fcs/Documents/opiniao && python3 <<'PY'
import pathlib, sys
sys.path.insert(0, ".")
from plesk_migrator_orchestrator import PleskMigrationOrchestrator, DOCROOT_CANDIDATES
import os
vhost = pathlib.Path(os.environ["CLAUDE_JOB_DIR"]) / "vhosts" / "example.test"
manifests = {n: PleskMigrationOrchestrator._dir_manifest(vhost / n) for n in DOCROOT_CANDIDATES}
print("manifests:", manifests)
print("pick:", PleskMigrationOrchestrator._pick_docroot(manifests))
assert PleskMigrationOrchestrator._pick_docroot(manifests) == "public_html", "expected public_html"
print("OK")
PY
```

Expected output ends with `OK`. `manifests["public_html"][0]` should be `2`, `manifests["httpdocs"][0]` should be `0`.

- [ ] **Step 3: Test the symlink-equivalent case (httpdocs → public_html)**

```bash
JOB=$CLAUDE_JOB_DIR
rm -rf "$JOB/vhosts/example.test/httpdocs"
ln -s "$JOB/vhosts/example.test/public_html" "$JOB/vhosts/example.test/httpdocs"
cd /home/fcs/Documents/opiniao && python3 <<'PY'
import pathlib, sys, os
sys.path.insert(0, ".")
from plesk_migrator_orchestrator import PleskMigrationOrchestrator, DOCROOT_CANDIDATES
vhost = pathlib.Path(os.environ["CLAUDE_JOB_DIR"]) / "vhosts" / "example.test"
manifests = {n: PleskMigrationOrchestrator._dir_manifest(vhost / n) for n in DOCROOT_CANDIDATES}
print("manifests:", manifests)
print("pick:", PleskMigrationOrchestrator._pick_docroot(manifests))
# httpdocs is a symlink to public_html. is_dir() resolves the symlink, so
# we scan public_html via httpdocs → httpdocs reports populated → pick=None.
assert PleskMigrationOrchestrator._pick_docroot(manifests) is None, "should skip when httpdocs symlinks to populated dir"
print("OK")
PY
```

Expected: `OK`.

- [ ] **Step 4: Commit the refactor**

```bash
cd /home/fcs/Documents/opiniao
git add plesk_migrator_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(fix-docroot): scan candidate dirs and pick richest by manifest

Replaces the public_html-vs-httpdocs check with a multi-candidate scan
(httpdocs, public_html, www, web). Uses _dir_manifest (stat-only) to
rank candidates by total_bytes and detect symlink/hardlink equivalence
via manifest_hash. fix-docroot.log records every candidate's
count/bytes/hash for postmortem.

Behavior:
- httpdocs populated -> skip (canonical wins, never move a working site)
- all empty -> skip
- chosen candidate hash-equal to httpdocs -> skip (already linked)
- otherwise -> plesk bin subscription -u <dom> -www-root <chosen>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Update CLAUDE.md gotcha entry for fix-docroot

**Files:**
- Modify: `CLAUDE.md` (the existing fix-docroot bullet point)

- [ ] **Step 1: Locate the fix-docroot gotcha**

The current line in CLAUDE.md starts with `- **Docroot vazio pós-migração**` and explains the old public_html/httpdocs logic.

- [ ] **Step 2: Replace it**

Use the Edit tool to replace that bullet with:

```
- **Docroot vazio pós-migração**: plesk-migrator pode depositar conteúdo em qualquer subpasta cPanel-style (`public_html/`, `www/`, `web/`) enquanto Plesk default `www-root` = `httpdocs/`. Fase `fix-docroot` (após `copy-db`) escaneia `DOCROOT_CANDIDATES = ("httpdocs", "public_html", "www", "web")` em `/var/www/vhosts/<dom>/`, gera manifest stat-only via `_dir_manifest` (count + bytes + MD5 de `relpath:size` ordenado, não lê conteúdo — rápido em GB), `_pick_docroot` escolhe o candidato não-canonical de maior `total_bytes`. Skip se: httpdocs já populado, tudo vazio, ou hash do escolhido = hash do httpdocs (symlink/hardlink). Aplica `plesk bin subscription -u <dom> -www-root <path>`. Domínios carregados de `<session>/successful-subscriptions.*` (fallback: JSON status/report). Skip-flag: `--skip-fix-docroot`. Isolado: `--only-phase fix-docroot --resume`. Log per-domínio em `<log_dir>/fix-docroot.log` (todos os candidatos com count/bytes/hash truncado).
```

- [ ] **Step 3: Commit**

```bash
cd /home/fcs/Documents/opiniao
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(CLAUDE.md): update fix-docroot entry for manifest-hash scan

Reflects the new DOCROOT_CANDIDATES + _dir_manifest + _pick_docroot
flow that replaced the public_html/httpdocs-only check.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Verification — full end-to-end

These run after all tasks land.

- [ ] **Unit suite green**

```bash
cd /home/fcs/Documents/opiniao && python3 -m unittest discover tests -v
```

Expected: 12 passed (6 manifest + 6 selection).

- [ ] **Byte-compile clean**

```bash
cd /home/fcs/Documents/opiniao && python3 -m py_compile plesk_migrator_orchestrator.py && echo OK
```

Expected: `OK`.

- [ ] **CLI still shape-correct**

```bash
cd /home/fcs/Documents/opiniao && python3 plesk_migrator_orchestrator.py --help | grep -E "fix-docroot|skip-fix-docroot"
```

Expected: lines mentioning `fix-docroot` (in `--only-phase` choices) and `--skip-fix-docroot` flag.

- [ ] **Live dry-run on the Plesk destination server**

Once changes are pushed and deployed to the Plesk box (root shell required):

```bash
sudo ./run.sh --config /etc/plesk-migration.yaml --skip-install --dry-run --only-phase fix-docroot --resume
```

Expected log highlights:
- `Fase: fix_docroot`
- For each migrated domain: `fix_docroot: <dom> scan: httpdocs=Nf/NB/HHHH, public_html=...`
- Either `fix_docroot: <dom> nada a ajustar — skip` or `[DRY-RUN] ... bin subscription -u <dom> -www-root /var/www/vhosts/<dom>/<chosen>`
- A populated `<log_dir>/fix-docroot.log` with per-domain candidate breakdown

- [ ] **Live apply run**

```bash
sudo ./run.sh --config /etc/plesk-migration.yaml --skip-install --only-phase fix-docroot --resume
```

Spot-check 2–3 sites in a browser: pages render (no Plesk default index). Re-run the same command — second pass should log `nada a ajustar — skip` for every domain (idempotency).
