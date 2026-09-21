"""Tests for the credential rotation helper (temp files only, never .env)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.rotate_credentials import (
    apply_rotation,
    fingerprint,
    generate_values,
    prune_backups,
    resolve_bot_endpoint,
    rewrite_env,
)


class RotationHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="taffy-rotate-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_group_returns_three_high_entropy_values(self) -> None:
        values = generate_values("all")

        self.assertEqual(
            set(values),
            {"ADMIN_API_TOKEN", "ONEBOT_V11_ACCESS_TOKEN", "ONEBOT_V11_SECRET"},
        )
        for value in values.values():
            self.assertEqual(len(value), 43)  # token_urlsafe(32)
            self.assertNotIn("\n", value)

    def test_groups_are_mutually_exclusive(self) -> None:
        self.assertEqual(set(generate_values("admin")), {"ADMIN_API_TOKEN"})
        self.assertEqual(
            set(generate_values("onebot")),
            {"ONEBOT_V11_ACCESS_TOKEN", "ONEBOT_V11_SECRET"},
        )

    def test_generated_values_differ_between_calls(self) -> None:
        self.assertNotEqual(generate_values("all"), generate_values("all"))

    def test_fingerprint_never_contains_the_full_secret(self) -> None:
        value = generate_values("admin")["ADMIN_API_TOKEN"]

        rendered = fingerprint(value)

        self.assertNotIn(value, rendered)
        self.assertTrue(rendered.startswith(value[:4]))
        self.assertIn("sha256:", rendered)

    def test_rewrite_replaces_existing_keys_and_keeps_other_lines(self) -> None:
        env = self.tmp / ".env"
        env.write_text(
            "# comment\nADMIN_API_TOKEN=old-admin\nTAFFY_LEARNING_ENABLED=false\n"
            "ONEBOT_V11_ACCESS_TOKEN=old-access\n",
            encoding="utf-8",
        )
        changed = rewrite_env(
            env,
            {"ADMIN_API_TOKEN": "new-admin", "ONEBOT_V11_ACCESS_TOKEN": "new-access"},
        )

        self.assertEqual(changed, 2)
        text = env.read_text(encoding="utf-8")
        self.assertIn("ADMIN_API_TOKEN=new-admin", text)
        self.assertIn("ONEBOT_V11_ACCESS_TOKEN=new-access", text)
        self.assertIn("TAFFY_LEARNING_ENABLED=false", text)
        self.assertIn("# comment", text)
        self.assertNotIn("old-admin", text)

    def test_rewrite_appends_missing_keys(self) -> None:
        env = self.tmp / ".env"
        env.write_text("TAFFY_LEARNING_ENABLED=false\n", encoding="utf-8")
        changed = rewrite_env(env, {"ADMIN_API_TOKEN": "new-admin"})

        self.assertEqual(changed, 1)
        text = env.read_text(encoding="utf-8")
        self.assertIn("ADMIN_API_TOKEN=new-admin", text)
        self.assertIn("TAFFY_LEARNING_ENABLED=false", text)

    def test_rewrite_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            rewrite_env(self.tmp / "missing.env", {"ADMIN_API_TOKEN": "x"})

    def test_apply_backs_up_before_writing_and_prunes_old_backups(self) -> None:
        backup_dir = self.tmp / "backups"
        backup_dir.mkdir()
        for index in range(3):
            stale = backup_dir / f"env-backup-old-{index}"
            stale.write_text(f"old {index}", encoding="utf-8")
            # Distinct mtimes so mtime-based pruning has a deterministic order
            # (all strictly in the past, older than the fresh backup).
            stamp = 1_700_000_000 + index * 3_600
            os.utime(stale, (stamp, stamp))

        env = self.tmp / ".env"
        env.write_text("ADMIN_API_TOKEN=old-admin\n", encoding="utf-8")
        backup = apply_rotation(env, backup_dir, {"ADMIN_API_TOKEN": "new-admin"})

        self.assertTrue(backup.exists())
        self.assertIn("old-admin", backup.read_text(encoding="utf-8"))
        self.assertIn("ADMIN_API_TOKEN=new-admin", env.read_text(encoding="utf-8"))
        # Newest two entries remain: the fresh backup plus one old one.
        remaining = sorted(path.name for path in backup_dir.glob("env-backup-*"))
        self.assertEqual(len(remaining), 2)
        self.assertIn(backup.name, remaining)

    def test_prune_backups_without_directory_is_a_noop(self) -> None:
        self.assertEqual(prune_backups(self.tmp / "absent"), 0)

    def test_bot_endpoint_resolution_from_env_file(self) -> None:
        env = self.tmp / ".env"
        env.write_text('HOST=0.0.0.0\nPORT="9090"\n', encoding="utf-8")

        host, port = resolve_bot_endpoint(env)

        # Wildcard listen addresses are probed through loopback.
        self.assertEqual((host, port), ("127.0.0.1", 9090))

        env.write_text("PORT=not-a-number\n", encoding="utf-8")
        host, port = resolve_bot_endpoint(env)
        self.assertEqual((host, port), ("127.0.0.1", 8080))

        host, port = resolve_bot_endpoint(self.tmp / "missing.env")
        self.assertEqual((host, port), ("127.0.0.1", 8080))


if __name__ == "__main__":
    unittest.main()
