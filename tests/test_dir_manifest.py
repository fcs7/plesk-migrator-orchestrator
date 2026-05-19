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
