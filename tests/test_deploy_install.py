"""The Linux deployment pieces install.sh generates, checked without installing anything:
`install.sh --render DIR` writes the four systemd --user units and the env file into a temp
dir (HOME is a temp dir too), `install.sh --hook` edits a temp ~/.claude/settings.json, and
deploy/rc-tmux is exercised against a tmux socket in a temp dir. Nothing here calls
systemctl, and no tmux call can reach the invoking user's own tmux server."""

import atexit
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNITS = ("rc-tmux.service", "rc-launcher.service", "rc-healthcheck.service")
TIMER = "rc-healthcheck.timer"
ROOT = os.geteuid() == 0  # install.sh refuses to run as root
# Stand-ins that shadow every host-changing tool on PATH: --render and --hook never call
# them, and if a regression ever did, the test fails instead of touching the real host.
POISON = Path(tempfile.mkdtemp(prefix="rc-poison-"))
for _tool in ("systemctl", "loginctl", "launchctl", "tailscale", "sudo", "tmux"):
    (POISON / _tool).write_text(
        f"#!/bin/sh\necho 'test must not run {_tool}' >&2\nexit 97\n"
    )
    (POISON / _tool).chmod(0o755)
atexit.register(shutil.rmtree, POISON, True)


def _run(args, home, extra_env=None, check=True):
    env = {
        "PATH": f"{POISON}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "RC_PYTHON": sys.executable,
        "LANG": "C.UTF-8",
        **(extra_env or {}),
    }
    r = subprocess.run(
        ["bash", str(REPO / "install.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and r.returncode:
        raise AssertionError(
            f"install.sh {args} -> {r.returncode}\n{r.stdout}{r.stderr}"
        )
    return r


@unittest.skipIf(ROOT, "install.sh refuses to run as root")
class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.out = self.tmp / "out"

    def _render(self, **env):
        """(result, {unit: its directives, comments dropped}, env file text)"""
        r = _run(["--render", str(self.out)], self.home, env)
        units = {
            u: "\n".join(
                ln
                for ln in (self.out / u).read_text().splitlines()
                if not ln.startswith("#")
            )
            for u in (*UNITS, TIMER)
        }
        return r, units, (self.out / "rc-launcher.env").read_text()

    def test_units_split_the_tmux_server_from_the_launcher(self):
        _, u, _ = self._render()
        tmux, launcher = u["rc-tmux.service"], u["rc-launcher.service"]
        # the tmux server is rc-tmux's own process (foreground, its own config)...
        self.assertIn(
            f"ExecStart={REPO}/deploy/rc-tmux -D -f {REPO}/deploy/rc-tmux.conf", tmux
        )
        self.assertIn("RuntimeDirectory=rc-tmux", tmux)
        # ...and the launcher only a client of it, pinned past the env file
        self.assertIn("Requires=rc-tmux.service", launcher)
        self.assertIn("After=rc-tmux.service", launcher)
        execstart = next(
            ln for ln in launcher.splitlines() if ln.startswith("ExecStart=")
        )
        self.assertIn(f"RC_TMUX_BIN={REPO}/deploy/rc-tmux", execstart)
        self.assertIn("RC_LAUNCHER_BIND=127.0.0.1", execstart)
        self.assertIn(f"{sys.executable} -E -s -u {REPO}/rc_launcher.py", execstart)
        self.assertNotIn("KillMode=process", launcher)

    def test_every_unit_finds_local_bin_and_carries_no_inline_config(self):
        _, u, _ = self._render()
        for name in UNITS:
            with self.subTest(unit=name):
                self.assertIn(
                    "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin",
                    u[name],
                )
                # settings come from the 0600 env file, never inline Environment=RC_...
                self.assertNotRegex(u[name], r"(?m)^Environment=RC_")
                self.assertNotIn("@REPO@", u[name])
                self.assertNotIn("@PYTHON@", u[name])
        for name in ("rc-launcher.service", "rc-healthcheck.service"):
            self.assertIn(
                "EnvironmentFile=%h/.config/rc-launcher/rc-launcher.env", u[name]
            )
        # the sessions' environment never sees the settings (e.g. the ntfy topic)
        self.assertNotIn("EnvironmentFile", u["rc-tmux.service"])

    def test_hardening_is_userns_free_and_spares_the_sessions(self):
        _, u, _ = self._render()
        # these need unprivileged user namespaces in a --user unit, which Ubuntu 24.04's
        # AppArmor denies (the unit would fail to start); MemoryDenyWriteExecute would kill
        # claude's JIT runtime
        banned = (
            "ProtectSystem=",
            "ProtectHome=",
            "PrivateTmp=",
            "ReadWritePaths=",
            "PrivateDevices=",
            "PrivateUsers=",
            "ProtectKernel",
            "MemoryDenyWriteExecute=",
        )
        for name in UNITS:
            for directive in banned:
                self.assertNotIn(directive, u[name], f"{name}: {directive}")
        for name in ("rc-launcher.service", "rc-healthcheck.service"):
            for directive in (
                "NoNewPrivileges=yes",
                "RestrictNamespaces=yes",
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
                "UMask=0077",
            ):
                self.assertIn(directive, u[name], f"{name}: {directive}")
        # the tmux unit: only the one restriction that costs a Claude session nothing
        self.assertIn("NoNewPrivileges=yes", u["rc-tmux.service"])
        self.assertNotIn("RestrictNamespaces", u["rc-tmux.service"])
        self.assertNotIn("SystemCallFilter", u["rc-tmux.service"])

    def test_restart_policies(self):
        _, u, _ = self._render()
        self.assertIn("Restart=on-failure", u["rc-launcher.service"])
        self.assertIn("RestartMaxDelaySec=", u["rc-launcher.service"])
        self.assertIn("Restart=always", u["rc-tmux.service"])
        self.assertIn("KillMode=control-group", u["rc-tmux.service"])
        self.assertIn("Type=oneshot", u["rc-healthcheck.service"])
        self.assertIn("OnUnitActiveSec=30min", u[TIMER])
        self.assertIn("OnActiveSec=", u[TIMER])

    def test_env_file_values_precedence_and_permissions(self):
        conf = self.home / ".config" / "rc-launcher"
        conf.mkdir(parents=True)
        (conf / "rc-launcher.env").write_text(
            "# old comment\n"
            "RC_SPAWN=session\n"
            'RC_RESUME="fork"\n'
            "RC_LAUNCHER_BIND=0.0.0.0\n"
            "RC_TMUX_BIN=/usr/bin/tmux\n"
            "RC_FUTURE_KNOB=on\n"
            "NOT_OURS=1\n"
        )
        r, _, env = self._render(
            RC_MODEL="claude-opus-5-5",
            RC_RESUME="off",
            RC_PROJECTS_PARENT="~/workspace",
            RC_PROJECT_GROUPS="work,hobby",
        )
        lines = set(env.splitlines())
        self.assertIn("RC_SPAWN=session", lines)  # kept from the file
        self.assertIn("RC_RESUME=off", lines)  # this invocation wins over the file
        self.assertIn("RC_MODEL=claude-opus-5-5", lines)
        self.assertIn("RC_PROJECTS_PARENT=~/workspace", lines)
        self.assertIn("RC_FUTURE_KNOB=on", lines)  # unknown RC_* carried over
        self.assertIn("RC_SHARE_ENABLED=0", lines)  # the share is opt-in
        self.assertIn("# RC_NOTIFY_URL=", lines)  # empty -> commented, never KEY=
        self.assertNotIn("NOT_OURS=1", lines)
        # pinned by the unit: dropped from the file, and said so
        self.assertNotRegex(env, r"(?m)^RC_LAUNCHER_BIND=")
        self.assertNotRegex(env, r"(?m)^RC_TMUX_BIN=")
        self.assertIn("RC_LAUNCHER_BIND", r.stderr)
        # bash's GROUPS is a magic array: the old script wrote the user's gid here
        self.assertIn("RC_PROJECT_GROUPS=work,hobby", lines)
        mode = stat.S_IMODE((self.out / "rc-launcher.env").stat().st_mode)
        self.assertEqual(mode, 0o600)
        for u in UNITS:
            self.assertEqual(stat.S_IMODE((self.out / u).stat().st_mode), 0o600)

    def test_the_example_env_file_is_complete_and_loads_as_is(self):
        example = (REPO / "deploy/rc-launcher.env.example").read_text()
        conf = self.home / ".config" / "rc-launcher"
        conf.mkdir(parents=True)
        (conf / "rc-launcher.env").write_text(example)
        _, _, env = self._render()
        lines = set(env.splitlines())
        for kv in (
            "RC_PROJECTS_PARENT=~/workspace",
            "RC_MODEL=claude-opus-5-5",
            "RC_SPAWN=worktree",
            "RC_SHARE_ENABLED=0",
        ):
            self.assertIn(kv, lines)
        # every key install.sh manages is documented in the example
        keys = re.findall(r"(?m)^(?:# )?(RC_[A-Z0-9_]+)=", env)
        self.assertGreaterEqual(len(keys), 15)
        for key in keys:
            self.assertIn(key, example, f"{key} undocumented")

    def test_env_file_rejects_values_it_cannot_carry_safely(self):
        for bad in ("https://x/$(id)", "a b", 'x"y', "x\\y", "`id`"):
            with self.subTest(value=bad):
                r = _run(
                    ["--render", str(self.out)],
                    self.home,
                    {"RC_NOTIFY_URL": bad},
                    check=False,
                )
                self.assertNotEqual(r.returncode, 0)

    def test_systemd_analyze_verify_accepts_the_rendered_units(self):
        sa = shutil.which("systemd-analyze")
        if not sa:
            self.skipTest("systemd-analyze not available")
        self._render()
        probe = self.tmp / "probe"
        probe.mkdir()
        (probe / "probe.service").write_text(
            "[Unit]\nDescription=probe\n[Service]\nExecStart=/bin/true\n"
        )
        base = subprocess.run(
            [sa, "--user", "verify", "probe.service"],
            cwd=probe,
            capture_output=True,
            text=True,
        )
        if base.returncode:
            self.skipTest(f"systemd-analyze --user verify unusable here: {base.stderr}")
        r = subprocess.run(
            [sa, "--user", "verify", *UNITS, TIMER],
            cwd=self.out,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.strip(), "", r.stderr)  # no warnings either

    def test_unit_paths_with_specials_are_refused(self):
        r = _run(
            ["--render", str(self.out)],
            self.home,
            {"RC_PYTHON": "/tmp/has space/python3"},
            check=False,
        )
        self.assertNotEqual(r.returncode, 0)


@unittest.skipIf(ROOT, "install.sh refuses to run as root")
class HookTest(unittest.TestCase):
    """install.sh --hook / deploy/claude-settings.sh: backup, merge, atomic replace."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        self.settings = self.claude / "settings.json"
        self.original = {
            "model": "opus",
            "statusLine": {"type": "command", "command": "echo ✓"},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]},
        }
        self.settings.write_text(json.dumps(self.original, ensure_ascii=False))
        os.chmod(self.settings, 0o640)

    def _backups(self):
        return sorted(self.claude.glob("settings.json.rc-backup-*"))

    def _hook_cmds(self, d):
        return [
            h["command"]
            for entries in d.get("hooks", {}).values()
            for e in entries
            for h in e.get("hooks", [])
        ]

    def _remove(self):
        return subprocess.run(
            [
                "bash",
                "-c",
                '. "$REPO/deploy/claude-settings.sh"; edit_claude_settings --remove-hook',
            ],
            env={
                "PATH": f"{POISON}:/usr/bin:/bin",
                "HOME": str(self.home),
                "PY": sys.executable,
                "REPO": str(REPO),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_hook_install_backs_up_merges_and_keeps_mode(self):
        r = _run(["--hook"], self.home)
        d = json.loads(self.settings.read_text())
        self.assertEqual(d["model"], "opus")
        self.assertEqual(d["statusLine"], self.original["statusLine"])
        self.assertIn("mine", self._hook_cmds(d))  # the user's own hook survives
        self.assertTrue(any("rc_state_hook.py" in c for c in self._hook_cmds(d)))
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o640)
        (bak,) = self._backups()
        self.assertEqual(json.loads(bak.read_text()), self.original)
        self.assertEqual(stat.S_IMODE(bak.stat().st_mode), 0o600)
        self.assertIn("backup:", r.stdout)
        self.assertEqual(list(self.claude.glob(".rc-settings.*")), [])  # no stage left
        # idempotent: a second run changes nothing and takes no second backup
        time.sleep(1.1)  # backups are named by the second
        r = _run(["--hook"], self.home)
        self.assertIn("no change needed", r.stdout)
        self.assertEqual(len(self._backups()), 1)
        # and the removal is the exact inverse
        r = self._remove()
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(self.settings.read_text())
        self.assertEqual(self._hook_cmds(d), ["mine"])
        self.assertEqual(d["model"], "opus")

    def test_symlinked_settings_keeps_the_link(self):
        real = self.home / "dotfiles" / "claude-settings.json"
        real.parent.mkdir()
        self.settings.rename(real)
        self.settings.symlink_to(real)
        _run(["--hook"], self.home)
        self.assertTrue(self.settings.is_symlink())
        d = json.loads(real.read_text())
        self.assertTrue(any("rc_state_hook.py" in c for c in self._hook_cmds(d)))

    def test_unparseable_settings_are_left_untouched(self):
        self.settings.write_text("{not json")
        r = _run(["--hook"], self.home, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.settings.read_text(), "{not json")
        self.assertEqual(self._backups(), [])
        self.assertEqual(list(self.claude.glob(".rc-settings.*")), [])

    def test_missing_settings_is_created_owner_only(self):
        self.settings.unlink()
        _run(["--hook"], self.home)
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        self.assertEqual(self._backups(), [])  # nothing existed to back up


class ScriptHygieneTest(unittest.TestCase):
    """Static checks over the shell the deployment runs."""

    SCRIPTS = (
        "install.sh",
        "uninstall.sh",
        "deploy/tailscale-serve.sh",
        "deploy/rc-tmux",
        "deploy/claude-settings.sh",
    )

    @staticmethod
    def _code(text):
        """The script's lines with comments and quoted strings (which may span lines)
        blanked out, so only what the shell would execute is left. Approximate — heredoc
        bodies stay in — but it never turns a string into code."""
        out, i, state = [], 0, None  # state: None | '"' | "'" | "#"
        while i < len(text):
            c = text[i]
            if state == "#":
                if c == "\n":
                    state = None
                    out.append(c)
            elif state == "'":
                if c == "'":
                    state = None
                elif c == "\n":
                    out.append(c)
            elif state == '"':
                if c == "\\":
                    i += 1
                elif c == '"':
                    state = None
                elif c == "\n":
                    out.append(c)
            elif c == "\\":
                out.append(text[i : i + 2])
                i += 1
            elif c in "'\"":
                state = c
            elif c == "#" and (i == 0 or text[i - 1] in " \t\n"):
                state = "#"
            else:
                out.append(c)
            i += 1
        return "".join(out).splitlines()

    def test_no_script_runs_sudo_or_pipes_to_a_shell(self):
        for name in self.SCRIPTS:
            text = (REPO / name).read_text()
            for line in self._code(text):
                with self.subTest(script=name, line=line):
                    self.assertNotRegex(line, r"\bsudo\b")
                    self.assertNotRegex(line, r"\|\s*(ba|z)?sh\b")
                    self.assertNotRegex(line, r"enable-linger")

    def test_token_is_never_printed(self):
        text = (REPO / "install.sh").read_text()
        for line in self._code(text):
            self.assertNotRegex(line, r"\bcat\b.*TOKEN_FILE")
        self.assertNotIn("token=$(", text)

    def test_tailscale_script_never_runs_funnel(self):
        for line in self._code((REPO / "deploy/tailscale-serve.sh").read_text()):
            self.assertNotRegex(line, r"\btailscale\s+funnel\b")

    def test_scripts_are_executable(self):
        for name in (
            "install.sh",
            "uninstall.sh",
            "deploy/tailscale-serve.sh",
            "deploy/rc-tmux",
        ):
            self.assertTrue(os.access(REPO / name, os.X_OK), name)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TmuxShimTest(unittest.TestCase):
    """deploy/rc-tmux reaches exactly $XDG_RUNTIME_DIR/rc-tmux/tmux.sock. TMUX_TMPDIR is
    pointed at a temp dir too, so even a broken shim could not reach a real tmux server."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rct"))  # short: unix socket path limit
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fallback = self.tmp / "fallback"
        self.fallback.mkdir()
        self.env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "XDG_RUNTIME_DIR": str(self.tmp),
            "TMUX_TMPDIR": str(self.fallback),
        }

    def _shim(self, *args, timeout=10):
        return subprocess.run(
            [str(REPO / "deploy/rc-tmux"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_fails_closed_while_the_server_unit_is_down(self):
        # rc-tmux.service's RuntimeDirectory is absent: new-session must not start a server
        # anywhere — not at the pinned path, not in the TMUX_TMPDIR fallback
        self._shim("-f", "/dev/null", "new-session", "-d", "-s", "rc-x", "sleep 30")
        self.assertFalse((self.tmp / "rc-tmux").exists())
        self.assertEqual(list(self.fallback.iterdir()), [])
        self.assertNotEqual(self._shim("has-session", "-t", "=rc-x").returncode, 0)

    def test_client_and_server_meet_on_the_pinned_socket(self):
        (self.tmp / "rc-tmux").mkdir(mode=0o700)
        server = subprocess.Popen(
            [
                str(REPO / "deploy/rc-tmux"),
                "-D",
                "-f",
                str(REPO / "deploy/rc-tmux.conf"),
            ],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(server.kill)
        sock = self.tmp / "rc-tmux" / "tmux.sock"
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.1)
        self.assertTrue(sock.is_socket())
        # with zero sessions the -D server stays up (a plain server would exit)
        self.assertEqual(self._shim("list-sessions").returncode, 0)
        r = self._shim("new-session", "-d", "-s", "rc-y", "sleep 30")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._shim("has-session", "-t", "=rc-y").returncode, 0)
        self._shim("kill-server")
        server.wait(timeout=10)
        self.assertEqual(list(self.fallback.iterdir()), [])  # never touched


if __name__ == "__main__":
    unittest.main()
