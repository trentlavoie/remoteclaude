"""Unit tests for the launcher's filesystem/logic functions — project discovery, the
create/trust flow, the session-state rank+TTL filter, breadcrumb/size rendering and the
one-pass template fill. Every filesystem-touching global is redirected to a tmp dir; the
tmux/process-orchestration paths (launch/stop/spawn/takeover) are left to integration, git
state to test_git.py."""

import io
import json
import os
import shutil
import socketserver
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import sys
import subprocess
import rc_claude
import rc_config
import rc_desk
import rc_git
import rc_launcher
import rc_sessions
import rc_share
import rc_state
import rc_templates
import rc_tmux

from tests._harness import env, restore_globals

_REAL_LOG = rc_config.log_event  # captured before any test stubs it


class FunctionTest(unittest.TestCase):
    def setUp(self):
        restore_globals(self)
        self.tmp = tempfile.mkdtemp()
        rc_config.PARENT = os.path.join(self.tmp, "projects")
        rc_config.ROOTS_FILE = Path(self.tmp, "roots.json")
        os.makedirs(rc_config.PARENT)
        rc_sessions.STATE_DIR = Path(self.tmp, "state")
        rc_config.CLAUDE_JSON = os.path.join(self.tmp, "claude.json")
        rc_config.log_event = lambda *a: None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parent_resolves_a_symlinked_projects_parent_to_physical(self):
        # PARENT is realpath'd at import so the trust key matches claude's own
        # physical-getcwd key. Drive a FRESH import with a symlinked RC_PROJECTS_PARENT: a
        # test that sets rc_config.PARENT itself can't see the realpath being dropped at
        # rc_config.py:31. This goes red if that realpath is removed.
        real = os.path.realpath(os.path.join(self.tmp, "real"))
        os.makedirs(real)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        repo = os.path.dirname(os.path.abspath(rc_config.__file__))
        out = subprocess.run(
            [sys.executable, "-c", "import rc_config; print(rc_config.PARENT)"],
            capture_output=True,
            text=True,
            cwd=repo,
            env={**os.environ, "RC_PROJECTS_PARENT": link},
        ).stdout.strip()
        self.assertEqual(out, real)  # resolved to the physical dir
        self.assertNotEqual(out, link)  # not the symlink path

    def _proj(self, name: str) -> str:
        p = os.path.join(rc_config.PARENT, name)
        os.makedirs(p, exist_ok=True)
        return p

    # --- create() ---

    def test_create_makes_dir_git_and_stub(self):
        status, reason = rc_sessions.create("myproj")
        self.assertEqual(status, "created")
        self.assertIsNone(reason)
        root = os.path.join(rc_config.PARENT, "myproj")
        self.assertTrue(os.path.isdir(os.path.join(root, ".git")))
        self.assertEqual(Path(root, "CLAUDE.md").read_text(), "# myproj\n")

    def test_create_rejects_bad_name(self):
        self.assertEqual(rc_sessions.create("../evil")[0], "badname")
        self.assertEqual(rc_sessions.create("a b")[0], "badname")
        # a dot is banned: rc-<name> is a tmux target and "rc-a.b" parses as session.pane,
        # so the session would be untargetable and stop() would report a phantom success
        self.assertEqual(rc_sessions.create("a.b")[0], "badname")
        self.assertEqual(rc_sessions.create(".claude")[0], "badname")
        # must start alphanumeric: leading _ (meta dirs like _archive) and leading - (a
        # shell/tmux arg-injection shape) are rejected; interior _ and - stay valid
        self.assertEqual(rc_sessions.create("_archive")[0], "badname")
        self.assertEqual(rc_sessions.create("-rf")[0], "badname")
        # a category name is not a project: /create bypasses the membership guard, so this
        # would otherwise spawn an unlistable, unstoppable rc-<group> session
        self.addCleanup(setattr, rc_config, "GROUPS", rc_config.GROUPS)
        rc_config.GROUPS = frozenset({"work"})
        self.assertEqual(rc_sessions.create("work")[0], "badname")
        # a root label is likewise not a flat project name
        media = os.path.realpath(
            os.path.join(os.path.dirname(rc_config.PARENT), "media")
        )
        os.makedirs(media)
        rc_config.add_root(media)
        self.assertEqual(rc_sessions.create("media")[0], "badname")
        # even a configured-but-currently-DOWN root reserves its label, so create() can't
        # mkdir PARENT/<label> and permanently shadow it when the root comes back
        import json as _j

        Path(rc_config.ROOTS_FILE).write_text(
            _j.dumps([os.path.join(self.tmp, "gone")])
        )
        self.assertNotIn(
            "gone", rc_config.extra_roots()
        )  # dir doesn't exist -> not valid
        self.assertEqual(rc_sessions.create("gone")[0], "badname")  # but still reserved

    def test_create_refuses_existing(self):
        self._proj("dup")
        self.assertEqual(rc_sessions.create("dup")[0], "exists")

    # --- projects() ---

    def test_projects_lists_valid_dirs_sorted(self):
        self._proj("beta_1")
        self._proj("alpha")
        self._proj("with space")  # NAME_RE rejects the space
        self._proj(".hidden")  # a dot-dir (.claude, .ruff_cache) is not a project
        self._proj("_meta")  # a leading-underscore meta dir (_archive) is not a project
        open(os.path.join(rc_config.PARENT, "afile"), "w").close()  # a file, not a dir
        self.assertEqual(
            rc_config.projects(), ["alpha", "beta_1"]
        )  # beta_1: interior _ ok

    def test_project_groups_are_opt_in_from_env(self):
        # unset -> empty (flat); set -> parsed; empty string -> empty (the install.sh default,
        # which must agree with the code default or the service silently lists flat)
        def groups(env):
            e = {k: v for k, v in os.environ.items() if k != "RC_PROJECT_GROUPS"}
            if env is not None:
                e["RC_PROJECT_GROUPS"] = env
            out = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import rc_config; print(sorted(rc_config.GROUPS))",
                ],
                capture_output=True,
                text=True,
                cwd=os.path.dirname(os.path.abspath(rc_config.__file__)),
                env=e,
            ).stdout.strip()
            return out

        self.assertEqual(groups(None), "[]")  # unset -> flat
        self.assertEqual(
            groups(""), "[]"
        )  # install.sh's empty default -> flat (must match)
        self.assertEqual(groups("work,hobby"), "['hobby', 'work']")  # opt in
        self.assertEqual(
            groups("work, hobby "), "['hobby', 'work']"
        )  # whitespace trimmed

    def test_extra_roots_union_and_collisions(self):
        # env + file union; a basename that is a GROUP, an existing PARENT dir, or a dup drops
        self.addCleanup(setattr, rc_config, "GROUPS", rc_config.GROUPS)
        rc_config.GROUPS = frozenset({"work"})
        os.makedirs(os.path.join(rc_config.PARENT, "alpha"))  # a flat project
        base = os.path.dirname(rc_config.PARENT)
        media = os.path.realpath(os.path.join(base, "media"))
        os.makedirs(media)
        os.makedirs(os.path.realpath(os.path.join(base, "work")))  # basename == a GROUP
        os.makedirs(os.path.realpath(os.path.join(base, "alpha")))  # == a flat project
        Path(rc_config.ROOTS_FILE).write_text(
            json.dumps([media, os.path.join(base, "work"), os.path.join(base, "alpha")])
        )
        self.assertEqual(
            rc_config.extra_roots(), {"media": media}
        )  # only the clean one

    def test_project_dir_resolves_root_and_confines_the_name(self):
        self.addCleanup(setattr, rc_config, "GROUPS", rc_config.GROUPS)
        rc_config.GROUPS = frozenset({"grp"})
        base = os.path.dirname(rc_config.PARENT)
        media = os.path.realpath(os.path.join(base, "media"))
        os.makedirs(media)
        Path(rc_config.ROOTS_FILE).write_text(json.dumps([media]))
        self.assertEqual(
            rc_config.project_dir("media/movie"), os.path.join(media, "movie")
        )
        self.assertEqual(
            rc_config.project_dir("flat"), os.path.join(rc_config.PARENT, "flat")
        )
        self.assertEqual(
            rc_config.project_dir("grp/x"), os.path.join(rc_config.PARENT, "grp/x")
        )
        # a crafted name segment must NOT escape the root — falls back under PARENT
        self.assertEqual(
            rc_config.project_dir("media/../etc"),
            os.path.join(rc_config.PARENT, "media/../etc"),
        )

    def test_add_root_validation_matrix_and_listing(self):
        base = os.path.dirname(rc_config.PARENT)
        good = os.path.join(base, "extra")
        os.makedirs(os.path.join(good, "sub"))
        self.assertEqual(rc_config.add_root(good)[0], "added")
        self.assertEqual(rc_config.add_root(good)[0], "exists")  # idempotent
        self.assertEqual(rc_config.add_root("")[0], "badpath")
        self.assertEqual(rc_config.add_root("/no/such/dir")[0], "badpath")
        self.assertEqual(rc_config.add_root("/")[0], "badpath")  # forbidden
        self.assertEqual(rc_config.add_root("~")[0], "badpath")  # $HOME forbidden
        os.makedirs(os.path.join(rc_config.PARENT, "dup"))  # a flat project
        os.makedirs(os.path.join(base, "dup"))
        self.assertEqual(rc_config.add_root(os.path.join(base, "dup"))[0], "collision")
        self.assertIn("extra/sub", rc_config.projects())  # the added root's child lists
        # a symlinked child of an added root pointing OUTSIDE it must not list (realpath guard)
        os.symlink("/etc", os.path.join(good, "escape"))
        self.assertNotIn("extra/escape", rc_config.projects())
        # nesting a new root inside an existing one is refused (ambiguous label/listing)
        os.makedirs(os.path.join(good, "nested"))
        self.assertEqual(
            rc_config.add_root(os.path.join(good, "nested"))[0], "collision"
        )

    def test_add_root_unlinks_temp_on_write_failure(self):
        base = os.path.dirname(rc_config.PARENT)
        good = os.path.join(base, "wf")
        os.makedirs(good)
        real = rc_sessions.json.dump if False else None  # noqa: F841 (placeholder)
        import rc_config as _c

        orig = _c.json.dump
        _c.json.dump = lambda *a, **k: (_ for _ in ()).throw(OSError("no space"))
        self.addCleanup(setattr, _c.json, "dump", orig)
        self.assertEqual(rc_config.add_root(good)[0], "failed")
        leftover = [
            n for n in os.listdir(rc_config.ROOTS_FILE.parent) if n.startswith("roots.")
        ]
        self.assertEqual(leftover, [])  # temp unlinked

    def test_projects_descends_group_dirs_one_level(self):
        # a dir named in GROUPS is a category (list its children as group/name); any other
        # dir is a flat project and its children are NOT descended; a category never
        # self-lists. Mixed trees (some flat, some grouped) are the transition state.
        self.addCleanup(setattr, rc_config, "GROUPS", rc_config.GROUPS)
        rc_config.GROUPS = frozenset({"work", "hobby"})
        self._proj("flatproj")
        os.makedirs(os.path.join(rc_config.PARENT, "work", "aws"))
        os.makedirs(os.path.join(rc_config.PARENT, "work", "msd"))
        os.makedirs(os.path.join(rc_config.PARENT, "hobby", "pinball"))
        os.makedirs(os.path.join(rc_config.PARENT, "notagroup", "sub"))  # not a group
        self.assertEqual(
            rc_config.projects(),
            ["flatproj", "hobby/pinball", "notagroup", "work/aws", "work/msd"],
        )  # grouped as group/name, notagroup flat (sub ignored), work/hobby never self-list

    # --- session_states() ---

    def test_session_states_ranks_and_expires(self):
        os.makedirs(rc_sessions.STATE_DIR)
        now = time.time()

        def write(sid, state, proj, ts):
            (rc_sessions.STATE_DIR / f"{sid}.json").write_text(
                json.dumps({"state": state, "project": proj, "ts": ts})
            )

        write("s1", "working", "alpha", now)  # fresh, working
        write("s2", "idle", "alpha", now)  # fresh, idle — lower rank than working
        write("s3", "waiting", "beta", now)  # fresh, waiting
        # stale -> ignored
        write("s4", "working", "gamma", now - rc_state.STATE_TTL - 60)
        got = rc_sessions.session_states()
        # working outranks idle for the same project
        self.assertEqual(got["alpha"], "working")
        self.assertEqual(got["beta"], "waiting")
        self.assertNotIn("gamma", got)  # stale entry dropped

    # --- human_size() / crumb_html() ---

    def test_human_size_units(self):
        self.assertEqual(rc_share.human_size(512), "512 B")
        self.assertEqual(rc_share.human_size(1536), "1.5 KB")
        self.assertEqual(rc_share.human_size(1024**2), "1.0 MB")
        # the loop-fallthrough tail
        self.assertEqual(rc_share.human_size(1024**5), "1.0 PB")

    def test_crumb_html_builds_breadcrumb(self):
        html = rc_share.crumb_html("sub/deep")
        self.assertIn('href="/files">rc-share</a>', html)
        self.assertIn('href="/files/sub">sub</a>', html)
        self.assertIn('href="/files/sub/deep">deep</a>', html)

    def test_crumb_html_encoded_rel_not_double_encoded(self):
        # _files hands crumb_html the still-percent-encoded URL remainder; requoting
        # the encoded segment produced /files/my%2520file (404) labeled "my%20file".
        share = os.path.realpath(os.path.join(self.tmp, "share"))
        os.makedirs(os.path.join(share, "my file"))
        rc_config.SHARE = share
        crumb = rc_share.crumb_html("/my%20file")
        # once-encoded, decoded label
        self.assertIn('<a href="/files/my%20file">my file</a>', crumb)
        self.assertNotIn("%2520", crumb)
        page = rc_share.share_page(
            os.path.join(share, "my file"), "/my%20file"
        ).decode()
        self.assertIn('<a href="/files/my%20file">my file</a>', page)
        self.assertNotIn("%2520", page)

    def test_ttl_cached_is_single_flight_under_concurrent_misses(self):
        # two threads missing the same key at once must produce ONE computation — the
        # page's 5s poll fires whether or not the last one finished, so without this a
        # slow scan fanned out into parallel git forks per repo
        started, release, calls = threading.Event(), threading.Event(), []

        @rc_config.ttl_cached(lambda: 60.0)
        def slow(key):
            calls.append(key)
            started.set()
            release.wait(2)
            return key

        results = []
        t1 = threading.Thread(target=lambda: results.append(slow("k")))
        t2 = threading.Thread(target=lambda: results.append(slow("k")))
        t1.start()
        started.wait(2)
        t2.start()
        release.set()
        t1.join(2)
        t2.join(2)
        self.assertEqual(results, ["k", "k"])
        self.assertEqual(calls, ["k"])
        slow.invalidate()
        slow("k")
        self.assertEqual(calls, ["k", "k"])

    def test_invalidate_during_a_fill_drops_the_stale_result(self):
        # A fill in flight when invalidate() fires is pre-invalidate: it must not land in
        # the cache (last-writer-wins would otherwise re-seat the stale value a fresh caller
        # just replaced). Also pins the per-key prune: _inflight must not retain the key.
        import threading

        started, release, returns = (
            threading.Event(),
            threading.Event(),
            iter(["A", "B"]),
        )

        @rc_config.ttl_cached(lambda: 60.0)
        def f():
            v = next(returns)
            if v == "A":
                started.set()
                release.wait(2)  # A blocks mid-flight until we have invalidated
            return v

        got = []
        t1 = threading.Thread(target=lambda: got.append(f()))
        t1.start()
        started.wait(2)
        f.invalidate()  # bump the generation while A is still computing
        release.set()
        t1.join(2)
        self.assertEqual(got, ["A"])  # A's own caller still gets A
        self.assertEqual(f(), "B")  # but the cache recomputes — A did not poison it
        self.assertEqual(f(), "B")  # and B is now cached
        self.assertEqual(len(f._inflight), 0)  # every key's lock pruned after its fill

    def test_build_stamp_hashes_rc_sources_and_survives_an_empty_dir(self):
        d = Path(self.tmp, "src")
        d.mkdir()
        (d / "rc_a.py").write_text("a = 1\n")
        (d / "rc_b.py").write_text("b = 2\n")
        (d / "notes.txt").write_text("ignored\n")  # only rc_*.py counts
        stamp = rc_config._build_stamp(d)
        self.assertEqual(len(stamp), 12)  # kills the [:12] -> [:8] mutant
        (d / "rc_a.py").write_text("a = 999\n")  # source changed
        self.assertNotEqual(rc_config._build_stamp(d), stamp)  # ...so the stamp changed
        self.assertEqual(
            rc_config._build_stamp(Path(self.tmp, "empty")), ""
        )  # no rc_*.py
        (
            d / "rc_bad.py"
        ).mkdir()  # an rc_*.py that can't be read as bytes (a dir) -> OSError
        self.assertEqual(
            rc_config._build_stamp(d), ""
        )  # fail closed, not a partial hash

    def test_ensure_trusted_concurrent_launches_keep_claude_json_valid(self):
        # the mkstemp fix's reason: concurrent first-time-trust launches of different
        # projects must never tear ~/.claude.json through a shared temp. Hammer it and
        # assert the file stays valid JSON and every project lands trusted, no orphan temp.
        Path(rc_config.CLAUDE_JSON).write_text("{}")
        n = 24
        for i in range(n):
            os.makedirs(os.path.join(rc_config.PARENT, f"p{i}"), exist_ok=True)
        threads = [
            threading.Thread(target=rc_sessions.ensure_trusted, args=(f"p{i}",))
            for i in range(n)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(5)
        data = json.loads(Path(rc_config.CLAUDE_JSON).read_text())  # never torn
        trusted = data.get("projects", {})
        # last-writer-wins can drop some flags (benign — re-added next launch), but the file
        # is always valid and at least one project is trusted; the pre-fix shared temp tore it
        self.assertTrue(trusted)
        self.assertEqual(
            [
                f
                for f in os.listdir(Path(rc_config.CLAUDE_JSON).parent)
                if ".claude.json.rc" in f
            ],
            [],
        )

    def test_ensure_trusted_write_failure_logs_and_leaves_no_temp(self):
        # disk-full / unwritable: the launch must not 500 and no orphan temp may accumulate
        d = Path(rc_config.CLAUDE_JSON).parent
        os.makedirs(os.path.join(rc_config.PARENT, "p"))
        Path(rc_config.CLAUDE_JSON).write_text("{}")  # p untrusted -> reaches the write

        def _boom(*a, **k):
            raise OSError("No space left on device")

        real_dump = rc_claude.json.dump
        rc_claude.json.dump = _boom
        self.addCleanup(setattr, rc_claude.json, "dump", real_dump)
        rc_sessions.ensure_trusted("p")  # must not raise
        self.assertEqual(Path(rc_config.CLAUDE_JSON).read_text(), "{}")  # untouched
        self.assertEqual(
            [f for f in os.listdir(d) if f.startswith(".claude.json.rc")], []
        )  # temp unlinked on failure

    def test_git_ttl_default_is_thirty_seconds(self):
        # raised 15 -> 30 on 2026-09-04 when /status started driving it; pinned so a
        # silent revert (or a silent bump) shows up here
        self.assertEqual(rc_config.GIT_TTL, float(os.environ.get("RC_GIT_TTL", "30")))

    def test_read_token_without_a_file_is_empty_not_env(self):
        # no file -> "" (main() then refuses to start); there is no env fallback any more
        env(
            self,
            RC_LAUNCHER_TOKEN_FILE=os.path.join(self.tmp, "nope"),
            RC_LAUNCHER_TOKEN="should-be-ignored",
        )
        self.assertEqual(rc_config._read_token(), "")

    def test_rows_html_carries_sort_keys_dirs_first(self):
        share = os.path.realpath(os.path.join(self.tmp, "share"))
        os.makedirs(os.path.join(share, "sub"))  # a directory
        Path(share, "a.txt").write_text("xxxxx")  # a 5-byte file
        Path(share, "b.log").write_text("")  # a 0-byte file
        Path(share, "Zoo.txt").write_text("y")  # mixed case — the key must lowercase
        rc_config.SHARE = share
        rows = rc_share.rows_html(share, "")
        self.assertIn('data-d="1"', rows)  # the dir carries is-dir=1
        self.assertIn('data-d="0"', rows)  # a file carries is-dir=0
        self.assertIn('data-n="a.txt"', rows)  # lowercased name key
        # "Zoo.txt" lowercased, not passed through
        self.assertIn('data-n="zoo.txt"', rows)
        self.assertIn('data-s="5"', rows)  # size in bytes for the client sort
        self.assertRegex(rows, r'data-t="\d+"')  # mtime key
        # dirs precede files (no-JS default)
        self.assertLess(rows.index("sub/"), rows.index("a.txt"))

    # --- ensure_trusted() ---

    def test_ensure_trusted_sets_flag_idempotently(self):
        Path(rc_config.CLAUDE_JSON).write_text(json.dumps({"projects": {}}))
        key = self._proj("proj")
        rc_sessions.ensure_trusted("proj")
        d = json.loads(Path(rc_config.CLAUDE_JSON).read_text())
        self.assertTrue(d["projects"][key]["hasTrustDialogAccepted"])
        rc_sessions.ensure_trusted("proj")  # idempotent — no error, flag preserved
        d = json.loads(Path(rc_config.CLAUDE_JSON).read_text())
        self.assertTrue(d["projects"][key]["hasTrustDialogAccepted"])

    def test_ensure_trusted_missing_file_is_noop(self):
        # CLAUDE_JSON absent -> FileNotFoundError guard, no raise
        rc_sessions.ensure_trusted("proj")

    def test_ensure_trusted_unreadable_logs_and_noops(self):
        events = []
        rc_config.log_event = lambda *a: events.append(a)
        # a dir where a file is expected -> IsADirectoryError
        os.makedirs(rc_config.CLAUDE_JSON)
        rc_sessions.ensure_trusted("proj")  # must not raise / 500 the launch
        # the real error is surfaced, not swallowed
        self.assertTrue(any(e[0] == "trust" for e in events))

    def test_ensure_trusted_corrupt_json_logs_and_noops(self):
        events = []
        rc_config.log_event = lambda *a: events.append(a)
        # partial write / race with claude
        Path(rc_config.CLAUDE_JSON).write_text("{not json")
        self._proj("proj")
        # JSONDecodeError (a ValueError, not OSError) -> no raise
        rc_sessions.ensure_trusted("proj")
        self.assertTrue(any(e[0] == "trust" for e in events))

    # --- page() templating / Server.handle_error ---

    def test_fill_prefix_keys_longest_first(self):
        # A key that is a strict prefix of another: insertion order would match __A__
        # inside __A__B__ first and mangle it to "shortB__" — the longest-first sort
        # is what makes the mapping order-independent.
        out = rc_templates.fill(
            "__A__B__ + __A__", {"__A__": "short", "__A__B__": "long"}
        )
        self.assertEqual(out, b"long + short")

    def test_page_placeholder_named_project_not_reinterpreted(self):
        # a project name embedding a real placeholder token: NAME_RE now needs a leading
        # alphanumeric, but an interior __LOGIN__ still collides — single-pass fill must not
        # rewrite the name where it sits in the PROJECTS data.
        self._proj("x__LOGIN__")
        stubs = {
            rc_sessions: {"login_status": lambda: "ok", "session_states": lambda: {}},
            rc_tmux: {"running": lambda: set()},
            rc_git: {"git_states": lambda projs=None: {}},
            rc_desk: {"desk_projects": lambda: []},  # else this forks real pgrep/ps
        }
        for module, funcs in stubs.items():
            for name, stub in funcs.items():
                self.addCleanup(setattr, module, name, getattr(module, name))
                setattr(module, name, stub)
        out = rc_sessions.page().decode()
        self.assertIn('["x__LOGIN__"]', out)  # the name survives as data in PROJECTS...
        # ...and the real __LOGIN__ slot filled, not clobbered
        # (the old chained-replace produced [""ok""] here — a broken script)
        self.assertIn('LOGIN="ok"', out)

    def test_server_handle_error_swallows_conn_noise_reraises_real(self):
        self.addCleanup(
            setattr,
            socketserver.BaseServer,
            "handle_error",
            socketserver.BaseServer.handle_error,
        )
        reached = []
        # exact arity: Server.handle_error must forward (request, client_address)
        socketserver.BaseServer.handle_error = lambda self, req, addr: reached.append(
            (req, addr)
        )
        srv = rc_launcher.Server(("127.0.0.1", 0), rc_launcher.Handler)
        self.addCleanup(srv.server_close)
        try:
            raise ConnectionResetError("client RST")
        except ConnectionResetError:
            srv.handle_error(None, ("127.0.0.1", 0))
        self.assertEqual(reached, [])  # a dropped connection is swallowed, no traceback
        try:
            raise ValueError("a real bug")
        except ValueError:
            srv.handle_error(None, ("127.0.0.1", 0))
        # a real error still reaches the traceback path
        self.assertEqual(reached, [(None, ("127.0.0.1", 0))])  # forwarded intact

    # --- log_event / projects edge / session_states skips ---

    def test_log_event_prints_audit_line(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _REAL_LOG("launch", "proj", "ok")
        line = buf.getvalue()
        self.assertIn("launch", line)
        self.assertIn("proj", line)
        self.assertIn("ok", line)

    def test_projects_missing_parent_is_empty(self):
        shutil.rmtree(rc_config.PARENT)
        self.assertEqual(rc_config.projects(), [])

    def test_session_states_skips_nonjson_bad_and_projectless(self):
        os.makedirs(rc_sessions.STATE_DIR)
        (rc_sessions.STATE_DIR / "note.txt").write_text("x")  # not .json -> skipped
        # decode error -> skipped
        (rc_sessions.STATE_DIR / "bad.json").write_text("{not json")
        # no project
        (rc_sessions.STATE_DIR / "np.json").write_text('{"state":"working","ts":9e18}')
        self.assertEqual(rc_sessions.session_states(), {})


if __name__ == "__main__":
    unittest.main()
