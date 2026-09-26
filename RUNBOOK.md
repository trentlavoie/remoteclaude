# Remote Control launcher

Start a Claude Code **Remote Control** session on the Mac from your phone, in
any one of dozens of project directories, with that project's full context —
without SSH, without leaving VS Code running, and without the Mac running
Tailscale.

> **Host:** this runbook is written for the macOS (launchd) host. `install.sh`
> also targets Linux via `systemd --user`; the daily-use, security, and design
> sections apply to both. Only the launchd, `pmset`, and auto-login specifics are
> macOS-only. **This fork deploys on a Linux server behind Tailscale:** read
> [Linux + Tailscale host](#linux--tailscale-host-this-fork) first; where it and the
> macOS text below disagree, it wins.

## Linux + Tailscale host (this fork)

A headless Ubuntu 24.04 server with a public IP, systemd --user, reached only over the
tailnet. Network setup, tailnet policy and firewall: [docs/TAILSCALE.md](docs/TAILSCALE.md).

| Topic | macOS text below | This fork |
|---|---|---|
| Reach | `http://<mac-lan-ip>:8787` over a router subnet route | `https://<host>.<tailnet>.ts.net/` via `tailscale serve`; the launcher is bound to `127.0.0.1` |
| Services | two LaunchAgents | `rc-tmux.service` (holds the sessions), `rc-launcher.service` (web), `rc-healthcheck.timer` |
| Settings | plist env, set via `RC_X=... ./install.sh` | `~/.config/rc-launcher/rc-launcher.env` (0600; `RC_X=... ./install.sh` still writes it) |
| Logs | `/tmp/rc-launcher.log`, `/tmp/rc-healthcheck.log` | `journalctl --user -u rc-launcher` / `-u rc-healthcheck` / `-u rc-tmux` |
| Watch a session | `tmux attach -t rc-<proj>` | `deploy/rc-tmux attach -t rc-<proj>` (plain `tmux` is your personal server) |
| Survive logout/reboot | `pmset` + auto-login | lingering: `loginctl show-user $USER -p Linger` must say `yes` (else `sudo loginctl enable-linger $USER`) |
| Login storage | GUI keychain (`security unlock-keychain` over SSH) | `~/.claude/.credentials.json`; no keychain step |
| Alerts | desktop + optional ntfy | journal (`-p warning`) + `RC_NOTIFY_URL` (ntfy): the only push a headless box has |
| Turn-state hook | registered by `install.sh` | opt-in: `./install.sh --hook` (backup + atomic replace; `uninstall.sh` removes it) |
| File share | always on | off unless `RC_SHARE_ENABLED=1` |

### Deploy, in order

| # | Step | Who |
|---|---|---|
| 1 | `./install.sh` (settings via the env file or `RC_X=... ./install.sh`; see `deploy/rc-launcher.env.example`) | you, no sudo |
| 2 | `bash deploy/install-tailscale.sh` from an SSH shell | you, **sudo** |
| 3 | Admin console: MagicDNS, HTTPS Certificates, disable key expiry for the server, tailnet policy | you, browser |
| 4 | `deploy/tailscale-serve.sh` | no sudo |
| 5 | Put the `RC_ALLOWED_HOSTS` / `RC_TAILSCALE_USERS` lines it prints in the env file, `./install.sh --reload` | no sudo |
| 6 | Phone: Tailscale app on, open `https://<host>.<tailnet>.ts.net/?token=<token>` once, Add to Home Screen | you |

`./install.sh --render DIR` writes the units and env file it would install into `DIR` and
stops, so you can read exactly what will run (`systemd-analyze --user verify DIR/*.service`).

### Why two units, and what restarts what

A process belongs to the cgroup of the unit that started it. If the launcher's first
`tmux new-session` started the tmux server, every Claude session would live inside
`rc-launcher.service`: a restart would kill them all (the default `KillMode=control-group`),
and any sandboxing of the launcher would apply to them. `KillMode=process` would only hide
that (orphans left in a dead unit's cgroup). So the server has a unit of its own:

- `rc-tmux.service` runs `tmux -D` on the exact socket `$XDG_RUNTIME_DIR/rc-tmux/tmux.sock`
  (its RuntimeDirectory), with its own config. `deploy/rc-tmux` is the only client and is
  what `RC_TMUX_BIN` points at. `-S` has no fallback: `TMUX_TMPDIR` / `-L` silently fall
  back to `/tmp/tmux-$UID/`, your personal server. While the unit is down its directory is
  gone, so a launch fails ("tmux new-session failed") instead of starting a server inside
  the launcher.
- `./install.sh --reload` (= `systemctl --user restart rc-launcher`) is always safe:
  sessions keep running. Re-running `./install.sh` restarts the launcher, never rc-tmux.
- `systemctl --user restart rc-tmux` (or stop) ends every session: systemd SIGTERMs each
  claude, which flushes its transcript and deregisters from the relay, so each thread stays
  resumable. Stopping it also stops the launcher (`Requires=`).

Hardening: the launcher and watchdog units get `NoNewPrivileges`, `RestrictNamespaces`,
`RestrictAddressFamilies`, `RestrictSUIDSGID`, `LockPersonality`, a syscall deny list,
`UMask=0077` and memory/task caps. Not `ProtectSystem`/`ProtectHome`/`PrivateTmp`: in a
user unit they need unprivileged user namespaces, which Ubuntu 24.04's AppArmor denies
(the unit would not start), and the launcher must write `$HOME` anyway (`~/.claude.json`).
Not `MemoryDenyWriteExecute`: the launcher runs `claude auth status`, a JIT runtime.
`rc-tmux.service` gets only `NoNewPrivileges` (no sudo/su/pkexec from a phone session),
because the sessions need your full environment.

### Verify (Linux)

1. `systemctl --user status rc-tmux rc-launcher rc-healthcheck.timer`: all active.
2. `curl -s localhost:8787/version`: the build stamp (after `--reload`, a new one).
3. `deploy/tailscale-serve.sh --status`: your URL proxies to `http://127.0.0.1:8787`.
4. From the phone, tap a project; `deploy/rc-tmux ls` shows `rc-<project>`, and the Claude
   app shows it with a green dot.
5. `systemctl --user start rc-healthcheck; journalctl --user -u rc-healthcheck -n 3`:
   `login=ok build=<stamp>`. Alerts: `journalctl --user -u rc-healthcheck -p warning`.

### Remote re-login (Linux)

Same as below minus the keychain: SSH in (over the tailnet or the public SSH port),
`~/.local/bin/claude auth login`, open the printed URL on the phone, sign in, paste the code
back. `~/.local/bin/claude auth status` shows `loggedIn: true` when done.

### Desk side on this host

A `claude` you start over SSH inside a project counts as a desk session: `/launch` answers
"already (desk)" instead of starting a second one. The desk guard (`rc_guard.sh`) only sees
the phone sessions if it uses the same tmux client: `export
RC_TMUX_BIN=~/workspace/remoteclaude/deploy/rc-tmux` in the shell that sources it.
`RC_SPAWN=worktree` (this fork's setting) launches fresh in a worktree; resume/takeover do
not apply to it, and upstream lists that spawn mode as unverified on current Claude Code
builds, so check the first launch.

## How it works

- A tiny always-on web server on the Mac (`rc_launcher.py`) shows a searchable
  list of every directory under `~/projects`. Tap one.
- That launches claude **in the project's root** — `claude --continue
  --remote-control <project>` when a desk-resumable thread exists, else the fresh
  flag form `claude --remote-control <project>` (never the `remote-control`
  subcommand for same-dir: it births relay-only threads nothing can resume) —
  held in a detached `tmux` session so it survives the request returning.
- The session shows up in the **Claude app → Code** with a green dot. You drive
  it from there. Because it launched in the project root, it loads that project's
  `CLAUDE.md`, `.claude/` settings and project MCP exactly like the VS Code
  extension does.
- The phone reaches the launcher at the Mac's LAN IP (`<mac-lan-ip>:8787`)
  through the **Tailscale subnet route on your router** — the Mac itself needs no
  Tailscale. The Claude app reaches the RC sessions over Anthropic's own relay,
  so that path needs nothing from you.

Per-project switching genuinely requires a per-directory launch: `claude
remote-control` is rooted at its launch cwd. One server already accepts up to 32
concurrent sessions *within* a directory, so the launcher only handles switching
*between* projects.

## What runs on the Mac

This is the whole Mac-side footprint. Take the network items to ops-notes.

| Piece | What | State |
|---|---|---|
| `rc_launcher.py` + `com.matt.rc-launcher` LaunchAgent | always-on web server, one cheap process, binds `0.0.0.0:8787`, token-guarded | installed by `install.sh` |
| `rc_healthcheck.py` + `com.matt.rc-healthcheck` LaunchAgent | health watchdog, every 30 min: `claude auth status` (login), free space (< 5 GiB alert), and a `/version` GET (launcher wedged/down); notifies on any | installed by `install.sh` |
| `tmux` | holds each launched RC session | `brew install tmux` (was missing) |
| `claude` binary | `~/.local/bin/claude` v2.1.258, logged in once via `/login` so the OAuth token is cached | present; confirm login |
| `pmset` | `autorestart 1`, `sleep 0`, `disksleep 0` so it powers back on and stays awake | manual (sudo) |
| auto-login | temporary, so the user session + keychain load at boot and RC can authenticate | manual (System Settings) |
| Remote Login (SSH) | optional fallback way in | manual (System Settings) |

## Module map

One job per file, and the import graph is acyclic — every arrow points down this table, so
a change to a leaf can't reach back into the launcher.

| Module | Its one job | Imports |
|---|---|---|
| `rc_claude.py` | the `claude` binary path, the `claude auth status` contract, MT | — |
| `rc_state.py` | the turn-state vocabulary (names, rank, dir, TTL) shared by hook/status/launcher | — |
| `rc_tmux.py` | tmux spoken once: the binary, `tmux()`, session naming, `has_session`, `graceful_stop` | — |
| `rc_templates.py` | the chunks both pages share, plus `fill()`/`js()` | — |
| `rc_page.py`, `rc_files_page.py` | one page template each, spliced at import | `rc_templates` |
| `rc_config.py` | every env-derived setting, `log_event`, `projects()`, the TTL-cache decorator | `rc_claude` |
| `rc_git.py` | per-project branch/dirty for the badges, and the opt-in `RC_SNAPSHOT` checkpoint | `rc_config` |
| `rc_desk.py` | finding, badging and closing desk (non-remote) claude sessions | `rc_config` |
| `rc_sessions.py` | starting, describing and closing the sessions a phone tap drives | the four above |
| `rc_share.py` | the `~/rc-share` boundary, its listing, and the `.rcpart` sweep | `rc_config`, page |
| `rc_launcher.py` | the HTTP tier: auth, routing, framing, `/files` byte-pushing | `rc_sessions`, `rc_share` |
| `rc_guard.py` | the desk-side launch guard (attach / takeover / fresh) | `rc_tmux` |
| `rc_status.py`, `rc_state_hook.py`, `rc_healthcheck.py` | standalone entry points: prompt tag, hook writer, health watchdog (login + disk + launcher liveness) | `rc_state` / `rc_claude` |
| `rc_upload.js`, `rc_download.js` | the files page's resumable-upload policy and download size-cap decision, pure and `node --test`-covered | — (spliced into `rc_files_page`) |

`rc_config` exists so the session and file-share clusters can be separate modules at all:
`launch()` reads `SHARE` and both clusters need `log_event`/`projects()`/`NAME_RE` plus the
env globals, so extracting a cluster while importing those from the launcher would have
made this repo's only import cycle. Read settings as `cfg.NAME` at call time, never
`from rc_config import NAME` — one binding per name is what lets a test (or the service)
redirect `PARENT` or `SHARE` once and have every module follow.

## Ops-notes (network side — not the Mac)

- **DHCP reservation** for the Mac (`<mac-lan-ip>`) so the bookmarked URL stays
  valid.
- **Tailnet subnet route**: the router advertises `<lan-subnet>`, route approved
  in the tailnet admin.
- **Phone**: Tailscale app installed with "use subnet routes" on.

## Install

```sh
cd ~/projects/remoteclaude
./install.sh
```

It installs tmux if missing, generates a token (stored at
`~/.config/rc-launcher/token`, not in the repo), writes and loads the
LaunchAgent, and prints your phone URL plus the manual steps. Re-runnable.

Then, by hand:

```sh
sudo pmset -a autorestart 1 sleep 0 disksleep 0
~/.local/bin/claude          # run once, /login if not already, then /exit
```

System Settings → Users & Groups → temporary auto-login; optionally General →
Sharing → Remote Login.

On the phone: open `http://<mac-lan-ip>:8787/?token=<token>` (the install
script prints the full URL), then **Add to Home Screen**.

## Daily use

Tap the home-screen icon → type to filter → tap a project → switch to the Claude
app, it's there by name with a green dot. The tapped row shows a spinner until
the session is confirmed up; if it can't start, a toast names why (untrusted
dir, login expired, …) instead of a false "launched". Live projects show a green
dot in the launcher too, refreshed every 5s from `/status`, so a session that
dies on its own clears without a manual reload. Tapping a live row again is a
no-op ("already live"). Rows also show *where* a project is live: **📱** means
the launcher runs it (tmux), **🖥** means a plain desk `claude` is live there
(auto-paired with the app; tapping the row takes it over). The **✕** closes a
session where it lives: on a 📱 row it sends claude the double Ctrl-C its TUI needs,
waits for a clean exit, kills the tmux session only as a fallback, and reports "failed"
if the session is somehow still there; on a
🖥 row it SIGTERMs the desk claude gracefully (transcript flushed, relay
archived, thread still resumable). Recents float to the top (localStorage).

To start a brand-new project, type a name that matches nothing: a dashed
**＋ create & start** row appears (or just press Enter). It makes the folder under
`~/projects`, runs `git init`, writes a one-line `CLAUDE.md`, marks the dir
trusted, and launches the RC session — after that it behaves like any other
project. Names must start with a letter or digit, then letters/digits/dash/underscore
(no dot — a dotted
name is an untargetable tmux session; no path
separators), so a typed name can't escape `~/projects`.

The header shows login health. If it reads "logged out", new sessions will fail
until you run `claude /login` on the Mac — the watchdog also fires a notification
when it first detects this, so you usually hear about it before you tap.

Launches and stops are logged with Mountain-Time stamps to `/tmp/rc-launcher.log`
(`launch greenbutton -> launched`), an audit trail of what you started when.

## Resume & takeover

By default every tap **reopens the project's most recent thread** (`claude --continue
--remote-control <proj>`), so the phone lands where you left off — the recent turns are
right there, the full history scrollable above. Same "most recent session for this dir"
rule the VS Code extension uses.

Resume works in **both directions**. Fresh same-dir launches use the local-first
`--remote-control` *flag* form, so desk and phone converge on one thread pool: a thread
started at the desk continues on the phone, and a thread started from the phone continues
at the desk with plain `claude --continue`. One historical exception: sessions born
relay-only under the old `remote-control` *subcommand* form left transcripts neither side
can reopen — those threads remain app-only. A project affected that way self-heals on its
next tap (the launcher detects there's nothing desk-resumable and launches fresh,
flag-form), and is symmetric from then on. Separately, current Claude Code auto-pairs
interactive desk sessions with the phone app — one session, multiple viewers — which is
why a desk session can appear on the phone with no launcher involvement at all.

Because a resumed thread would make the phone a second live client on it, the launcher
first **hands the project off**: it closes any live *desktop* claude session (VS Code or
terminal) whose working dir is inside that project — SIGTERM, wait for it to flush its
transcript and exit, SIGKILL only if it won't. Scoped strictly by process cwd, so a
desktop session on any *other* project keeps running. The thread survives the handoff
(the transcript is on disk — that's what the phone resumes), but a desktop session caught
mid-turn has that turn cut off.

A brand-new project (create-and-start) has no thread to continue: the launcher checks
up front (`has_desk_thread` — is there a desk-resumable transcript?) and goes straight
to the fresh flag-form launch, so nothing breaks and no doomed resume attempt is paid.
The exit-1 fallback still exists underneath as a safety net for other startup deaths.

Toggles (set at install time, e.g. `RC_RESUME=off ./install.sh`):
- `RC_RESUME` — `continue` (default), `fork` (branch a new thread from the last one,
  leaving the desktop thread untouched), or `off` (always start fresh).
- `RC_TAKEOVER` — `1` (default) closes the project's desktop session first; `0` leaves it
  running and you accept two clients on one thread.

Resume applies to `same-dir` spawn (the default); `RC_SPAWN=worktree`/`session` always
launch fresh, since those modes are isolated by design. Takeover uses `pgrep`/`ps`/`lsof`
and is verified on macOS; on Linux it reads `/proc/<pid>/cwd` and should behave the
same, but has not been exercised there — treat Linux takeover as untested, not as off.

Caveat: if VS Code auto-restarts the session it just lost and reattaches, it re-creates
the collision — close that panel rather than letting it reconnect while you drive from the
phone.

### Desk-side shell integration (bash/zsh, opt-in)

The launcher guards the phone→desk direction above; two snippets in this repo cover
the desk side. Both are opt-in — nothing sources them for you:

- **`rc_prompt.zsh`** — a right-prompt tag (`rc:working` / `rc:waiting`) when a
  phone-driven turn is live in the repo you're standing in. `source` it from `~/.zshrc`
  and you're done (it sets `RPROMPT`; fold `$(_rc_prompt)` into yours if you already use
  one). zsh-only (it's a prompt hook).
- **`rc_guard.sh`** — the desk→phone launch guard, sourceable from bash or zsh (the
  logic lives in `rc_guard.py`, plain stdlib Python; the shim just maps its exit code
  to `_RC_GUARD_ARGS`). Without it, typing `claude` in a
  project whose `rc-*` session is live walks into the thread that session is holding
  (`--continue` dies with "No conversation found to continue"; a fresh launch silently
  ignores the running session). With it, the wrapper stops first and offers: **attach**
  to the running tmux session (one conversation, phone and desk both driving it),
  **take over** (close the remote session the way `stop()` does — double Ctrl-C so it
  deregisters from the relay and flushes its transcript — then resume the thread at the
  desk), a **separate fresh** session, or quit.

Minimal wiring, if `claude` isn't already a function in your `~/.zshrc` / `~/.bashrc`:

```sh
source /path/to/remoteclaude/rc_guard.sh
claude() {
  _rc_guard "$@" || return
  # $_RC_GUARD_ARGS is "--new" when you chose "separate fresh session" — it is a
  # signal for YOUR resume logic (skip --continue), never an argument to claude.
  command claude "$@"
}
```

If your wrapper auto-resumes (emits `--continue` when a transcript exists), feed
`$_RC_GUARD_ARGS` to that logic so the fresh choice actually skips the resume. The guard
never prompts for scripts/pipes (no tty) or when you already chose a session yourself
(`-c`/`-r`/`-p`/`--new`), and it matches tmux sessions exactly — a live `rc-alpha-sub`
does not count as `rc-alpha`.

## File share

A dedicated drop directory, `~/rc-share` (`RC_SHARE_DIR`), served over HTTP at `/files`
behind the same token as the launcher — browse, download, resumable upload, long-press
delete — and, if you turn on SMB by hand, mountable as a drive on Windows. It is a directory of its own, never `~/projects` or
`$HOME`: the `/files` handler resolves every request with `realpath` and 404s anything
that lands outside `~/rc-share`, including symlinks that point out of it.

- **Sessions copy files there.** Every phone-launched session inherits `RC_SHARE_DIR` in
  its env, so "copy the build output into the rc" resolves to a known path: `cp
  dist/app.zip "$RC_SHARE_DIR/"`. It appears immediately at `/files` and on the SMB
  mount. **Copy, don't symlink** anything you want to see on the web — a symlink pointing
  back into a project resolves outside the share and is refused over HTTP by design (it
  would work over SMB, but not both). See the global CLAUDE.md "rc file share" note.
- **Browse from the phone.** Tap **files** in the launcher header, or open
  `http://<mac-lan-ip>:8787/files`. Folders drill in; files stream (PDFs and images
  preview inline, everything else downloads). Upload with the **+ upload** button
  (multiple files at once); they PUT into the folder you're viewing. **Long-press** a
  file to delete it (asks first). Uploads and deletes work in the phone app's web view
  too, no app rebuild needed — they're page + server, not native.
- **Mount on Windows (optional).** SMB is an optional second door for drive-letter
  workflows — HTTP already does read + upload + delete (realpath-confined; see the
  Upload protocol section). Do not "restore" a read-only HTTP share: uploads are
  load-bearing for the phone app's share-target and the resumable-upload clients. Enable it once: System Settings → General → Sharing → **File Sharing**, add *only*
  `~/rc-share`, protocol SMB, guest off. Two gotchas the Apple docs skip: tick your user
  **On** in *Options* (that stores the SMB password hash — without it auth fails with no
  clear error), and mount by **IP**, not name — the subnet route carries unicast only, so
  `\\<name>`-style resolution doesn't cross it. Then on Windows: `net use Z:
  \\<mac-lan-ip>\rc-share`. It rides the same subnet route as everything else; nothing is
  exposed to the internet and the Mac still runs no Tailscale.

Writes — HTTP upload, local `cp` from sessions, and SMB from Windows — all land in the
same folder. HTTP uploads are token-gated and `realpath`-confined to `~/rc-share` exactly
like reads (one `within_share()` boundary): a filename that resolves outside it is refused.
The Android share target uploads are **resumable**: bytes stream into a `.rcpart` temp
(hidden from the listing) and a dropped connection resumes from the last received byte —
the app `HEAD`s the path for `X-Rc-Have`, then `PUT`s from that offset with `X-Rc-Total`,
and the server renames to the final name once the temp reaches the total. Matters over a
flaky link (Starlink/Tailscale), where restarting an 82 MB upload from zero is painful.

## Upload protocol

The resumable upload is one wire contract implemented three times: the Python server
(`rc_launcher.py` `_upload`/`do_HEAD`), the browser JS on the `/files` page, and the
Android share target (`UploadActivity`/`UploadLogic`). Change one, check the other two.

- `PUT /files/<path>` streams the body into a hidden `.rcpart` temp, never the final
  name. Request headers:
  - `X-Rc-Offset` — write position in the temp (default 0); the temp is truncated to
    this offset before writing, so a retried slice overwrites cleanly.
  - `X-Rc-Total` — final file size. Defaults to offset + body length; an explicit value
    `<= 0` (or `< offset`) is rejected 400.
  - `X-Rc-Id` — the client's resume identity for the file (the browser uses
    `size-lastModified`). The temp is keyed by `sha1(id)[:12]`, so a stale partial from
    a *different* file of the same name resolves to a different temp and never merges
    into the new upload.
- `HEAD /files/<path>` (same `X-Rc-Id`) answers `X-Rc-Have`: bytes already held in the
  temp, 0 if none. Clients probe before and between PUTs and send from that offset. A
  failed probe means *unknown*, not zero — re-probe rather than restart at 0, which
  would truncate the held partial.
- Responses: a PUT that leaves the temp short of the total returns
  `{"ok":true,"done":false,"have":N}`; `{"done":true}` comes only on finalize, when the
  temp reaches `X-Rc-Total` and is `os.replace`d to the final name — atomic, so a
  reader never sees a partial under the real name. If the client's offset is ahead of
  what the server holds, the server answers `409 {"error":"gap","have":N}` and the
  client backs down to `have`.
- A dropped connection keeps the partial on disk for the next resume; only finalize or
  the sweep removes it.
- Single-writer assumption: clients upload sequentially (one PUT in flight per
  target + id), so the temp needs no lock. A concurrent overlap could corrupt only the
  partial; the finalized file stays atomic regardless.
- Sweep: partials with no writes for 6 h are deleted (an interrupted upload that never
  resumed).

## Verify

1. `cat /tmp/rc-launcher.err` — should be empty; `/tmp/rc-launcher.log` shows the
   bind line.
2. Hit the URL from a laptop browser first; the list should render.
3. Tap one project, then `tmux ls` on the Mac — expect `rc-<project>`. Open the
   Claude app → Code → green dot for that name. **This is the one thing worth
   confirming empirically** — that a headless `tmux`-launched RC session
   registers and shows the green dot.
4. After editing any `rc_*.py`: the launcher holds its code in memory, so the edit
   does nothing until the service restarts. Run `./install.sh --reload` (kickstart on
   macOS, `systemctl --user restart` on Linux — no reinstall), then
   `curl -s localhost:8787/version`: the stamp is a hash of the shipped `rc_*.py` and
   changes when the source does, so a matching new stamp confirms the reload took.

## Troubleshooting

- **Edited `rc_*.py` and nothing changed**: the launcher serves its in-memory code
  until restarted. `./install.sh --reload`, then `curl -s localhost:8787/version` — if the
  stamp didn't change, the reload didn't take. (Same for rotating the token.)
- **No green dot / session dies immediately**: the cached login expired. Run
  `~/.local/bin/claude` and `/login` again. RC needs the claude.ai OAuth token;
  API keys don't work. The launcher header and the watchdog both surface this;
  `cat /tmp/rc-healthcheck.log` shows the last few checks (`login=ok build=<stamp> …`).
- **Low-disk or launcher-down alert**: the watchdog also probes free space (alerts under
  5 GiB on the boot volume or `~/rc-share`) and the launcher itself (GETs `/version`,
  alerts if it is wedged or crash-looping) — not just the login.
- **Want the login-lapse alert on your phone, not just the Mac**: the watchdog
  POSTs to `RC_NOTIFY_URL` if set. Pick an [ntfy.sh](https://ntfy.sh) topic,
  subscribe to it in the ntfy app, then re-run `install.sh` with
  `RC_NOTIFY_URL=https://ntfy.sh/your-topic ./install.sh`. Unset, it only does a
  local macOS notification.
- **Launcher 403**: wrong/missing `?token=`. Re-copy from
  `~/.config/rc-launcher/token`.
- **Phone can't reach the URL**: subnet route not approved or "use subnet routes"
  off on the phone — ops-notes side. Test the LAN IP from the Mac itself first.
- **`tmux: command not found` in the err log**: PATH in the plist; confirm
  `/opt/homebrew/bin/tmux` exists.
- **Turn it off after the power week**: `./uninstall.sh`, and switch auto-login
  back off in System Settings.

## Remote re-login (if the claude.ai login lapses while you're away)

You normally won't need this. The OAuth login refreshes itself, has no short
expiry, and survives reboots. It only drops if you `claude logout`, sign in
somewhere that revokes it, change the subscription, or the Mac's clock is wrong.
`claude setup-token` does **not** help — its token is inference-only and can't
open Remote Control sessions, so the only recovery is an interactive `/login`.

The recovery path is SSH from your phone. Remote Login is on, the Mac is
`<mac-lan-ip>`, reachable over the router's Tailscale subnet route:

1. From an SSH client app on the phone (Termius etc.): `ssh <you>@<mac-lan-ip>`.
2. **`security unlock-keychain`** (prompts for your Mac login password). Do this
   first — an SSH session is a separate security context and can't read the GUI
   login keychain where the OAuth token lives, so without it every `claude`
   command reports `loggedIn: false` even when the GUI session is fine. This is
   the easiest step to forget and the one that makes the rest work.
3. `~/.local/bin/claude auth login`, then press `c` to copy the login URL.
4. Open that URL in the phone browser and sign in (**Continue with Google** —
   the provider is handled entirely in the browser; nothing changes CLI-side).
5. The browser shows a **login code** instead of redirecting — expected, the
   Mac's localhost callback isn't reachable from the phone. Copy it.
6. Paste the code into the SSH session, press Enter. The launcher header flips
   back to "login ok".
7. Tap a project in the launcher as usual; it relaunches remote-control per tap.

Sanity check the whole path *before you leave*: SSH in, `security unlock-keychain`,
then `~/.local/bin/claude auth status` — `loggedIn: true` proves it end to end.

Test the phone→SSH hop *before* you leave — the recovery is useless if you can't
get a shell. And don't `claude logout` on the Mac while away: it kills the RC
login, and it's the same lever the stale-ghost note suggests, so skip it remotely.

## Design decisions (don't "helpfully" undo these)

- **One launcher in `~/projects`, on-demand per-project launch** — not one
  always-on RC server per project. With dozens of projects, per-project daemons don't
  scale; a single parent-folder RC server would read files but wouldn't anchor
  any project's `CLAUDE.md`/`.claude/`. The launch-in-root model is the only one
  that preserves full per-project context.
- **`tmux`, not a bare background process** — RC wants a pty and must outlive the
  HTTP request; tmux gives both and lets you `tmux attach -t rc-<proj>` to see
  status/QR.
- **`same-dir` spawn (the RC default)**, not `worktree` — most of these dirs
  aren't git repos, and worktree mode requires git. Set `RC_SPAWN=worktree` at install time (`RC_SPAWN=worktree ./install.sh` writes it
  into the service env) only for repos you want isolated.
- **Mac needs no Tailscale** — reachability rides the router's subnet route. This
  removes the "Tailscale app must auto-start at boot on the Mac" failure mode,
  which matters for the hands-off-after-power scenario.
- **Token in a 0600 file (`~/.config/rc-launcher/token`), nowhere else** — the
  launcher reads it directly; the plist/systemd unit carry no secret (so
  `launchctl print` can't leak it), it never lands in version control, and
  rotation is write-file + kickstart. Do not "helpfully" put it back in the
  service env or a CI secret — removing it from those channels was a deliberate
  remediation (see CHANGELOG 2026-08-16).
- **Remote taps resume the last thread and take over the desk by default** — `claude
  --continue --remote-control` (flag form; the subcommand can't resume, and the flag form
  doesn't take/need `--spawn` for same-dir) reopens the project's most recent thread, and
  a live desktop session for that project is closed first so the phone isn't a second
  client on it. Scoped by process cwd inside the project, never other projects. Falls back
  to a fresh launch when there's nothing to resume. `RC_RESUME=off` / `RC_TAKEOVER=0` opt
  out; resume is same-dir only.
- **File share is `realpath`-confined to `~/rc-share`; HTTP does read + upload** — `/files`
  browses, downloads, and PUT-uploads into only `~/rc-share`, with one `within_share()`
  boundary so `..` and escaping symlinks are refused on reads *and* writes; the launcher
  token gates it and it never serves `~/projects`. `tests/test_confinement.py` pins this. The Windows drive-mount is Apple's own
  SMB (config, not code, not a dependency), not WebDAV — WebDAV over plain HTTP on Windows
  needs per-client registry edits, caps transfers at 50 MB by default, and sends the
  password in clear text. `tailscale serve` was rejected for the same reason the Mac runs
  no Tailscale: it needs `tailscaled` on the Mac.
- **(This fork) The Linux server does run Tailscale, and only `tailscale serve`** — a
  public-IP server has no router subnet route to hide behind, so the launcher binds
  loopback and `tailscale serve` is the one door: tailnet-only, HTTPS, identity headers.
  Never Funnel (`deploy/tailscale-serve.sh` refuses it). The sessions' tmux server is its
  own unit (`rc-tmux.service`) so the launcher can restart and be sandboxed; do not fold it
  back into `rc-launcher.service` or switch to `KillMode=process`.

## Eval notes (grounded against the real machine)

- `claude remote-control` confirmed in v2.1.169; flags `--name`, `--spawn`
  (`same-dir`/`worktree`/`session`), `--capacity` (default 32), `--permission-mode`
  are real. It is a persistent multi-session server.
- `claude` on the interactive shell is a function alias (`claude-sub`); the real
  binary is `~/.local/bin/claude`. Scripts/agents must use the absolute path.
- Resume verified against v2.1.195: `claude --continue --remote-control <name>` brings the
  RC session up with the project's last thread already loaded (the recent turns replay in
  the session view). The `remote-control` subcommand has no resume flag; the flag form
  doesn't accept `--spawn`/`--capacity` (subcommand-only); `--continue` on a dir with no
  history exits 1 under remote-control (hence the fresh-launch fallback), though it starts
  fresh cleanly under `-p`. Desktop-vs-RC sessions are told apart by exe basename
  (`claude`) + absence of `remote-control` in argv; each is tied to a project by its cwd
  (`lsof -d cwd`).
- Re-checked 2026-09-04 against v2.1.258: `--remote-control [name]` is in `claude --help`;
  the `remote-control` *subcommand* (`--name`/`--spawn`, used only by
  `RC_SPAWN=worktree|session`) no longer appears in the top-level help and was not
  re-exercised — treat those two spawn modes as unverified on current builds.
- Projects live in `~/projects` (dozens of dirs), not `~/code`.
- `tmux` was not installed; `install.sh` adds it.
- `python3` for the agent: `/opt/homebrew/bin/python3`.
