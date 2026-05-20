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

    def test_identical_trees_have_identical_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            a = root / "a"
            b = root / "b"
            for d in (a, b):
                d.mkdir()
                (d / "index.php").write_text("<?php echo 1;")  # 13 bytes
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
            self.assertEqual(digest, hashlib.md5(b"").hexdigest())

    def test_entries_globally_lex_sorted_matches_remote_format(self) -> None:
        """Local manifest must use the SAME entry ordering as the remote
        helper (`find ... | sort -u`) — a full lexicographic sort over
        joined "relpath:size" strings. os.walk yields root-files first,
        then descends per-subdir, which breaks parity for any tree where
        a root file lex-sorts after a first-level subdir name. The local
        helper must call entries.sort() before MD5 to align."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # `zzz.txt` lex-sorts after `aaa/` — os.walk would emit it
            # first (root-files-first), `find | sort -u` emits it last.
            (root / "zzz.txt").write_text("hi")  # 2 bytes
            (root / "aaa").mkdir()
            (root / "aaa" / "file.php").write_text("xxx")  # 3 bytes

            count, total, digest = PleskMigrationOrchestrator._dir_manifest(
                root
            )
            expected_body = "\n".join(sorted([
                "aaa/file.php:3",
                "zzz.txt:2",
            ]))
            expected_digest = hashlib.md5(
                expected_body.encode("utf-8")
            ).hexdigest()
            self.assertEqual(count, 2)
            self.assertEqual(total, 5)
            self.assertEqual(digest, expected_digest)


if __name__ == "__main__":
    unittest.main()
