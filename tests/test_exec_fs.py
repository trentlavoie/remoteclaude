"""Filesystem and argv hardening: project creation, the git calls the /status poll makes in
every repo, and the opt-in permission-mode pin."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import rc_config
import rc_git
import rc_sessions
import rc_settings

from tests._harness import MockedToolsCase, env, keep, restore_globals


class CreateHardeningTest(unittest.TestCase):
    def setUp(self):
        restore_globals(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        rc_config.PARENT = os.path.join(self.tmp, "projects")
        rc_config.ROOTS_FILE = Path(self.tmp, "roots.json")
        os.makedirs(rc_config.PARENT)
        rc_config.log_event = lambda *a: None

    def test_trailing_newline_and_overlong_names_are_badnames(self):
        # NAME_RE.match's `$` matched before a trailing "\n" (/create?proj=x%0A)
        for bad in ("x\n", "x\n\n", "a" * (rc_sessions.MAX_NAME + 1), "é", "x\t", ""):
            self.assertEqual(rc_sessions.create(bad)[0], "badname", repr(bad))
        self.assertEqual(os.listdir(rc_config.PARENT), [])
        self.assertEqual(rc_sessions.create("a" * rc_sessions.MAX_NAME)[0], "created")

    def test_unwritable_parent_is_failed_not_a_500(self):
        rc_config.PARENT = os.path.join(self.tmp, "a-file")
        Path(rc_config.PARENT).write_text("")
        status, reason = rc_sessions.create("proj")
        self.assertEqual(status, "failed")
        self.assertTrue(reason)

    def test_missing_or_hung_git_still_creates(self):
        keep(self, (rc_config, "GIT"))
        rc_config.GIT = "/nonexistent/git"
        self.assertEqual(rc_sessions.create("nogit"), ("created", None))
        self.assertTrue(Path(rc_config.PARENT, "nogit", "CLAUDE.md").exists())
        keep(self, (subprocess, "run"))
        seen = {}

        def hang(cmd, **kw):
            seen.update(kw)
            raise subprocess.TimeoutExpired(cmd, 1)

        subprocess.run = hang
        self.assertEqual(rc_sessions.create("hung"), ("created", None))
        self.assertIn("timeout", seen)


class GitNoRepoExecTest(unittest.TestCase):
    def setUp(self):
        restore_globals(self)
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        rc_config.PARENT = os.path.join(self.tmp, "projects")
        self.repo = os.path.join(rc_config.PARENT, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.canary = os.path.join(self.tmp, "PWNED")

    def test_a_planted_fsmonitor_does_not_run_on_the_status_poll(self):
        # repo-local config is writable by anything working in the repo; the launcher's
        # every-30s `git status` must not execute it outside any permission prompt
        hook = f"touch {self.canary} #"
        subprocess.run(["git", "-C", self.repo, "config", "core.fsmonitor", hook])
        subprocess.run(["git", "-C", self.repo, "status"], capture_output=True)
        self.assertTrue(os.path.exists(self.canary), "control: plain git runs it")
        os.unlink(self.canary)
        self.assertIsNotNone(rc_git._git_state("repo"))
        self.assertFalse(os.path.exists(self.canary))

    def test_snapshot_runs_no_repo_hooks(self):
        env(self, RC_SNAPSHOT="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t")
        env(self, GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        g = ["git", "-C", self.repo]
        Path(self.repo, "f").write_text("a")
        subprocess.run([*g, "add", "f"], check=True)
        subprocess.run([*g, "commit", "-qm", "i"], check=True, capture_output=True)
        Path(self.repo, "f").write_text("b")
        hook = Path(self.repo, ".git", "hooks", "reference-transaction")
        hook.write_text(f"#!/bin/sh\ntouch {self.canary}\n")
        hook.chmod(0o755)
        self.assertIsNotNone(rc_git.snapshot("repo"))
        self.assertFalse(os.path.exists(self.canary))


class SnapshotDegradesTest(MockedToolsCase):
    def test_missing_or_hung_git_is_no_snapshot_not_a_failed_launch(self):
        env(self, RC_SNAPSHOT="1")
        for exc in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 1)):
            seen = {}

            def boom(cmd, _e=exc, **kw):
                seen.update(kw)
                raise _e

            subprocess.run = boom
            self.assertIsNone(rc_git.snapshot("proj"))
            self.assertEqual(seen.get("timeout"), rc_git.SNAPSHOT_TIMEOUT)


class PermissionModeTest(unittest.TestCase):
    def setUp(self):
        keep(
            self,
            (rc_settings, "PERMISSION_MODE"),
            (rc_settings, "RESUME"),
            (rc_settings, "SPAWN"),
            (rc_settings, "SETTINGS_FILE"),
        )
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        rc_settings.SETTINGS_FILE = Path(d, "settings.json")

    def test_parse_never_yields_bypass(self):
        p = rc_settings._permission_mode
        self.assertEqual(p(""), "")  # unset: no flag, upstream behavior
        self.assertEqual(p("plan"), "plan")
        self.assertEqual(p("acceptEdits"), "acceptEdits")
        for bad in ("bypassPermissions", "dangerously-skip", "acceptedits", "x y"):
            self.assertEqual(p(bad), "default", bad)  # fails closed

    def test_env_is_read_at_import(self):
        code = "import rc_settings; print(rc_settings.PERMISSION_MODE)"
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(rc_settings.__file__)),
            env={**os.environ, "RC_PERMISSION_MODE": "bypassPermissions"},
        ).stdout.strip()
        self.assertEqual(out, "default")

    def _forms(self):
        out = []
        for resume, spawn in (("continue", "same-dir"), ("off", "same-dir")):
            rc_settings.RESUME, rc_settings.SPAWN = resume, spawn
            out.append(rc_sessions.launch_cmd("p")[0])
        rc_settings.SPAWN = "worktree"
        out.append(rc_sessions.launch_cmd("p")[0])
        return out

    def test_pin_rides_every_launch_form_in_the_right_place(self):
        rc_settings.PERMISSION_MODE = "plan"
        resume, fresh, sub = self._forms()
        for cmd in (resume, fresh):  # a top-level flag, before --remote-control
            i = cmd.index("--permission-mode")
            self.assertEqual(cmd[i + 1], "plan")
            self.assertLess(i, cmd.index("--remote-control"))
        i = sub.index("--permission-mode")  # a subcommand option, after remote-control
        self.assertGreater(i, sub.index("remote-control"))
        self.assertEqual(sub[i + 1], "plan")

    def test_no_launch_form_ever_carries_a_bypass(self):
        for mode in ("", "default", "plan"):
            rc_settings.PERMISSION_MODE = mode
            for cmd in self._forms():
                self.assertNotIn("bypassPermissions", cmd)
                self.assertNotIn("--dangerously-skip-permissions", cmd)
                self.assertNotIn("--allow-dangerously-skip-permissions", cmd)
                self.assertEqual("--permission-mode" in cmd, bool(mode))


if __name__ == "__main__":
    unittest.main()


class NameReTest(unittest.TestCase):
    def test_trailing_newline_is_not_a_valid_name(self):
        # `$` matches before a final newline; `\Z` doesn't. listings/project_dir use .match
        self.assertIsNone(rc_config.NAME_RE.match("proj\n"))
        self.assertIsNotNone(rc_config.NAME_RE.match("proj"))
