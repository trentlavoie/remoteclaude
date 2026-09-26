"""git state for the launcher page — the exact branch/dirty parse, the two ways it must
degrade rather than 500 a page load (a missing binary, a hung `git status`), the TTL cache
on both sides, and the opt-in RC_SNAPSHOT checkpoint. These run against real throwaway
repos: the parse is against git's actual porcelain=v2 output, not a fixture of it."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import rc_config
import rc_git

from tests._harness import MockedToolsCase, env, proc, respond, restore_globals


class GitStateTest(unittest.TestCase):
    def setUp(self):
        restore_globals(self)
        self.tmp = tempfile.mkdtemp()
        rc_config.PARENT = os.path.join(self.tmp, "projects")
        os.makedirs(rc_config.PARENT)
        rc_config.log_event = lambda *a: None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _proj(self, name: str) -> str:
        p = os.path.join(rc_config.PARENT, name)
        os.makedirs(p, exist_ok=True)
        return p

    def _git_repo(self, name: str, branch: str = "main") -> str:
        path = self._proj(name)

        def g(*a):
            subprocess.run(
                [rc_config.GIT, "--no-optional-locks", "-C", path, *a],
                capture_output=True,
                check=True,
            )

        g("init", "-q")
        Path(path, "f.txt").write_text("x")
        g("add", "f.txt")
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
        # force a known branch so the parse can be asserted exactly
        g("branch", "-M", branch)
        return path

    def test_git_state_reports_exact_branch_and_dirty(self):
        path = self._git_repo("repo", branch="feat/x")
        st = rc_git._git_state("repo")
        # exact branch — a wrong prefix parse (e.g. branch.oid) fails
        self.assertEqual(st["b"], "feat/x")
        self.assertFalse(st["d"])  # clean right after the commit
        Path(path, "f.txt").write_text("changed")
        self.assertTrue(rc_git._git_state("repo")["d"])  # a tracked edit reads dirty

    def test_git_state_untracked_counts_as_dirty(self):
        path = self._git_repo("repo")
        self.assertFalse(rc_git._git_state("repo")["d"])  # clean baseline
        Path(path, "new.txt").write_text("z")  # untracked-only, nothing staged
        # porcelain '?' line -> dirty, per the docstring
        self.assertTrue(rc_git._git_state("repo")["d"])

    def test_git_state_none_for_nonrepo(self):
        self._proj("plain")
        self.assertIsNone(rc_git._git_state("plain"))  # a bare dir isn't a work tree

    def test_git_state_missing_git_is_none_not_crash(self):
        self._git_repo("repo")
        # simulate the missing binary under launchd PATH
        rc_config.GIT = "/nonexistent/git"
        # OSError swallowed -> None, page() won't 500
        self.assertIsNone(rc_git._git_state("repo"))

    def test_git_state_timeout_is_none_not_crash(self):
        self._git_repo("repo")

        seen = {}

        def boom(*a, **k):  # a hung `git status` hitting the timeout
            seen.update(k)
            raise subprocess.TimeoutExpired("git", rc_config.GIT_STATUS_TIMEOUT)

        subprocess.run = boom  # restored by restore_globals()
        # TimeoutExpired is NOT an OSError; if the guard is narrowed to `except OSError:` this
        # raises through git_states() -> page() and 500s the launcher. It must return None.
        self.assertIsNone(rc_git._git_state("repo"))
        # ...and the knob itself must be REQUESTED: dropping timeout= from the call re-ships
        # the worker hang while this mock still raises unconditionally.
        self.assertEqual(seen.get("timeout"), rc_config.GIT_STATUS_TIMEOUT)

    def test_git_states_maps_only_repos(self):
        self._git_repo("repo")
        self._proj("plain")
        got = rc_git.git_states()
        self.assertIn("repo", got)  # a repo -> present with branch/dirty
        self.assertNotIn("plain", got)  # non-repo -> dropped

    def test_git_cache_expires_after_ttl(self):
        repo = self._git_repo("repo")
        rc_config.GIT_TTL = 0.0  # every entry expires immediately
        self.assertFalse(rc_git.git_states()["repo"]["d"])
        Path(repo, "f.txt").write_text("changed")
        # expiry must force a rescan; freezing the deadline check would pin badges
        # to launch-time state until a launcher restart
        self.assertTrue(rc_git.git_states()["repo"]["d"])

    def test_git_states_serves_stale_from_cache(self):
        repo = self._git_repo("repo")
        self.assertFalse(rc_git.git_states()["repo"]["d"])  # clean, now cached
        Path(repo, "f.txt").write_text("changed")  # dirty it AFTER the cache fill
        # a fresh _git_state would report dirty; git_states must return the cached clean value,
        # which only holds if the cache READ path (not just the write) actually runs.
        self.assertFalse(rc_git.git_states()["repo"]["d"])

    def test_snapshot_off_by_default(self):
        self.assertIsNone(rc_git.snapshot("repo"))  # RC_SNAPSHOT unset -> None

    def test_snapshot_creates_ref_when_enabled(self):
        path = self._proj("repo")
        env(
            self,
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@t",
        )

        def git(*a):
            return subprocess.run(
                ["git", "--no-optional-locks", "-C", path, *a],
                capture_output=True,
                text=True,
            )

        git("init", "-q")
        Path(path, "f.txt").write_text("a")
        git("add", "f.txt")
        git("commit", "-q", "-m", "init")
        Path(path, "f.txt").write_text("b")  # a dirty tracked change for `stash create`
        env(self, RC_SNAPSHOT="1")
        ref = rc_git.snapshot("repo")
        self.assertIsNotNone(ref)
        self.assertTrue(ref.startswith("refs/rc-snapshots/repo/"))
        self.assertEqual(git("rev-parse", "--verify", ref).returncode, 0)


class SnapshotBranchTest(MockedToolsCase):
    """snapshot()'s refusals and its text= modes, on canned git output — the cases a real
    repo can't stage cheaply (a non-repo, a clean tree, an undecodable stderr)."""

    def test_snapshot_not_a_repo(self):
        env(self, RC_SNAPSHOT="1")
        self.responses = {"is-inside-work-tree": proc(returncode=1)}
        self.assertIsNone(rc_git.snapshot("proj"))

    def test_snapshot_clean_tree_returns_none(self):
        env(self, RC_SNAPSHOT="1")
        self.responses = {
            "is-inside-work-tree": proc(returncode=0),
            "stash create": proc(stdout="\n"),
        }
        self.assertIsNone(rc_git.snapshot("proj"))

    def test_snapshot_returncode_only_git_calls_stay_bytes(self):
        # Gate lead: routing rev-parse/update-ref through _git() silently added text=True,
        # so an undecodable byte in git's stderr would raise UnicodeDecodeError out of
        # launch(); those two only read .returncode and must not decode. `stash create`
        # reads stdout and legitimately decodes.
        seen = []

        def run(cmd, **kw):
            seen.append((cmd, kw.get("text")))
            return respond(cmd, self.desk, self.responses)

        subprocess.run = run
        self.responses = {"stash create": proc(stdout="abc123\n")}
        env(self, RC_SNAPSHOT="1")
        rc_git.snapshot("proj")
        # the subcommand follows `-C <path>` (the -c safety pins sit before it)
        modes = {
            c[c.index("-C") + 2]: text for c, text in seen if c[0] == rc_config.GIT
        }
        self.assertEqual(modes.get("rev-parse"), False)
        self.assertEqual(modes.get("update-ref"), False)
        self.assertEqual(modes.get("stash"), True)


if __name__ == "__main__":
    unittest.main()
