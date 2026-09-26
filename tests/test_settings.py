"""rc_settings: the persisted fork/worktree toggles, their resolution to RESUME/SPAWN,
and the /settings route that writes them."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import rc_settings

from tests._harness import TOKEN, ServerCase


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("RESUME", "SPAWN", "SETTINGS_FILE"):
            self.addCleanup(setattr, rc_settings, name, getattr(rc_settings, name))
        rc_settings.RESUME, rc_settings.SPAWN = "continue", "same-dir"
        rc_settings.SETTINGS_FILE = Path(self.tmp, "settings.json")

    def test_absent_file_falls_back_to_env_defaults(self):
        self.assertEqual(
            (rc_settings.resume(), rc_settings.spawn()), ("continue", "same-dir")
        )

    def test_toggle_roundtrip_persists_and_resolves(self):
        self.assertEqual(rc_settings.set_toggle("fork", True), ("set", None))
        self.assertEqual(rc_settings.set_toggle("worktree", True), ("set", None))
        # persisted to disk, so it survives a restart (a fresh read of the file)
        self.assertEqual(
            json.loads(rc_settings.SETTINGS_FILE.read_text()),
            {"fork": True, "worktree": True},
        )
        self.assertEqual(
            (rc_settings.resume(), rc_settings.spawn()), ("fork", "worktree")
        )
        # flipping fork off maps to "continue" (the explicit off value), not the env default
        rc_settings.RESUME = (
            "off"  # would leak through if _toggled ignored a present key
        )
        rc_settings.set_toggle("fork", False)
        self.assertEqual(rc_settings.resume(), "continue")

    def test_unknown_toggle_rejected_and_not_written(self):
        self.assertEqual(rc_settings.set_toggle("nope", True)[0], "badname")
        self.assertFalse(rc_settings.SETTINGS_FILE.exists())

    def test_write_failure_cleans_up_temp_and_reports_failed(self):
        # os.replace fails after mkstemp already made the temp: set_toggle must unlink it
        # (no orphaned settings.* left) and report "failed", never a half-written file
        def boom(*_a):
            raise OSError(28, "disk full")  # ENOSPC, as a real write reports it

        self.addCleanup(setattr, os, "replace", os.replace)
        os.replace = boom
        status, reason = rc_settings.set_toggle("fork", True)
        self.assertEqual(status, "failed")
        self.assertIn("disk full", reason)
        self.assertEqual(list(rc_settings.SETTINGS_FILE.parent.glob("settings.*")), [])

    def test_corrupt_file_reads_as_defaults(self):
        rc_settings.SETTINGS_FILE.write_text("{not json")
        self.assertEqual(rc_settings.resume(), "continue")  # tolerated, not a crash

    def test_non_utf8_and_malformed_values_read_as_defaults(self):
        # read_text() raises UnicodeDecodeError (a ValueError) on non-UTF8 bytes; the
        # "torn file never breaks a launch" promise must cover that, not just bad JSON
        rc_settings.SETTINGS_FILE.write_bytes(b"\xff\xfe not utf8")
        self.assertEqual(rc_settings.resume(), "continue")
        # a valid JSON object with a non-bool value must not flip a toggle via truthiness
        rc_settings.SETTINGS_FILE.write_text(json.dumps({"fork": "yes"}))
        self.assertEqual(rc_settings.resume(), "continue")

    def test_concurrent_set_toggle_keeps_both_keys(self):
        # two /settings taps land in two ThreadingHTTPServer threads; without the lock the
        # later os.replace drops the other toggle (read-modify-write race)
        import threading

        start = threading.Barrier(2)

        def flip(name):
            start.wait()
            rc_settings.set_toggle(name, True)

        ts = [threading.Thread(target=flip, args=(n,)) for n in ("fork", "worktree")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(
            json.loads(rc_settings.SETTINGS_FILE.read_text()),
            {"fork": True, "worktree": True},
        )


SAME = {"Sec-Fetch-Site": "same-origin"}  # what the page's fetch() sends


class SettingsRouteTest(ServerCase):
    def test_settings_route_persists_and_shows_in_status(self):
        st, _, body = self.req(
            "POST", f"/settings?name=fork&on=1&token={TOKEN}", headers=SAME
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["status"], "set")
        # the live status the page polls now reports fork on
        _, _, s = self.req("GET", f"/status?token={TOKEN}")
        self.assertTrue(json.loads(s)["settings"]["fork"])

    def test_settings_route_rejects_unknown_name(self):
        _, _, body = self.req(
            "POST", f"/settings?name=bogus&on=1&token={TOKEN}", headers=SAME
        )
        self.assertEqual(json.loads(body)["status"], "badname")

    def test_worktree_on_reports_fork_off_even_when_fork_set(self):
        # both toggles independent, but a worktree launch never resumes, so fork can't apply.
        # status must not advertise fork active while worktree is on — or the UI lies.
        self.req("POST", f"/settings?name=fork&on=1&token={TOKEN}", headers=SAME)
        self.req("POST", f"/settings?name=worktree&on=1&token={TOKEN}", headers=SAME)
        _, _, s = self.req("GET", f"/status?token={TOKEN}")
        st = json.loads(s)["settings"]
        self.assertEqual(st, {"fork": False, "worktree": True})


if __name__ == "__main__":
    unittest.main()
