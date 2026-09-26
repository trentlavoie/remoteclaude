"""Starting, describing and closing the sessions a phone tap drives.

The cluster the launcher's HTTP layer calls into: pre-accept the workspace trust dialog,
decide resume-vs-fresh, spawn into tmux and prove the session came up, and report what is
live. Everything that talks to a process goes through the leaf modules (rc_tmux, rc_desk,
rc_git), so this file is the policy and they are the mechanics.

`launch()` is deliberately one long function: its steps share the decisions they make
(resuming, env_opts, the fresh-relaunch fallback) and splitting them would only hide the
order they must happen in.
"""

import contextlib
import html
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from types import MappingProxyType

import rc_config as cfg
import rc_desk
import rc_git
import rc_settings
import rc_tmux
from rc_claude import CLAUDE, auth_status, trust_dir
from rc_page import PAGE
from rc_state import RANK, STATE_DIR, valid_states
from rc_templates import fill, js


@cfg.ttl_cached(lambda: cfg.LOGIN_TTL)
def login_status() -> str:
    """'ok' | 'loggedout' | 'unknown'. `claude auth status` spawns a process and the phone
    polls /status every few seconds, so the answer is cached for cfg.LOGIN_TTL."""
    return auth_status()[0]  # shared probe; the badge needs only the state


def session_states() -> dict[str, str]:
    """{project: most-urgent turn state} from the files rc_state_hook.py writes,
    so the UI can show working/waiting, not just live. Stale files are ignored."""
    out: dict[str, str] = {}
    for d in valid_states(STATE_DIR):
        # st is already a RANK key (valid_states)
        proj, st = d.get("project") or "", d["state"]
        if proj and RANK[st] > RANK.get(out.get(proj, ""), 0):
            out[proj] = st
    return out


def status_payload() -> dict:
    """The live view the phone polls: which projects have an rc session, whether the
    login still works, each session's turn state, and the desk sessions the tmux dots
    can't see, plus each repo's branch/dirty so the badges follow a remote turn instead
    of freezing at page load. /status and page() both read it here so they can't drift.
    Git is the expensive key — one `git status` per repo — and the per-project TTL cache
    (cfg.GIT_TTL) bounds it to one fork per repo per window while a viewer is open."""
    projs = cfg.projects()
    running = rc_tmux.running()
    return {
        "projects": projs,
        "running": sorted(running),
        "login": login_status(),
        "states": session_states(),
        "desk": rc_desk.desk_projects(),
        # external RC started outside the launcher, minus any shown as a launcher tmux session
        "extrc": sorted(set(rc_desk.rc_projects()) - running),
        "git": rc_git.git_states(projs),
        "roots": cfg.extra_roots(),
        "model": rc_settings.MODEL,  # the model every launch is pinned to (not per-session)
        # fork only takes effect on a same-dir resume, so report it OFF while worktree is on
        # (worktree launches fresh, never resumes) — else the toggle would lie about launches
        "settings": {
            "fork": rc_settings.resume() == "fork"
            and rc_settings.spawn() == "same-dir",
            "worktree": rc_settings.spawn() == "worktree",
        },
    }


def page() -> bytes:
    """status_payload() rendered as the launcher page, plus the two things only a page
    load pays for: the project list (scanned once and shared with git_states) and the
    per-repo branch/dirty badges."""
    live = status_payload()
    projs = live["projects"]
    return fill(
        PAGE,
        {
            "__PROJECTS__": js(projs),
            "__RUNNING__": js(live["running"]),
            "__STATES__": js(live["states"]),
            "__GITSTATES__": js(live["git"]),
            "__DESK__": js(live["desk"]),
            "__EXT__": js(live["extrc"]),
            "__LOGIN__": js(live["login"]),
            "__SETTINGS__": js(live["settings"]),
            "__MODEL__": html.escape(live["model"]),
            "__HOST__": html.escape(cfg.HOST),
        },
    )


def ensure_trusted(proj: str) -> None:
    """Pre-accept the workspace trust dialog for the project dir. `claude remote-control`
    refuses to start in an untrusted dir, exiting 1 before it registers with the relay — so
    the app never sees the session and the phone tap silently does nothing, and no trust
    dialog is reachable from the phone. The careful write (atomic, fsync'd, 0600, re-merged
    if a live claude wrote the file meanwhile) is rc_claude.trust_dir; a skip is logged,
    never raised, so it can't 500 the launch."""
    if why := trust_dir(cfg.CLAUDE_JSON, cfg.project_dir(proj)):
        cfg.log_event("trust", proj, why)


def rc_name(proj: str) -> str:
    """The Remote Control session name the phone app shows — hostname-prefixed so a
    session's Mac origin is visible in the app's own list (frostwrym/proj)."""
    return f"{cfg.HOST}/{proj}"


def fresh_cmd(proj: str, model: str | None = None) -> list[str]:
    """Fresh-launch invocation. same-dir uses the top-level FLAG form: it starts a
    local-first session whose phone-driven turns land in a normal desk-resumable
    transcript. The `remote-control` subcommand/server form births relay-only threads that
    neither the desk nor the launcher's own --continue can ever reopen (proven 2026-08-16).
    worktree/session keep the subcommand form — the flag form takes no --spawn, and those
    modes are isolated by design, so desk resumability isn't their point. --model pins the
    session's model (global flag, so it precedes the remote-control subcommand); default pin."""
    mflag = ["--model", model or rc_settings.MODEL]
    if (sp := rc_settings.spawn()) == "same-dir":
        return [CLAUDE, *mflag, "--remote-control", rc_name(proj)]
    return [CLAUDE, *mflag, "remote-control", "--name", rc_name(proj), "--spawn", sp]


def launch_cmd(proj: str, model: str | None = None) -> tuple[list[str], bool]:
    """The claude invocation for proj, and whether it resumes. Resume is the top-level flag
    form `claude --continue --remote-control <name>` (the subcommand can't resume); it exists
    only for same-dir, takes no --spawn, and reloads the most recent thread. The fork toggle
    adds --fork-session (branch on resume); worktree flips spawn() off same-dir to the
    subcommand form. --model pins the model on resume too (resume otherwise keeps the thread's
    last). Otherwise launch fresh."""
    if (res := rc_settings.resume()) in (
        "continue",
        "fork",
    ) and rc_settings.spawn() == "same-dir":
        cmd = [CLAUDE, "--model", model or rc_settings.MODEL, "--continue"]
        if res == "fork":
            cmd.append("--fork-session")
        return [*cmd, "--remote-control", rc_name(proj)], True
    return fresh_cmd(proj, model), False


def has_desk_thread(proj: str) -> bool:
    """Anything locally resumable for proj? Desk/flag-form sessions write transcripts with
    entrypoint "cli" (or "claude-vscode"); phone-born relay-only sessions leave only
    "sdk-cli" mirrors that `--continue` refuses. Deciding up front skips the doomed resume
    attempt, whose death can land AFTER _spawn's 3s window and read as a phantom "launched".
    Only the first 256 KiB of each transcript is read: the entrypoint field appears within
    the first records of every real transcript, and transcripts grow to hundreds of MB —
    slurping them whole made every launch tap pay for the largest project's history."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", cfg.project_dir(proj))
    for f in cfg.CLAUDE_PROJECTS.glob(f"{slug}/*.jsonl"):
        with contextlib.suppress(OSError), open(f, "rb") as fh:
            if re.search(rb'"entrypoint":"(cli|claude-vscode)"', fh.read(262144)):
                return True
    return False


def death_reason(sess: str) -> str:
    """Why a just-launched RC session died, read from its dead pane."""
    out = rc_tmux.tmux("capture-pane", "-t", rc_tmux.pane(sess), "-p").stdout
    last = next(
        (
            s
            for ln in reversed(out.splitlines())
            if (s := ln.strip()) and not s.startswith("Pane is dead")
        ),
        "",
    )
    low = last.lower()
    if "trust" in low:
        return "untrusted dir"
    if any(w in low for w in ("auth", "logged out", "log in", "login", "credential")):
        return "login expired — run `claude /login` on the Mac"
    return last[:80] or "exited immediately"


# Interactive-prompt policy: pane sentinel -> (keys to answer with, audit-log note).
# This is PRODUCT policy (the owner's standing "never compact, always full resume"
# choice), kept as data so the next claude prompt is a table row, not a _spawn rewrite.
_PROMPT_ANSWERS = MappingProxyType(
    {
        "Resume from summary": (("Down", "Enter"), "auto-confirmed FULL resume"),
    }
)


def _settle_prompt(sess: str, proj: str) -> str:
    """Detect and answer a known interactive prompt in the freshly-spawned session.
    Claude can survive the liveness window stuck at a prompt the phone never sees —
    it then never registers with the relay, so the tap would read "launched" while
    the session is absent from the app (hit live: the resume-cost prompt on a
    9h/833k-token thread). Returns '' when there is no prompt or it was answered;
    a death reason for an UNKNOWN confirm-style prompt (fail loudly, never
    phantom-succeed)."""
    pane = rc_tmux.tmux("capture-pane", "-t", rc_tmux.pane(sess), "-p").stdout
    for sentinel, (keys, note) in _PROMPT_ANSWERS.items():
        if sentinel in pane:
            # fixed key NAMES from the policy table, never pane text: nothing read back
            # from the session is ever typed into it
            rc_tmux.tmux("send-keys", "-t", rc_tmux.pane(sess), *keys)
            cfg.log_event("launch", proj, note)
            return ""
    if "Enter to confirm" in pane:
        first = next((s for ln in pane.splitlines() if (s := ln.strip())), "prompt")
        return f"stuck at interactive prompt: {first[:60]}"
    return ""


def _spawn(sess: str, proj: str, cmd: list[str], env_opts: list[str]) -> str:
    """Start cmd detached in tmux, rooted in proj. Returns '' if it's still alive
    after the startup window, else the death reason (and kills the session). RC
    dies within ~2s on any startup error — untrusted dir, expired login, or
    nothing to --continue — taking its tmux session with it; remain-on-exit
    holds the dead pane so death_reason can read WHY. tmux hands its one command string
    to `sh -c`, so it is shlex.join'd — every word quoted, never re-parsed as syntax (a
    plain word is left bare, so the line reads as before) — after a `--`."""
    root = cfg.project_dir(proj)
    # raw subprocess.run so new-session stderr hits the log; nonzero = name taken, bail
    try:
        rc = subprocess.run(
            rc_tmux.argv("new-session", "-d", "-s", sess, *env_opts, "-c", root)
            + ["--", shlex.join(cmd)],
            env=rc_tmux.client_env(),
            timeout=rc_tmux.TIMEOUT,
        ).returncode
    except subprocess.TimeoutExpired:
        return "tmux new-session timed out"
    if rc:
        return "tmux new-session failed"
    pane = rc_tmux.pane(sess)
    rc_tmux.tmux("set-option", "-t", pane, "remain-on-exit", "on")
    time.sleep(3)
    dead = rc_tmux.tmux("list-panes", "-t", pane, "-F", "#{pane_dead}").stdout.strip()
    if dead != "0":
        reason = death_reason(sess)
        rc_tmux.tmux("kill-session", "-t", rc_tmux.target(sess))
        return reason
    if reason := _settle_prompt(sess, proj):
        rc_tmux.tmux("kill-session", "-t", rc_tmux.target(sess))
        return reason
    rc_tmux.tmux("set-option", "-t", pane, "remain-on-exit", "off")
    return ""


def _session_env(sess: str, proj: str) -> list[str]:
    """The `-e` pairs a spawned session carries.

    RC_REMOTE/RC_PROJECT tag the session so the state hook fires for remote
    (phone-driven) sessions only, not local desk ones; the sessions the RC server
    spawns inherit them, so rc_status.py can tell when a remote turn is live on the
    shared working tree. PATH goes in per-session, not the plist: tmux sessions inherit
    the tmux SERVER's environment, set by whoever started the server first, so a plist
    PATH is non-deterministic; -e is order-immune and carries to the future systemd
    host. Without ~/.local/bin, MCP servers and hooks claude spawns by name (uvx, uv,
    ruff) fail on phone-launched sessions while working at the desk.
    """
    env = {
        "RC_REMOTE": sess,
        "RC_PROJECT": proj,
        "RC_SHARE_DIR": cfg.SHARE,
        "PATH": f"{os.path.expanduser('~/.local/bin')}:"
        f"{os.environ.get('PATH', '/usr/bin:/bin')}",
    }
    if state_dir := os.environ.get("RC_STATE_DIR"):
        env["RC_STATE_DIR"] = state_dir
    return [opt for name, value in env.items() for opt in ("-e", f"{name}={value}")]


def live_kind(proj: str) -> str | None:
    """Which live session proj already has ("tmux"|"extrc"|"desk") or None, matched by cwd.
    tmux is a live check; the desk/RC scans are force-freshed so a just-died one can't block."""
    if rc_tmux.has_session(rc_tmux.session_name(proj)):
        return "tmux"
    rc_desk.rc_projects.invalidate()
    rc_desk.desk_projects.invalidate()
    # desk before extrc to match the picker badge (desk beats ext) so kind agrees with the ✕
    if proj in rc_desk.desk_projects():
        return "desk"
    if proj in rc_desk.rc_projects():
        return "extrc"
    return None


def launch(proj: str, model: str | None = None) -> tuple[str, str | None]:
    """Idempotent: a project never gets a second session. If one is already live in any form
    (tmux/external RC/desk), return ("already", kind) and start nothing — use it, or /stop
    then /launch to replace it. Desk/external are never killed here (closing one is the ✕).
    model pins this session's model (default the RC_MODEL pin); it has no effect on already."""
    if kind := live_kind(proj):
        return "already", kind
    sess = rc_tmux.session_name(proj)
    ensure_trusted(proj)
    if snap := rc_git.snapshot(proj):
        cfg.log_event("snap", proj, snap)
    env_opts = _session_env(sess, proj)
    cmd, resuming = launch_cmd(proj, model)
    if resuming and not has_desk_thread(proj):
        # brand-new/phone-born: no --continue thread, so skip the doomed resume attempt
        cfg.log_event("resume", proj, "no desk thread; fresh launch")
        cmd, resuming = fresh_cmd(proj, model), False
    reason = _spawn(sess, proj, cmd, env_opts)
    if reason and resuming:
        # resume exits 1 with no thread; fall back to fresh, logging the real death reason
        cfg.log_event("resume", proj, f"fresh relaunch after: {reason}")
        reason = _spawn(sess, proj, fresh_cmd(proj, model), env_opts)
    return ("failed", reason) if reason else ("launched", None)


def stop(proj: str) -> tuple[str, str | None]:
    """Close proj's session however it was started. A launcher tmux session is stopped
    gracefully — double SIGINT so claude deregisters from the relay, kill-session only as the
    fallback, then confirmed. With no tmux session, fall through to an external remote-control
    session (a `claude --remote-control` started outside the launcher) and kill it, so a plain
    /stop closes the project's RC session whether it's tmux or terminal-born. Desk claude is
    never reaped here — that stays the explicit desk ✕ (desk_stop). "idle" when none exists."""
    sess = rc_tmux.session_name(proj)
    if rc_tmux.has_session(sess):
        if rc_tmux.graceful_stop(sess, wait=cfg.STOP_WAIT):
            rc_desk.rc_projects.invalidate()  # the tmux RC claude is gone; drop it from extrc
            return "stopped", None
        return "failed", "still alive after SIGINT and kill-session"
    return _pid_stop(proj, rc_desk.close_remote, "stopext", rc_desk.rc_projects)


def _pid_stop(proj, close, event, cache) -> tuple[str, str | None]:
    """Shared body of the pid-killing ✕s: SIGTERM/wait/SIGKILL via `close`, log, then drop
    `cache` so the badge reflects the change next poll. claude flushes its transcript and
    deregisters on the way out, so the thread stays resumable (desk claude or a tap reopen)."""
    pids = close(proj)
    if pids:
        cfg.log_event(event, proj, ",".join(map(str, pids)))
    cache.invalidate()
    return ("stopped" if pids else "idle"), None


def desk_stop(proj: str) -> tuple[str, str | None]:
    """✕ on a desk-badged row: close the project's auto-paired desk claude. Kept separate
    from stop() on purpose — reaping a desk claude (the user's own desktop session) stays an
    explicit action, never something a plain /stop falls into. RC_TAKEOVER=0 turns it off
    entirely (a shared/headless host where a desk claude may be someone's live work): the
    launcher then never signals a desk process, and says so instead of reporting "idle"."""
    if not rc_desk.TAKEOVER:
        return "failed", "closing desk sessions is disabled (RC_TAKEOVER=0)"
    return _pid_stop(proj, rc_desk.takeover, "stopdesk", rc_desk.desk_projects)


def create(proj: str) -> tuple[str, str | None]:
    """Make a new project dir under PARENT, git-init it, drop a CLAUDE.md stub.

    NAME_RE keeps proj a single path segment, so it can't escape PARENT. git
    runs best-effort: if it's missing the dir and CLAUDE.md still stand and the
    session launches anyway. The route launches it after this returns 'created'.
    """
    if not cfg.NAME_RE.match(proj):
        return (
            "badname",
            "start with a letter or digit, then letters/digits/dash/underscore",
        )
    # a category dir, not a project: /create bypasses the membership guard, so this would
    # otherwise spawn an rc-<group> session projects() never lists and /stop can't reach
    if proj in cfg.GROUPS:
        return "badname", f"{proj} is a category"
    # a root label (even a currently-down root) is not a flat project — don't let create()
    # mkdir PARENT/<label> and permanently shadow an offline root
    if proj in cfg.configured_root_labels():
        return "badname", f"{proj} is a root label"
    path = cfg.project_dir(proj)
    try:
        os.makedirs(path)
    except FileExistsError:  # an existing project, or a second tap racing the first
        return "exists", None
    subprocess.run([cfg.GIT, "init", "-q"], cwd=path, capture_output=True)
    Path(path, "CLAUDE.md").write_text(f"# {proj}\n")
    return "created", None
