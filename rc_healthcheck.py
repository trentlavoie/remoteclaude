#!/usr/bin/env python3
"""Login-health watchdog for the RC launcher.

`claude remote-control` needs a valid claude.ai OAuth login. If it lapses
(long idle, a `claude logout` to clear relay ghosts, a revoked token), every
project tap dies silently and you can't re-login from the phone — you're
locked out until you're back at the host. This runs on a timer (a LaunchAgent on
macOS, the rc-healthcheck.timer systemd --user unit on Linux), checks `claude auth
status`, free disk and the launcher's /version, and alerts on failure so you fix it
before you need it. Healthy runs just append a line to the log and stay quiet.

Alerts always go to this process's stderr (the journal under systemd, tagged warning, so
`journalctl --user -p warning -u rc-healthcheck` lists them). Set RC_NOTIFY_URL to an ntfy
topic (or any http(s) webhook) to also get a phone push — on a headless server that is the
only alert that reaches you. A desktop notification is tried too where one exists (macOS,
or Linux with notify-send). The exit status is non-zero when anything is wrong, so a failing
watchdog also shows in `systemctl --user --failed`.
"""

import contextlib
import http.client
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime

from rc_claude import MT, auth_status

NOTIFY_URL = os.environ.get("RC_NOTIFY_URL", "")
# Read from env, not by importing rc_config — the watchdog stays independent of the launcher
# tree so it still runs when the launcher is broken. Defaults match rc_config's own.
SHARE = os.path.realpath(
    os.path.expanduser(os.environ.get("RC_SHARE_DIR", "~/rc-share"))
)
PORT = int(
    os.environ.get("RC_LAUNCHER_PORT") or "8787"
)  # empty env must not ValueError
# absolute floor beats a percent: 10% of 1 TB is still 100 GB of false calm
MIN_FREE_GB = 5.0
LIVENESS_TIMEOUT = 5.0
PUSH_TIMEOUT = 10.0
# open() past any HTTP_PROXY: a corporate proxy must not intercept the localhost probe and
# report the launcher down. Named so tests can stub it.
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def _push_url() -> str:
    """RC_NOTIFY_URL when it is an http(s) URL, else ''. urlopen would also follow file:,
    ftp: and data: URLs, which an alert channel never needs. https is verified against the
    system CA store (urllib's default SSL context checks the chain and the hostname); plain
    http only makes sense to a loopback or tailnet (WireGuard-encrypted) listener."""
    scheme = urllib.parse.urlsplit(NOTIFY_URL).scheme.lower()
    return NOTIFY_URL if scheme in ("https", "http") else ""


def _log(line: str, warning: bool = False) -> None:
    """One line to stderr. Under systemd (JOURNAL_STREAM set) a '<4>' prefix files it at
    warning priority in the journal; elsewhere (a launchd log file, a tty) it stays plain."""
    prefix = "<4>" if warning and os.environ.get("JOURNAL_STREAM") else ""
    print(f"{prefix}{line}", file=sys.stderr, flush=True)


def notify(title: str, msg: str) -> None:
    _log(f"ALERT {title}: {msg}", warning=True)
    desktop: list[str] = []
    if platform.system() == "Darwin":
        # the text travels as argv, never spliced into the AppleScript source
        desktop = [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e",
            "end run",
            msg,
            title,
        ]
    elif shutil.which("notify-send"):
        desktop = ["notify-send", title, msg]
    if desktop:
        # a headless box has no notification daemon: the desktop path must never hang the run
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(desktop, capture_output=True, timeout=PUSH_TIMEOUT)
    if not NOTIFY_URL:
        return
    if not (url := _push_url()):
        _log("RC_NOTIFY_URL ignored: only http(s) URLs are pushed to")
        return
    req = urllib.request.Request(url, data=msg.encode(), headers={"Title": title})
    try:  # _push_url allowlists http(s): B310 (file:/custom schemes) cannot apply
        urllib.request.urlopen(req, timeout=PUSH_TIMEOUT)  # nosec B310
    except (OSError, ValueError, http.client.HTTPException) as e:
        # the exception type only: its text can carry the URL, and the URL (the ntfy topic)
        # is the secret that lets anyone read or forge these alerts
        _log(f"alert push failed: {type(e).__name__}")


def check_disk() -> int:
    """Notify if the boot volume or the share is running out — the failure that silently
    downed the launcher before. Each path is probed independently; returns how many are
    low."""
    low = 0
    for label, path in (("boot volume", "/"), ("rc-share", SHARE)):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        free_gb = st.f_bavail * st.f_frsize / 1024**3
        if free_gb < MIN_FREE_GB:
            low += 1
            notify(
                "RC launcher: low disk", f"{label} ({path}) has {free_gb:.1f} GiB free"
            )
    return low


def check_launcher() -> str:
    """GET the unauthenticated /version. Notify if the launcher isn't answering (a crash-loop
    or wedge the login check can't see), and return the running build stamp for the log —
    blank when unreachable."""
    try:
        with _open(f"http://127.0.0.1:{PORT}/version", timeout=LIVENESS_TIMEOUT) as r:
            body = json.loads(r.read())
        if isinstance(body, dict) and "version" in body:
            return body["version"]
        problem = (
            "unexpected /version response"  # something other than the launcher answered
        )
    except (OSError, ValueError, http.client.HTTPException) as e:
        # refused / timeout / HTTPError(OSError) / bad JSON / truncated or malformed HTTP
        problem = str(e) or type(e).__name__
    notify("RC launcher: not responding", f"/version on :{PORT}: {problem}")
    return ""


def main() -> int:
    """Run every probe; return how many of the three (login, liveness, disk) found a
    problem — the process exit status is non-zero when any did."""
    # the watchdog can afford a longer probe than the badge
    state, detail = auth_status(timeout=20)
    version = (
        check_launcher()
    )  # notifies if the launcher is down; returns the live build
    print(
        f"{datetime.now(MT):%Y-%m-%d %H:%M:%S} MT  login={state} build={version} "
        f"{detail}".rstrip(),
        flush=True,
    )
    if state != "ok":
        notify(
            "RC launcher: login problem",
            f"claude auth status = {state}. Run `claude /login` on the host to "
            "keep Remote Control working.",
        )
    return sum((state != "ok", not version, bool(check_disk())))


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
