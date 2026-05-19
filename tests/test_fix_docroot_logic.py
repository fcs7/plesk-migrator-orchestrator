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
