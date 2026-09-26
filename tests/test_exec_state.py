"""The state hook and its readers, hardened. The hook runs inside every remote session's
turn: its session id is confined to one filename, its dir is private (0700, ours, not a
symlink), its writes are atomic, it prunes what no reader can see, it never prints, and a
payload it can't use is dropped. The readers (launcher /status, the shell prompt) skip any
file of the wrong kind or shape instead of raising or blocking on it."""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import rc_sessions
import rc_state
import rc_state_hook
import rc_status

from tests._harness import env, keep, restore_globals


class HookHardeningTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.dir = self.base / "state"
        keep(self, (rc_state_hook, "STATE_DIR"), (rc_state_hook, "SETTINGS"))
        rc_state_hook.STATE_DIR = self.dir

    def _hook(self, payload, raw=None):
        buf, old = io.StringIO(), sys.stdin
        sys.stdin = io.StringIO(raw if raw is not None else json.dumps(payload))
        try:
            with redirect_stdout(buf):
                rc_state_hook.main()
        finally:
            sys.stdin = old
        self.assertEqual(buf.getvalue(), "")  # hook stdout becomes model context
        return buf.getvalue()

    def _files(self, d=None):
        return sorted(p.name for p in (d or self.dir).iterdir())

    def test_session_id_cannot_escape_the_state_dir(self):
        victim = self.base / "victim.json"
        victim.write_text("{}")
        for sid in ("../victim", "../../etc/x", "a/b", "..", "x\x00y"):
            self._hook({"hook_event_name": "Stop", "session_id": sid})
            self._hook({"hook_event_name": "SessionEnd", "session_id": sid})
        self.assertTrue(victim.exists())  # SessionEnd's unlink never reached it
        self._hook({"hook_event_name": "Stop", "session_id": "../victim"})
        (only,) = self._files()
        self.assertTrue(only.startswith("h-") and only.endswith(".json"))
        self.assertEqual(
            sorted(p.name for p in self.base.iterdir()), ["state", "victim.json"]
        )

    def test_plain_ids_keep_their_readable_name(self):
        self._hook({"hook_event_name": "Stop", "session_id": "0f3e-uuid_1"})
        env(self, RC_REMOTE="rc-work+aws")
        self._hook({"hook_event_name": "Stop"})  # falls back to the tmux session name
        self.assertEqual(self._files(), ["0f3e-uuid_1.json", "rc-work+aws.json"])

    def test_dir_is_private_and_files_are_0600_with_no_temp_left(self):
        self._hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "s1.json").st_mode), 0o600)
        self.assertEqual(self._files(), ["s1.json"])  # the atomic temp was renamed away

    def test_an_existing_loose_dir_is_tightened(self):
        self.dir.mkdir(mode=0o755)
        os.chmod(self.dir, 0o755)
        self._hook({"hook_event_name": "Stop", "session_id": "s1"})
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)

    def test_a_symlinked_state_dir_gets_no_writes(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.dir.symlink_to(elsewhere)
        self._hook({"hook_event_name": "Stop", "session_id": "s1"})
        self.assertEqual(self._files(elsewhere), [])

    def test_a_planted_symlink_at_the_state_file_is_replaced_not_followed(self):
        self.dir.mkdir(mode=0o700)
        target = self.base / "target.txt"
        target.write_text("keep")
        (self.dir / "s1.json").symlink_to(target)
        self._hook({"hook_event_name": "Stop", "session_id": "s1"})
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse((self.dir / "s1.json").is_symlink())

    def test_session_start_prunes_what_no_reader_can_see(self):
        self.dir.mkdir(mode=0o700)
        old = self.dir / "dead.json"
        old.write_text("{}")
        stale = time.time() - rc_state.STATE_TTL - 60
        os.utime(old, (stale, stale))
        fresh = self.dir / "live.json"
        fresh.write_text("{}")
        self._hook({"hook_event_name": "Stop", "session_id": "s1"})
        self.assertTrue(old.exists())  # only SessionStart pays for the sweep
        self._hook({"hook_event_name": "SessionStart", "session_id": "s2"})
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_unusable_payloads_are_dropped_quietly(self):
        for raw in ("not json", "[1, 2]", "null", '"str"'):
            self._hook(None, raw=raw)
        self._hook({"hook_event_name": "Notification", "session_id": "s", "message": 5})
        self.assertEqual(
            json.loads((self.dir / "s.json").read_text())["state"], "waiting"
        )
        self._hook({"hook_event_name": "Stop", "session_id": "c", "cwd": ["x"]})
        self.assertEqual(
            json.loads((self.dir / "c.json").read_text())["cwd"], os.getcwd()
        )

    def test_hook_command_quotes_the_script_path(self):
        self.assertEqual(
            rc_state_hook.hook_command("/r"),
            '[ -n "$RC_REMOTE" ] && python3 /r/rc_state_hook.py; true',
        )  # a plain path is unchanged, so old registrations still match for removal
        cmd = rc_state_hook.hook_command("/my dir/$(touch x)")
        self.assertIn("'/my dir/$(touch x)/rc_state_hook.py'", cmd)

    def test_settings_write_is_atomic_keeps_mode_and_symlink(self):
        real = self.base / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"theme": "dark"}))
        os.chmod(real, 0o640)
        link = self.base / "settings.json"
        link.symlink_to(real)
        rc_state_hook.SETTINGS = str(link)
        rc_state_hook.install_hook("/r")
        self.assertTrue(link.is_symlink())  # the dotfile link survives
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o640)
        d = json.loads(real.read_text())
        self.assertEqual(d["theme"], "dark")
        self.assertIn("SessionEnd", d["hooks"])
        self.assertEqual(sorted(os.listdir(real.parent)), ["settings.json"])  # no temp

    def test_new_settings_file_is_0600(self):
        rc_state_hook.SETTINGS = str(self.base / "new" / "settings.json")
        rc_state_hook.install_hook("/r")
        self.assertEqual(stat.S_IMODE(os.stat(rc_state_hook.SETTINGS).st_mode), 0o600)

    def test_settings_write_failure_leaves_the_file_and_no_temp(self):
        p = self.base / "settings.json"
        p.write_text("{}")
        rc_state_hook.SETTINGS = str(p)
        keep(self, (rc_state_hook.os, "replace"))

        def boom(*a):
            raise OSError("disk full")

        rc_state_hook.os.replace = boom
        with self.assertRaises(OSError):
            rc_state_hook.install_hook("/r")
        self.assertEqual(p.read_text(), "{}")
        self.assertEqual(sorted(os.listdir(self.base)), ["settings.json"])


class ReaderHardeningTest(unittest.TestCase):
    def setUp(self):
        restore_globals(self)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        rc_sessions.STATE_DIR = self.dir
        self.good = {"state": "working", "project": "p", "cwd": "/tmp/p"}

    def _put(self, name, obj):
        (self.dir / name).write_text(obj if isinstance(obj, str) else json.dumps(obj))

    def test_wrong_shapes_are_skipped_not_raised(self):
        now = time.time()
        self._put("list.json", [1, 2])
        self._put("liststate.json", {"state": ["working"], "ts": now, "project": "x"})
        self._put("strts.json", {"state": "working", "ts": "now", "project": "x"})
        self._put("boolts.json", {"state": "working", "ts": True, "project": "x"})
        self._put("infts.json", '{"state":"working","ts":Infinity,"project":"x"}')
        self._put("nants.json", '{"state":"working","ts":NaN,"project":"x"}')
        self._put("listproj.json", {"state": "working", "ts": now, "project": ["x"]})
        (self.dir / "bin.json").write_bytes(b"\xff\xfe\x00junk")
        self._put("ok.json", self.good | {"ts": now})
        self.assertEqual(rc_sessions.session_states(), {"p": "working"})  # no 500

    def test_a_fifo_never_blocks_the_reader(self):
        os.mkfifo(self.dir / "trap.json")  # a plain open() would hang here forever
        self._put("ok.json", self.good | {"ts": time.time()})
        self.assertEqual(len(rc_state.valid_states(self.dir)), 1)

    def test_symlinks_and_oversized_files_are_skipped(self):
        real = self.dir.parent / f"{self.dir.name}-real.json"
        real.write_text(json.dumps(self.good | {"ts": time.time()}))
        self.addCleanup(real.unlink)
        (self.dir / "link.json").symlink_to(real)
        big = self.good | {"ts": time.time(), "pad": "x" * rc_state.MAX_STATE_BYTES}
        self._put("big.json", big)
        self.assertEqual(rc_state.valid_states(self.dir), [])

    def test_status_ignores_an_empty_cwd(self):
        # Path("") is ".", which would tag every prompt everywhere
        here = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, here, True)
        self.assertFalse(rc_status.shares_tree(here, ""))


if __name__ == "__main__":
    unittest.main()
