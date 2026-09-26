"""The ~/.claude.json trust write (rc_claude.trust_dir): claude's own hot, OAuth-bearing file,
rewritten by every running session — so the launcher's one-flag write must be atomic, keep
0600, go through a symlinked dotfile, and never clobber a concurrent claude write."""

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

import rc_claude

from tests._harness import keep


class TrustWriteTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cfg = self.d / ".claude.json"

    def _write(self, obj, mode=0o600):
        self.cfg.write_text(json.dumps(obj))
        os.chmod(self.cfg, mode)

    def _temps(self):
        return [f for f in os.listdir(self.d) if ".rc" in f]

    def test_sets_only_the_missing_flag_and_keeps_0600(self):
        self._write({"oauthAccount": {"x": 1}, "projects": {"/p": {"k": 1}}})
        self.assertIsNone(rc_claude.trust_dir(str(self.cfg), "/p"))
        d = json.loads(self.cfg.read_text())
        self.assertTrue(d["projects"]["/p"]["hasTrustDialogAccepted"])
        self.assertEqual(d["projects"]["/p"]["k"], 1)
        self.assertEqual(d["oauthAccount"], {"x": 1})
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
        mtime = os.stat(self.cfg).st_mtime_ns
        self.assertIsNone(rc_claude.trust_dir(str(self.cfg), "/p"))  # already: no write
        self.assertEqual(os.stat(self.cfg).st_mtime_ns, mtime)

    def test_a_concurrent_claude_write_is_merged_not_clobbered(self):
        # claude rewrites the file between our read and our replace: the optimistic check
        # must notice, re-read, and land BOTH changes
        self._write({"projects": {}})
        real_fsync, hits = os.fsync, []

        def racing_fsync(fd):
            real_fsync(fd)
            if not hits:
                hits.append(1)
                tmp = self.d / "claude-own.tmp"
                tmp.write_text(json.dumps({"projects": {}, "numStartups": 42}))
                os.replace(tmp, self.cfg)  # claude's own atomic rewrite

        keep(self, (rc_claude.os, "fsync"))
        rc_claude.os.fsync = racing_fsync
        self.assertIsNone(rc_claude.trust_dir(str(self.cfg), "/p"))
        d = json.loads(self.cfg.read_text())
        self.assertEqual(d["numStartups"], 42)  # claude's write survived
        self.assertTrue(d["projects"]["/p"]["hasTrustDialogAccepted"])
        self.assertEqual(self._temps(), [])

    def test_a_file_that_never_settles_is_left_alone(self):
        self._write({"projects": {}})
        keep(self, (rc_claude, "_stamp"))
        n = iter(range(10**6))
        rc_claude._stamp = lambda p: (next(n), 0, 0)  # different on every look
        why = rc_claude.trust_dir(str(self.cfg), "/p")
        self.assertIn("kept changing", why)
        self.assertEqual(json.loads(self.cfg.read_text()), {"projects": {}})
        self.assertEqual(self._temps(), [])

    def test_a_symlinked_dotfile_is_written_through_not_replaced(self):
        real = self.d / "dotfiles" / "claude.json"
        real.parent.mkdir()
        real.write_text("{}")
        os.chmod(real, 0o600)
        self.cfg.symlink_to(real)
        self.assertIsNone(rc_claude.trust_dir(str(self.cfg), "/p"))
        self.assertTrue(self.cfg.is_symlink())
        self.assertIn("/p", json.loads(real.read_text())["projects"])

    def test_unexpected_shapes_are_reported_and_untouched(self):
        for obj in ([1], {"projects": []}, {"projects": {"/p": "yes"}}):
            self._write(obj)
            self.assertIn("unexpected shape", rc_claude.trust_dir(str(self.cfg), "/p"))
            self.assertEqual(json.loads(self.cfg.read_text()), obj)

    def test_non_utf8_is_a_skip_not_a_500(self):
        self.cfg.write_bytes(b"\xff\xfe{")
        self.assertIn("skip", rc_claude.trust_dir(str(self.cfg), "/p"))


if __name__ == "__main__":
    unittest.main()
