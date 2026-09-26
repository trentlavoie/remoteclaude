"""The launcher's own routes — /status, /create, /launch, /stop and the root page — over
the real server with subprocess/os.kill/sleep mocked, so no tmux or claude is actually
spawned; plus the share page those same handlers render."""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import rc_config
import rc_sessions
import rc_settings
import rc_share
import rc_tmux
import rc_templates

from tests._harness import (
    ServerCase,
    desk,
    proc,
    respond,
    restore_globals,
    share_dir,
    spawn_ok,
)


class RowsHtmlTest(unittest.TestCase):
    def setUp(self):
        restore_globals(self)
        self.share = rc_config.SHARE = share_dir(self)

    def test_hides_rcpart_and_escaping_symlink(self):
        open(os.path.join(self.share, "real.txt"), "w").close()
        open(os.path.join(self.share, "partial.rcpart"), "w").close()
        # -> outside SHARE
        os.symlink(tempfile.mkdtemp(), os.path.join(self.share, "escape"))
        rows = rc_share.rows_html(self.share, "")
        self.assertIn("real.txt", rows)
        self.assertNotIn("partial", rows)  # .rcpart hidden
        self.assertNotIn("escape", rows)  # symlink out of SHARE not listed

    def test_files_page_carries_the_download_confirmation_hooks(self):
        # the app keys on the UA tag and calls rcDownloadDone(); a browser gets the
        # fetch-with-progress path. Both live in the page, so both are pinned here.
        out = rc_share.share_page(self.share, "").decode()
        self.assertIn("rc-launcher-app", out)
        self.assertIn("window.rcDownloadDone=function", out)
        self.assertIn("URL.createObjectURL", out)
        # and the script must parse: a bad \\u escape or an unbalanced brace in the new
        # async function would leave the page dead while every substring above still matched
        if not (node := shutil.which("node")):
            self.skipTest("node not installed")
        script = re.search(r"<script[^>]*>(.*)</script>", out, re.S).group(1)
        path = os.path.join(self.share, "page.js")
        Path(path).write_text(script)
        self.assertEqual(subprocess.run([node, "--check", path]).returncode, 0)

    def test_script_context_values_are_escaped(self):
        self.assertNotIn("<", rc_templates.js("</script>"))  # the escape at the source
        out = rc_share.share_page(self.share, "a</script>b")
        self.assertNotIn(b"</script>b", out)  # the rel didn't break out of the <script>
        self.assertNotIn(b"__REL__", out)  # no unfilled placeholder left

    def test_rows_html_dirs_first_empty_and_unreadable(self):
        os.makedirs(os.path.join(self.share, "adir"))
        open(os.path.join(self.share, "afile.txt"), "w").close()
        rows = rc_share.rows_html(self.share, "")
        # dirs before files
        self.assertLess(rows.index("adir"), rows.index("afile.txt"))
        self.assertIn(
            "empty", rc_share.rows_html(os.path.join(self.share, "adir"), "/adir")
        )
        # OSError is not "empty"
        self.assertIn(
            "unreadable", rc_share.rows_html(os.path.join(self.share, "no"), "/no")
        )


class RouteTest(ServerCase):
    """The do_GET routes (/status, /create, /launch, /stop, root) over the real server with
    subprocess/os.kill/sleep mocked, so no tmux/claude is actually spawned."""

    def setUp(self):
        super().setUp()
        self.aux = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.aux, True)
        rc_config.PARENT = os.path.join(self.aux, "projects")
        os.makedirs(rc_config.PARENT)
        rc_sessions.STATE_DIR = Path(self.aux, "state")
        rc_config.CLAUDE_JSON = os.path.join(self.aux, "claude.json")
        # never the host's
        rc_config.CLAUDE_PROJECTS = Path(self.aux, "claude-projects")
        Path(rc_config.CLAUDE_JSON).write_text("{}")
        self.responses: dict = {}
        self.desk: dict = {}
        subprocess.run = lambda cmd, **kw: self._resp(cmd)
        # force _pid_cwd down the mocked lsof path — on Linux a real /proc/<pid>/cwd for a
        # colliding pid would bypass the desk mock (macOS has no /proc, so it hid this)
        os.path.islink = lambda p: False
        os.kill = lambda *a: None
        time.sleep = lambda *a: None
        rc_settings.RESUME = (
            "off"  # fresh launches (no takeover) keep route tests simple
        )
        rc_config.ALLOW_GET_ACTIONS = (
            False  # the shipped default: state changes are POST
        )

    def _resp(self, cmd):
        return respond(cmd, self.desk, self.responses)

    def get(self, path):
        status, _, body = self.req("GET", path)
        return status, body

    def post(self, path):
        """A state change the way the page sends it: POST, same-origin."""
        status, _, body = self.req(
            "POST", path, headers={"Sec-Fetch-Site": "same-origin"}
        )
        return status, body

    def test_status_route(self):
        self.responses = {
            "list-sessions": proc(stdout="rc-alpha\n"),
            "auth status": proc(stdout='{"loggedIn": true}'),
        }
        status, body = self.get("/status")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["login"], "ok")
        self.assertIn("alpha", d["running"])
        self.assertIn("git", d)  # badges follow the poll now, not just the page load
        self.assertEqual(
            d["model"], rc_settings.MODEL
        )  # the pinned launch model, surfaced

    def test_root_page_fills_placeholders(self):
        self.responses = {"auth status": proc(stdout='{"loggedIn": true}')}
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"__PROJECTS__", body)
        self.assertNotIn(b"__LOGIN__", body)
        # the pinned default is rendered into the selector's title (fill() leaves unmatched
        # keys literal, so this goes red if __MODEL__ were dropped from page()'s fill dict)
        self.assertNotIn(b"__MODEL__", body)
        self.assertIn(f"pin: {rc_settings.MODEL}".encode(), body)
        # the per-launch selector, defaulting to Sonnet 5 (empty value = the pinned default)
        self.assertIn(b'<option value="" selected>Sonnet 5</option>', body)

    def test_root_page_has_live_band_and_the_script_parses(self):
        # the Live band is sourced from /status state (running/extrc/desk), so it can't drift
        # from reality the way the localStorage Recent list does; and a JS syntax error in the
        # inline script would leave every placeholder filled yet the page dead, so parse it
        self.responses = {"auth status": proc(stdout='{"loggedIn": true}')}
        out = self.get("/")[1].decode()
        self.assertIn("id=liveWrap", out)
        self.assertIn(
            "band('#liveWrap'", out
        )  # rendered from live state, not getRecent()
        # Recent is deduped against Live, and all four bands are collapsible (persisted)
        self.assertIn("!liveSet.has(n)", out)
        self.assertIn("rc_collapsed", out)
        for sec in ("pinned", "live", "recent", "all"):
            self.assertIn(f"data-sec={sec}", out)
        if not (node := shutil.which("node")):
            self.skipTest("node not installed")
        script = re.search(r"<script[^>]*>(.*)</script>", out, re.S).group(1)
        path = os.path.join(self.aux, "page.js")
        Path(path).write_text(script)
        self.assertEqual(subprocess.run([node, "--check", path]).returncode, 0)

    def test_create_route_makes_and_launches(self):
        self.responses = spawn_ok()
        status, body = self.post("/create?proj=newp")
        d = json.loads(body)
        self.assertEqual(d["status"], "created")
        self.assertEqual(d["launch"], "launched")
        self.assertTrue(os.path.isdir(os.path.join(rc_config.PARENT, "newp")))

    def test_create_route_bad_name_reports_reason(self):
        status, body = self.post("/create?proj=bad%20name")  # space -> badname + reason
        d = json.loads(body)
        self.assertEqual(d["status"], "badname")
        self.assertIn("reason", d)

    def test_launch_route_unknown_project_404(self):
        self.assertEqual(self.post("/launch?proj=ghost")[0], 404)

    def test_launch_and_stop_routes(self):
        os.makedirs(os.path.join(rc_config.PARENT, "realp"))
        self.responses = spawn_ok()
        self.assertEqual(
            json.loads(self.post("/launch?proj=realp&json=1")[1])["status"], "launched"
        )
        # model a live session that dies on the C-c, so /stop reports a real kill
        seq = iter([True, False, False, False])
        self.addCleanup(setattr, rc_tmux, "has_session", rc_tmux.has_session)
        rc_tmux.has_session = lambda s: next(seq, False)
        self.assertEqual(
            json.loads(self.post("/stop?proj=realp&json=1")[1])["status"], "stopped"
        )

    def test_stop_desk_route_sigterms_desk_session(self):
        # The X on a desk-badged row: /stop?desk=1 must take the desk_stop branch —
        # SIGTERM the desk claude, never the tmux C-c/kill-session path (deleting the
        # route conditional would fall through to stop() and phantom-"stop" nothing).
        os.makedirs(os.path.join(rc_config.PARENT, "deskp"))
        root = os.path.join(rc_config.PARENT, "deskp")
        killed, calls = [], []

        def kill(pid, sig):
            killed.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # SIGTERM worked; takeover needn't escalate

        os.kill = kill
        subprocess.run = lambda cmd, **kw: (
            calls.append(cmd),
            self._resp(cmd),
        )[1]
        self.desk = {"321": desk(root)}
        status, body = self.post("/stop?proj=deskp&desk=1&json=1")
        self.assertEqual(json.loads(body)["status"], "stopped")
        self.assertIn((321, signal.SIGTERM), killed)  # graceful desk close
        self.assertNotIn((321, signal.SIGKILL), killed)
        joined = [" ".join(map(str, c)) for c in calls]
        self.assertFalse(any("send-keys" in c or "kill-session" in c for c in joined))

    def test_plain_stop_route_falls_through_to_external_rc(self):
        # a PLAIN /stop (no ext=1) on a project with no tmux session but a live external RC
        # process SIGTERMs it — the caller need not know how the session was started
        os.makedirs(os.path.join(rc_config.PARENT, "extp"))
        root = os.path.join(rc_config.PARENT, "extp")
        killed, calls = [], []

        def kill(pid, sig):
            killed.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError

        os.kill = kill
        subprocess.run = lambda cmd, **kw: (calls.append(cmd), self._resp(cmd))[1]
        self.responses = {
            "has-session": proc(returncode=1)
        }  # not a tmux session -> the fallback
        self.desk = {"321": desk(root, command="claude --remote-control extp")}
        status, body = self.post("/stop?proj=extp&json=1")  # NO ext=1
        self.assertEqual(json.loads(body)["status"], "stopped")
        self.assertIn((321, signal.SIGTERM), killed)
        joined = [" ".join(map(str, c)) for c in calls]
        self.assertFalse(any("send-keys" in c or "kill-session" in c for c in joined))

    def test_plain_stop_never_reaps_a_desk_claude(self):
        # the load-bearing constraint: the fallback stops at external RC. A plain /stop on a
        # project whose only live session is a DESK claude must NOT kill it — desk stays the
        # explicit desk=1 action. Reads "idle" and signals nothing.
        os.makedirs(os.path.join(rc_config.PARENT, "deskonly"))
        root = os.path.join(rc_config.PARENT, "deskonly")
        killed = []
        os.kill = lambda pid, sig: killed.append((pid, sig))
        self.responses = {"has-session": proc(returncode=1)}  # no tmux session
        self.desk = {"321": desk(root)}  # a plain desk claude, no --remote-control
        status, body = self.post("/stop?proj=deskonly&json=1")
        self.assertEqual(
            json.loads(body)["status"], "idle"
        )  # nothing an RC-stop can close
        self.assertEqual(killed, [])  # the desk claude was left alone

    def test_addroot_requires_the_token(self):
        self.assertEqual(self.req("POST", "/addroot?path=/tmp", cookie=False)[0], 403)

    def test_addroot_adds_a_directory_and_lists_its_children(self):
        extra = os.path.join(self.aux, "extra")
        os.makedirs(os.path.join(extra, "sub"))
        status, _, body = self.req("POST", f"/addroot?path={extra}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "added")
        projs = json.loads(self.req("GET", "/status")[2])["projects"]
        self.assertIn("extra/sub", projs)  # the new root's child now lists
        self.assertEqual(
            json.loads(self.req("POST", "/addroot?path=/no/such")[2])["status"],
            "badpath",
        )

    def test_version_route_is_unauthenticated_and_returns_the_stamp(self):
        # the watchdog liveness probe hits this with no token; moving it below _authed would
        # 403 and break the probe, so pin that a token-less GET gets 200 + the build stamp
        status, _, body = self.req("GET", "/version", cookie=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], rc_config.VERSION)

    def test_stop_route_reports_failed_when_the_session_survives(self):
        # the contract stopSess keys on: a session still alive after the double C-c
        # and the kill is "failed" with a reason, so the page keeps the dot and toasts
        os.makedirs(os.path.join(rc_config.PARENT, "p"))
        rc_config.STOP_WAIT = 0
        self.responses = {"has-session": proc(returncode=0)}
        status, body = self.post("/stop?proj=p&json=1")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "status": "failed",
                "proj": "p",
                "reason": "still alive after SIGINT and kill-session",
            },
        )

    def test_launch_on_a_desk_live_project_is_already_not_a_launch(self):
        # idempotent: a live desk claude for the project makes /launch return "already" with
        # kind=desk (the response shape the picker turns into a stop-first prompt); it starts
        # nothing and kills nothing — desk=1 is meaningless on /launch (only /stop reads it)
        os.makedirs(os.path.join(rc_config.PARENT, "p"))
        killed = []
        os.kill = lambda pid, sig: killed.append((pid, sig))
        self.desk = {"321": desk(os.path.join(rc_config.PARENT, "p"))}
        self.responses = {"has-session": proc(returncode=1)}  # no tmux session
        status, body = self.post("/launch?proj=p&json=1")
        d = json.loads(body)
        self.assertEqual(d["status"], "already")
        self.assertEqual(d["kind"], "desk")
        self.assertEqual(killed, [])  # the desk claude is not reaped by /launch

    def test_unknown_route_404(self):
        self.assertEqual(self.get("/nonexistent")[0], 404)

    def _dead_spawn(self):
        # a fresh launch whose tmux pane dies inside the liveness window, with a
        # recognisable death reason on its dead pane
        return spawn_ok() | {
            "pane_dead": proc(stdout="1\n"),
            "capture-pane": proc(stdout="please trust this workspace\n"),
        }

    def test_launch_json_failure_carries_reason(self):
        os.makedirs(os.path.join(rc_config.PARENT, "p"))
        self.responses = self._dead_spawn()
        status, body = self.post("/launch?proj=p&json=1")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"status": "failed", "proj": "p", "reason": "untrusted dir"},
        )

    def test_launch_route_accepts_a_model_alias(self):
        # ?model=opus resolves to the full ID and reaches the spawned argv
        os.makedirs(os.path.join(rc_config.PARENT, "p"))
        calls = []
        real = subprocess.run
        subprocess.run = lambda cmd, **kw: (calls.append(cmd), real(cmd, **kw))[1]
        self.responses = spawn_ok()
        status, body = self.post("/launch?proj=p&model=opus&json=1")
        self.assertEqual(json.loads(body)["status"], "launched")
        newsession = next(c for c in calls if "new-session" in " ".join(map(str, c)))
        self.assertIn("--model claude-opus-5-5 --remote-control", newsession[-1])

    def test_launch_route_unknown_model_fails_with_allowed_list(self):
        # a bad ?model= is rejected before any spawn; the reason names the allowed aliases
        os.makedirs(os.path.join(rc_config.PARENT, "p"))
        killed = []
        os.kill = lambda pid, sig: killed.append((pid, sig))
        status, body = self.post("/launch?proj=p&model=gpt-9&json=1")
        d = json.loads(body)
        self.assertEqual(d["status"], "failed")
        self.assertIn("unknown model 'gpt-9'", d["reason"])
        self.assertIn("opus", d["reason"])  # the allowed aliases are listed

    def test_launch_already_with_model_notes_it_was_not_applied(self):
        # an "already" answer never switches a live session's model; it says how to switch
        os.makedirs(os.path.join(rc_config.PARENT, "p"))
        self.desk = {"321": desk(os.path.join(rc_config.PARENT, "p"))}
        self.responses = {"has-session": proc(returncode=1)}  # no tmux session
        d = json.loads(self.post("/launch?proj=p&model=opus&json=1")[1])
        self.assertEqual(d["status"], "already")
        self.assertEqual(d["kind"], "desk")
        self.assertIn("model not applied", d["note"])

    def test_create_then_failed_launch_carries_launch_reason(self):
        self.responses = self._dead_spawn()
        status, body = self.post("/create?proj=newproj")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual((payload["status"], payload["launch"]), ("created", "failed"))
        self.assertEqual(payload["launch_reason"], "untrusted dir")

    def test_launch_without_json_renders_the_page(self):
        # the browser form (no json=1) gets the launcher page back, not JSON
        os.makedirs(os.path.join(rc_config.PARENT, "p"))
        self.responses = spawn_ok()
        status, body = self.post("/launch?proj=p")
        self.assertEqual(status, 200)
        self.assertTrue(body.lstrip().lower().startswith(b"<!doctype html"), body[:40])


if __name__ == "__main__":
    unittest.main()
