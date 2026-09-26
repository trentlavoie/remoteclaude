# Changelog

Human-facing chronological record; newest first. One entry per change — what and why.

- 2026-09-26: Linux + Tailscale deployment (fork). The Linux install now runs three
  systemd --user units: `rc-tmux.service` owns a dedicated tmux server (exact socket
  `$XDG_RUNTIME_DIR/rc-tmux/tmux.sock`, reached only through `deploy/rc-tmux`, which is
  what `RC_TMUX_BIN` points at), `rc-launcher.service` (loopback bind and the tmux client
  pinned in `ExecStart`, settings from a 0600 `~/.config/rc-launcher/rc-launcher.env`,
  userns-free hardening, restart with backoff) and the watchdog timer. Before, the
  launcher's first `tmux new-session` started the server inside the launcher's own cgroup
  (or joined the personal default server), so `install.sh --reload` killed every session.
  Unit templates moved to `deploy/systemd/`; `install.sh --render DIR` writes what it
  would install. `install.sh` no longer runs sudo or `loginctl enable-linger` (it checks
  and prints), writes the token via mktemp+rename, and edits `~/.claude/settings.json`
  only on `--hook`, through `deploy/claude-settings.sh` (0600 timestamped backup, edit a
  private copy, atomic rename; `uninstall.sh` uses the same path to remove it). Fixed:
  `GROUPS=` assigned bash's magic `GROUPS` array, so every install wrote
  `RC_PROJECT_GROUPS=<gid>`; `uninstall.sh` aborted (set -e + pipefail) when no rc-*
  session existed and never removed the hook. New `deploy/tailscale-serve.sh` publishes
  the loopback launcher on the tailnet over HTTPS (never Funnel; refuses while one is on),
  `docs/TAILSCALE.md` covers install, policy, why HTTPS, and firewalling. The watchdog
  logs every alert to the journal, pushes only to http(s) `RC_NOTIFY_URL`s without ever
  logging the URL, and exits non-zero on a problem. Upgrading a Linux host from the old
  single unit ends the sessions running inside it once (they lived in its cgroup).

- 2026-09-19: Fix a Linux-only flaky-CI bug in RouteTest — its setUp mocked subprocess but,
  unlike MockedToolsCase, never forced `os.path.islink` to False, so desk detection read the
  runner's real `/proc/<pid>/cwd` whenever the test's fake pid (321) collided with a live
  process, and the desk/stop route tests failed with the wrong status. macOS has no `/proc`,
  which is why it never reproduced locally. Added the same `islink=False` guard to
  RouteTest.setUp. Test-only; surfaced by the per-launch-model push turning CI red.

- 2026-09-19: Per-launch model selection. `/launch?model=<alias|full-id>` picks the model
  for that one session; the picker now has a model dropdown (defaulting to Sonnet 5, i.e. the
  pin). Aliases (`sonnet|opus|haiku|fable`) map to full IDs, and only allowlisted full IDs
  ever reach the claude argv — an unrecognized value is rejected with `{"status":"failed",
  "reason":"unknown model '<x>'; allowed: ... (or full IDs)"}` before anything spawns, so a
  request string is never passed through as free text. Omitting `model` (or the picker's
  default) keeps the operator pin (`RC_MODEL`, default `claude-sonnet-5`). Because a live
  session's model can't be changed underneath it, an `"already"` answer that carried a
  `model` adds `note: "model not applied; /stop then /launch to switch"`. The chosen model is
  threaded through `launch`/`launch_cmd`/`fresh_cmd` as `--model` on every form (fresh and
  resume). Per-session model reporting in `/status` was deferred.

- 2026-09-19: Idempotent-/launch review follow-ups (no behavior change). Fixed a stale
  rc_page comment that still described the old desk takeover (a tap now returns
  "already (desk)", not a takeover), and removed a dead `j.launch==='already'` branch in
  createProj — a just-created project is never already-live, so launch there only ever
  returns launched/failed.

- 2026-09-19: `/launch` is idempotent — a project never gets a second session. If one is
  already live in any form — a launcher tmux session, an external `claude --remote-control`
  started outside the launcher, or a desktop claude — `/launch` returns `{"status":"already",
  "kind":"tmux|extrc|desk"}` and starts nothing; the caller uses that session, or `/stop`
  then `/launch` to replace it. This reverses the 2026-09-13 takeover (a live session is now
  kept, not reaped) per Matt's "never open two of the same": takeover could never safely kill
  an external or desktop session (Matt's own terminal tabs), and idempotency prevents the
  duplicate `/launch` used to start beside a terminal/desktop session. Liveness is matched to
  a project by the process's working directory; the tmux check is live and the desk/RC scans
  are force-freshed before the check so a session that died within the poll window can't
  linger in the cache and refuse a real launch. The picker turns "already (desk)" into a
  stop-first prompt rather than a bare failure. Removed: the tmux-reap and desk-takeover from
  `launch()` (the `takeover()` helper stays for the desk ✕). KNOWN ACCEPTED RACE: the
  liveness check → spawn window is guarded only for tmux (the `new-session` name-collision
  check); an external RC or desktop claude that starts in that window can still slip past
  into a second session beside it — accepted, launches are infrequent taps.

- 2026-09-19: Every launched session is pinned to a model — `--model $RC_MODEL` (default
  `claude-sonnet-5`) on every launch form: the same-dir fresh flag form, the resume
  (`--continue`) form, and the worktree/session subcommand form (as a global flag before the
  subcommand). Matt wants launcher sessions on Sonnet 5 100% of the time. This matters on
  resume specifically: a resumed thread otherwise keeps whatever model it last ran on
  (verified live 2026-09-19 — two resumed sessions came up on Opus 5 while the settings
  default was `sonnet`), and `claude --continue --model X` was confirmed to override that
  (fresh run recorded claude-sonnet-5, the resumed run with `--model claude-opus-4-8`
  recorded opus-4-8). `RC_MODEL` lives in rc_settings; `/status` and the picker header show
  the pinned model (the config value, not a per-session transcript read).

- 2026-09-14: Plain `/stop` now closes a project's Remote Control session however it was
  started — a launcher tmux session (graceful double-SIGINT) or, failing that, an external
  `claude --remote-control` started outside the launcher (SIGTERM/SIGKILL). So `ext=1` is
  now redundant for RC sessions and an API caller need not know the launch method. Peer
  (home-ops) hit this repeatedly: `/stop?proj=X` returned "idle" for terminal-born RC
  sessions it hadn't launched. The `stop()` tmux-first check makes the fallback safe (a live
  tmux session takes the graceful path, never a pid-kill); `remote_stop` and the `/stop?ext=1`
  route branch are removed as redundant (a legacy `ext=1` is ignored, not an error). The
  fallback deliberately stops at external RC — a plain `/stop` never reaps a desk claude
  (the user's own desktop session); that stays the explicit desk ✕ / `desk=1`.

- 2026-09-13: The launcher now surfaces external Remote Control sessions — a
  `claude --remote-control` started in a terminal or by the app, not by a launcher tap.
  Matt had five such sessions live (home-ops, claims, remoteclaude, raidrush, home-projects)
  showing nothing in the launcher, because detection only knew launcher tmux `rc-` sessions
  (📱) and plain desk claude (🖥), and explicitly excluded `--remote-control` processes from
  the desk scan. They now get a third badge (📡, purple) matched to their project by cwd, with
  a ✕ that closes them. `rc_desk`'s process scan was generalized to classify RC vs non-RC;
  `status_payload` reports `extrc = rc_projects() - running` (a project with a launcher tmux
  session stays 📱, not external). `remote_stop` guards against pid-killing a launcher-managed
  session (routes to the tmux stop if the project actually has a session), and `stop()` now
  invalidates the RC scan cache so a just-closed session doesn't briefly reappear as external.
  Size cap raised 400->410 (rc_sessions); a view/lifecycle split of rc_sessions is the
  committed next refactor. NOTE: none of this is live until the launcher is reloaded.

- 2026-09-13: Remote Control session names are now hostname-prefixed (`frostwrym/proj`), so
  the Claude mobile app's own session list shows which sessions were started on the Mac —
  Matt wanted the app itself to say a session came from the computer. Display-only via a new
  `rc_name()`; the tmux session name and all launcher tracking are unchanged.

- 2026-09-13: A phone-editable settings panel on the launcher page with two toggles — Fork
  (branch the conversation on resume) and Worktree (isolate each session in its own git
  worktree) — both default off and remembered across restarts. Matt uses neither and wanted
  them switchable rather than env-only. New `rc_settings.py` holds the persisted store
  (`~/.config/rc-launcher/settings.json`, atomic write mirroring `add_root`) and resolves
  `resume()`/`spawn()` from it, falling back to the `RC_RESUME`/`RC_SPAWN` env defaults when
  unset; a torn/absent/non-UTF8/malformed file reads as defaults so it can't break a launch,
  and concurrent toggle taps are serialized so neither is lost. Fork only applies to a
  same-dir resume, so the UI reports it off and disables its checkbox while Worktree is on
  (a worktree session launches fresh and never resumes). New GET `/settings` route
  (token-gated). Extracted to its own module because it pushed `rc_config` over the
  size cap; the cap was raised 375->400 for the takeover + settings growth (see
  tests/test_size_cap.py).

- 2026-09-13: `/launch` now always takes over. A live `rc-<proj>` session is no longer a
  no-op "already" — launch reaps it (graceful stop) and any desktop claude rooted in the
  project, then starts one fresh remote session, so relaunching from the phone lands a single
  clean client instead of doing nothing or racing a second client onto the same thread. Matt
  hit this: a launcher/API-started session plus a hand-started `claude --continue` in the same
  dir left two clients colliding, and a phone relaunch just returned "already". The `RC_TAKEOVER`
  opt-out is removed (Matt never used the "leave it running" behavior and wanted no switch);
  takeover is unconditional. A prior session that survives SIGINT and kill-session now fails
  loudly ("prior session would not stop for takeover") rather than silently attaching to and
  reporting "launched" over the old one. Limit unchanged: a pure phone/relay session with no
  local process can't be evicted — the `claude` CLI has no non-interactive takeover flag.

- 2026-09-13: `stop()` returns "idle" (not "stopped") when no `rc-<proj>` session exists, so a
  wrong or unmanaged proj no longer reads as a successful kill — flagged by the home-ops
  session after `/stop` on a non-launcher session returned `{"status":"stopped"}` though
  nothing was killed. Mirrors `desk_stop`'s idle/stopped vocabulary; the page already renders
  "idle" as "was already closed". "stopped" is now only a confirmed kill of a live session.

- 2026-09-12: README "File share" now states the share is read-write and that secrets must
  never be left in it — because a global CLAUDE.md audit (home-ops) found this repo's docs were
  the only place the writable fact was missing, and the global file had once been condensed to
  "read-only" for lack of a second source. Edited from the home-ops session.

- Paid down the 2026-09-06 audit (all nine findings).
  - **Docs caught up to the trio.** RUNBOOK Verify/Troubleshooting now cover `install.sh
    --reload` + `curl :8787/version` (the stale-serve fix was undocumented); the watchdog is
    described as login + disk + launcher-liveness (not login-only) in RUNBOOK and README, log
    sample updated to `login=ok build=<stamp>`; module maps gained rc_upload.js/rc_download.js
    and rc_claude/rc_state.
  - **Single-sourced the settings.json hook merge.** rc_state_hook gained install_hook/
    remove_hook (+ `--install-hook`/`--remove-hook` CLI); install.sh/uninstall.sh call them
    instead of embedding the JSON-merge Python twice. Hardened per the panel: remove iterates
    only the six EVENTS and skips non-list entries (an unrelated/corrupt event can't abort
    uninstall), empty/malformed settings.json is left unchanged with a message, and it is the
    exact inverse of install. First real test coverage of that merge.
  - **Small fixes.** rc_config PORT tolerates an empty `RC_LAUNCHER_PORT` (matches the
    healthcheck); MIN_FREE_GB straddled at 5.0 so the floor can't silently drift; a tracked
    `tests/test_size_cap.py` enforces the 375-line module cap (was honor-system, silently
    violated) and `.claude/review.toml` bumped to 375 (rc_sessions is honest work, not a
    split); `ty check rc_*.py` added to CI; _build_stamp docstring corrected; an
    ensure_trusted concurrency stress test. Two trailing-comment reflows cleaned.
## 2026-09-06

- Multiple project roots + an interface to add them. Beyond the primary `~/projects`, the
  launcher now serves additional root directories: each root's children list as
  `<basename>/<name>` (reusing the group/name identity — tmux `+` substitution, desk badge,
  membership guard all carry over), so projects can live in several locations with no reorg.
  Roots come from `RC_PROJECT_ROOTS` (env) plus a runtime-editable `~/.config/rc-launcher/
  roots.json` that a token-gated `GET /addroot` appends to, so a root added from the phone's
  "+ root" control shows on the next poll with no reload. Designed via an ultracode workflow
  (model + route/UI + security pre-mortem) and hardened through a defect pass + 3-model panel:
  JSON persistence (not line-based, so a path can't inject a second root), roots stored
  realpath'd with symlinked children filtered out (containment like the file share), the name
  segment re-validated in `project_dir` so a crafted `..`/absolute part can't escape, add-time
  rejection of `/`/`$HOME`/system-adjacent/PARENT-overlapping/nested roots, `create()` refuses
  a configured root label (even a currently-down one, so it can't shadow it), and the temp
  file is unlinked on a write failure. Backward-compat is structural: with no roots configured
  the primary tree behaves byte-identically. Known limit: `extra_roots()` is uncached on the
  poll thread, so a mount that dies after being added can wedge polls (the same exposure the
  primary root already has).
- Two-level project support (REORG-PLAN Phase 1), opt-in and transition-safe. Set
  `RC_PROJECT_GROUPS` (comma-separated, e.g. `work,hobby`) and any top-level dir with that
  name becomes a category: its children list as `group/name`, while every other dir stays a
  flat project. Empty/unset = flat, so it is a no-op until you declare buckets. `session_name`
  swaps the `/` for `+` (a slash breaks tmux `send-keys` targeting; `+` round-trips and NAME_RE
  forbids it in a segment); `running()` maps it back. Verified live: `claude remote-control
  --name work/aws` accepts the slash. Defect pass + 3-model panel hardened it: groups are
  opt-in (an empty install default had made a five-name code default inert), `create()` rejects
  a category name (it bypasses the membership guard, would spawn an unstoppable `rc-<group>`),
  the desk badge maps grouped projects to `group/name` (else the dot vanished), the inner
  category listdir is guarded, and `RC_PROJECT_GROUPS` trims whitespace. Traversal and the
  `+` round-trip confirmed safe (running() output never reaches a path op). Not done here:
  grouped UI section headers and nested project creation from the phone.
- Project names must now start with a letter or digit — leading-underscore meta/scratch dirs
  (`_archive`, `_baselines`) were listing as projects, the underscore equivalent of the dot-dir
  bug. `NAME_RE` is `^[A-Za-z0-9][A-Za-z0-9_-]*$`: interior `_`/`-` stay valid (`my_project`),
  a leading `_` or `-` (the latter a shell/tmux arg-injection shape) is rejected, in one gate
  shared by `projects()`, `create()`, `stop()` and the page JS. Confirmed live: 74 -> 72.
- Operational trio + a concurrency fix (from the 2026-09-06 improvement analysis).
  - **Healthcheck now watches more than login.** rc_healthcheck gained `check_disk` (statvfs
    on / and the share, alert < 5 GiB — the failure that silently downed the launcher) and
    `check_launcher` (GET the new unauthenticated /version; alert if the launcher is wedged
    or crash-looping, which the login probe can't see). It reads PORT/SHARE from env, still
    importing only rc_claude so it stays independent of the launcher tree.
  - **A verifiable reload.** `install.sh --reload` restarts only the launcher service (no
    reinstall), and a new unauthenticated `/version` returns a 12-hex hash of the shipped
    rc_*.py (`rc_config.VERSION`), so `curl localhost:8787/version` confirms edited code went
    live — a week of commits had served stale until a manual kickstart. Verified end to end:
    edit -> reload -> the stamp advances.
  - **Concurrency fix.** ensure_trusted wrote ~/.claude.json through a FIXED temp name, so two
    concurrent first-time-trust launches of different projects could tear claude's own global
    config and 500 the loser. Now a unique tempfile.mkstemp + os.replace; a write failure
    (disk full) logs and continues instead of 500-ing or orphaning the temp.
  - Gate (defect pass + 3-model panel): check_launcher also catches http.client.HTTPException
    (a truncated/malformed reply) and non-dict JSON; the liveness probe opens past HTTP_PROXY
    so a corporate proxy can't false-alarm; empty RC_LAUNCHER_PORT no longer ValueErrors at
    import; --reload reports a failed restart. All fixes mutation-pinned.

## 2026-09-05

- Worked the 2026-09-05 audit backlog (the actionable items; launcher screenshot excepted).
  Tests: browser download logic extracted to a testable `rc_download.js` (size-cap decision,
  progress text, `hum`) with `node --test` pinning the 256 MB boundary the page never
  asserted; `death_reason` gains a marker-TRAILING case and a `credential` case; `_upload`
  rejects offset>total (400 "bad total"); a symlinked `RC_PROJECTS_PARENT` is pinned to key
  on the physical path. Behavior: `rc_desk.takeover` times its SIGTERM grace with
  `monotonic` (an NTP/DST step can no longer shorten it); `TTLCache.invalidate` prunes the
  single-flight locks too. Cleanup: `cfg.project_dir(proj)` replaces `os.path.join(cfg.PARENT,
  proj)` at 7 sites; the four Kotlin `@Suppress("DEPRECATION")` now say why (systemUiVisibility,
  startActivityForResult, pre-33 getParcelableExtra).
- Gate on that batch (3-model panel): `TTLCache.invalidate` now bumps a generation instead
  of clearing `_inflight` wholesale — a fill in flight when invalidate fires is pre-invalidate
  and its cache write is dropped (the clear had let a stale result overwrite a fresh one,
  last-writer-wins), and each key's single-flight lock is pruned after its own fill; a
  threaded test pins both. `rc_templates` reads the JS assets with `encoding="utf-8"` so a
  C/POSIX-locale host can't crash at import on a non-ASCII byte (latent since rc_upload.js).
  The files page treats an unknown Content-Length as native download (no buffering an
  unbounded body).
- Project names no longer allow a dot. A `.` in a name is a tmux target metacharacter:
  `=rc-<name>` parses as `session.pane`, so a dotted session (e.g. an accidentally-created
  `.claude`) is untargetable — `stop()` operates on a nonexistent pane, and `graceful_stop`
  reads "no such session" as gone and reports "stopped" while the session lives on. Dropping
  `.` from `NAME_RE` (the one gate `projects()`, `create()` and `stop()` share) also stops
  dot-directories like `.claude`/`.ruff_cache` being listed as projects. No existing project
  dir had a dot.
- Downloads confirm like uploads do: tapping a file on the files page shows "⬇ name…" in a
  new status line, then "✓ downloaded name (size)" or "✗ name failed". In a browser the page
  fetches the bytes itself (streamed, with a percentage while it runs) and hands them to a
  download link — buffered in memory, so it suits the share's documents rather than
  multi-GB files. In the Android app the native DownloadManager still saves the file (to
  Downloads, with the system notification); the app now tags its UA (`rc-launcher-app/1`),
  keeps the download id → name map, and on the completion broadcast runs
  `rcDownloadDone(name, ok)` in the page — built by a pure `DownloadLogic.doneScript` that
  escapes the name as a JS literal (JVM-tested, incl. a `"</script>` name). Older pages
  without the hook are a no-op.
  Gate (3-model panel + defect pass): `registerReceiver` is branched on API 33 (the flag form
  is mandatory there and absent below); the receiver acts only on a terminal DownloadManager
  status so a spoofed broadcast (it is exported by necessity) cannot mark a running download
  failed; the id→name map is process-wide so an Activity recreation still resolves it; the
  JS-literal escaper covers every C0 control (a NUL in a name); files over 256 MB skip the
  in-memory fetch and go to the browser's native download. Known limit: in a browser the
  programmatic click of the download link happens after the fetch, outside the tap's
  activation window — Firefox/LibreWolf and Chrome honour it, Safari may not. Defect pass:
  the server serves files inline, so in a browser a tap on something it renders (PDF,
  image, text, video) still opens it as before and modifier-clicks stay native — only
  files a browser would save anyway (APK, archives, office docs) take the fetch path;
  downloads run one at a time so the status line is never shared; a disabled Download
  Manager (null `query()`) reads as gone instead of crashing the receiver; the escaper
  tests assert exact strings incl. U+2028/2029; the route test runs `node --check` on
  the page script (skips without node) instead of matching substrings only.

## 2026-09-04

- rc_tmux's module docstring still said the launcher's `stop()` used "the older fire-and-forget
  sequence" — true for the two hours between the cut and 64491ce, false since. Caught by
  `/grade`'s debt-marker count (it matched the word TODO in that sentence). Tier T, doc only.
- Gate on the quality pass: the defect subagent caught the one weakening in it — the
  `lambda *_` stub that vulture's unused-`addr` hint produced no longer pinned that
  `Server.handle_error` forwards exactly `(request, client_address)` to `super()`; the stub
  records the pair again and the test asserts it (mutant `super().handle_error(request)`
  killed). `restore_globals`'s docstring now says its restores run LAST (addCleanup is
  LIFO), the ordering hazard the pass exposed by dropping two fixtures' own `try/finally`.
- Readability pass over what the rc_sessions cut left behind (comments and test
  scaffolding only; no source expression changed, AST-verified per file). Four module
  docstrings still named rc_launcher for jobs that moved — rc_page pointed at
  `rc_launcher.page()`, rc_state and rc_claude listed rc_launcher as the importer, and
  _harness never mentioned MockedToolsCase. Eight `# --- ... ---` section headers in the
  test files described blocks that had travelled to a different file (test_desk's
  "death_reason", test_orchestration's "_run / _pid_cwd / _alive", test_upload's "auth")
  and are deleted; the nine accurate ones stay. 59 statements were exploded across three
  lines only because a trailing comment defeats `ruff format`'s line fit — the comment now
  sits above and the statement is one line, which is the reflow the 2026-09-02 audit asked
  for "when next touching them"; rc_config's env block reads as a settings table now.
  Three tests dropped hand-rolled save/restore of `os.path.islink`/`os.readlink`/`time.time`
  that `restore_globals()` has covered since the cut, and nine exploded
  `addCleanup(setattr, ...)` calls in test_hooks became `_harness.keep()`. 144 tests green
  before and after, 12 node, ruff format + check clean.

- Gate on the cut (defect pass, live oracle): the "graceful" stop never was — claude's TUI
  answers a single Ctrl-C with "Press Ctrl-C again to exit" and stays up (verified live on
  2.1.260: one C-c, or two 4s apart, leave it running; two 0.4s apart exit it), so every ✕
  since the first commit fell through to the SIGHUP kill the docstring said it avoided.
  `graceful_stop` sends the double Ctrl-C now. Also from that pass: `git status` in the
  poll took `index.lock` and would race a desk `git add` — `--no-optional-locks` on every
  launcher git call; `ttl_cached` is single-flight (concurrent misses on a key wait for one
  computation instead of each forking); install.sh's hook-command substitution could fail
  silently under `set -e` (prefix assignments escape errexit) and register six empty
  commands — assigned and checked on its own line; `RC_GIT_TTL`'s new 30s default and the
  `/stop` "failed" payload are pinned at the route; two test classes gained the isolation
  the rest have. Panel: 2 of 3 models, both leads refuted by inspection and recorded.
- `stop()` confirms: it now runs `rc_tmux.graceful_stop()` (SIGINT, wait up to
  `RC_STOP_WAIT`, kill-session only as fallback, then check) and answers "failed" with a
  reason when the session is still alive — the ✕ used to say "stopped" after a 2s sleep
  without looking, and the page now toasts the failure instead of clearing the dot.
- Branch/dirty badges follow the poll: `status_payload()` carries `git` (and the project
  list, so `page()` scans once), the page updates `GITSTATES` on every `/status`; the
  per-project TTL cache bounds it to one `git status` per repo per window, and
  `RC_GIT_TTL` defaults to 30s (was 15) since the poll now drives it.
- The state-hook command string has one source: `rc_state_hook.py --hook-command <repo>`;
  install.sh registers and uninstall.sh removes whatever it prints, and a test asserts the
  literal never reappears in either script (they used to each embed a copy).
- **The `rc_sessions.py` cut, and the leaves it needed** (the 2026-09-02 Next entry, in the
  order that entry specified). rc_launcher.py was 1117 lines doing eight jobs; it is now
  340 and does one — auth, routing, request framing and the /files byte-pushing. Around it:
  `rc_config.py` (every env-derived setting, `log_event`, `projects()`, one `ttl_cached`
  decorator), `rc_tmux.py` (the binary, `tmux()`, `session_name`, `has_session`,
  `graceful_stop`), `rc_git.py` (branch/dirty + the opt-in snapshot), `rc_desk.py` (the
  desk-claude scan and takeover), `rc_sessions.py` (starting, describing and closing the
  sessions a tap drives) and `rc_share.py` (the ~/rc-share boundary, its listing, the
  `.rcpart` sweep). rc_templates.py keeps only what both pages share plus `fill()`/`js()`;
  `rc_page.py` and `rc_files_page.py` hold one template each (verified byte-identical to
  the pre-split constants). The import graph is acyclic and layered — rc_config is what
  makes that possible: `launch()` reads `SHARE` and both clusters need
  `log_event`/`projects()`/`NAME_RE` plus the env globals, so extracting a cluster while
  importing those from the launcher would have been the repo's only cycle. No behavior
  change: 138 tests green before and after, and a live launcher was driven end to end
  (page renders with no unfilled placeholder, /status, /files, a finalized upload, a 403
  logged path-only).
  - Folded in along the way, all from the same entry: `PARENT` is realpath'd the way
    `SHARE` already was (the desk scan and takeover compare physical cwds read back from
    lsof and /proc, so a symlinked or trailing-slashed `~/projects` silently matched
    nothing); the three TTL caches the launcher had grown in three shapes (an lru_cache
    over a time bucket, a dict+lock keyed per project, a tuple+lock+hand-rolled
    invalidate) are one `ttl_cached` decorator with a uniform `.invalidate()`; one
    `status_payload()` now feeds both `page()` and `/status`, which were assembling the
    same four keys twice. `rc_guard.py` drops its second `RC_TMUX_BIN` default, its own
    `has-session` call, its own `rc-{proj}` and its own graceful stop, and calls
    `rc_tmux` — the second real consumer that justified that leaf.
  - **Deviation to note:** the login probe was an `lru_cache` over a 60s wall-clock bucket
    and is now a rolling 60s TTL (`cfg.LOGIN_TTL`, still hardcoded — it is the one TTL
    without an env knob). Same cost profile and the same reason
    for existing, but the expiry is per-entry-age rather than aligned to the minute, so a
    probe taken at :59 now lives to the next :59 instead of expiring at :00.
  - Not changed, deliberately: the launcher's `stop()` still sleeps 2s and reports
    "stopped" without confirming, rather than adopting `rc_tmux.graceful_stop()`. That is
    a product change (the desk-✕ toast item), not a refactor, so it stays in TODO.md.
- Tests follow the source seams: `tests/test_git.py` (the branch/dirty parse, both
  degradations, the cache on both sides, snapshot), `tests/test_desk.py` (the probe scan,
  the cwd boundary, takeover escalation, the TTL), `tests/test_routes.py` (the launcher's
  own routes plus the share page) and `tests/test_files_auth.py` (the gate and its
  keep-alive framing) split out of test_functions/test_orchestration/test_upload; every
  test name is preserved and no assertion was weakened. Two fixtures moved into
  `tests/_harness.py` — `ServerCase` (the loopback server, a tmp SHARE and a known token)
  and `MockedToolsCase` (the whole mocked tmux/git/pgrep world) — which is what let those
  files split without copying a 45-line setUp four times. `restore_globals()` now also
  empties the three TTL caches on both sides of each test, replacing three different
  hand-rolled cache pokes (`_git_cache.clear()`, `_desk_cache = (0.0, [])`,
  `_login_status.cache_clear()`); the desk-stop test asserts the badge rescans on the next
  poll rather than reading a private cache tuple. No tracked .py is over 350 lines now
  (was 1117 and 538).
- Coverage: the measured set in pyproject and ci.yml is all sixteen shipped modules, and
  every one clears the per-file floor (98% aggregate, lowest file 93%). The comment shapes
  `ruff format` had left as three-line `if (\n cond\n ):` blocks in the handler were
  unwrapped with the comment moved above the line, as the 09-04 formatting note asked.


- Audit Now batch (2026-09-02's paydown, all seven entries): the resumed-upload truncate
  is pinned (a re-PUT below `have` must leave `have=450`, not report the stale tail as
  stored) along with `connection: close` on the write-error 200 and the 403 PUT, and a
  handler-level PUT to an escaping path; the four mutation-deletable launcher paths are
  pinned (a fresh launch with a live desk claude kills nothing; `RC_TAKEOVER=0` kills
  nothing on resume; the SIGTERM grace loop must run before SIGKILL; `ensure_trusted`
  lands the trust flag; `RC_PROJECT` is in the session env) — all eight mutants re-run on
  the real tree and killed, source md5-restored and bytecode purged after each; the
  `RC_LAUNCHER_TOKEN` env fallback is deleted (panel-rated High: an env-carried token is
  readable via ps/launchctl and inherited by every child — the channel the 08-16
  remediation closed); two hollow guard tests and the JS 'gives up' test now discriminate
  (the JS one carries the onRetry tripwire so a stall-counting regression fails instead
  of hanging node); `serve_forever(poll_interval=0.05)` — the suite fell from ~20s to
  under 5s. Small fixes promoted by the panel: `create()` catches `FileExistsError` (a
  double-tap raced `os.makedirs` and 500'd), the hook fails loud on an unknown event
  instead of painting "working", `rows_html` renders an OSError as "unreadable" not
  "empty" (its pin flipped in the same commit), `stopSess` toasts "was already closed" on
  an idle desk-✕ instead of lying, the dead `json=1` on the /create fetch is gone, and
  `/launch?desk=1` is pinned to launch (the audit's half-pin).
- Docs: RUNBOOK's Linux-takeover claim was inverted (it said "degrades to a no-op" while
  the code has read `/proc/<pid>/cwd` since the first commit) — now "untested there";
  `RC_SPAWN`/`RC_SNAPSHOT` are written into the service env by install.sh, so the
  "flip it in the plist" instruction survives a re-install; version pin to 2.1.258 with
  a re-verify note (the `remote-control` subcommand behind `RC_SPAWN=worktree|session` no
  longer appears in `claude --help` — those modes are unverified on current builds);
  retired "read-only" wording in install.sh and the `_files` docstring; rc_guard.sh's
  RUNBOOK cross-reference; the Android build's default host is `rc-launcher.local`, not a
  `192.168.1.x` address in a public repo; CHANGELOG backfilled for f364ea8 and 8e989a7.
  Not done: docs/launcher.png needs a phone screenshot of the current page.

- `ruff format` adopted repo-wide and gated: 13 of 22 .py files reformatted (whitespace,
  quotes, wrapping only — every tracked module parses to an identical AST before and after,
  17 of 17), `ruff format --check .` added to ci.yml. Closes the audit's open decision; the
  drift had reached 6 of 8 shipped modules with nothing enforcing either way. Cosmetic cost:
  ruff splits a few long lines with trailing comments into three-line shapes — move those
  comments above the line when next touching them.
- Test flake fixed: the 411-without-Content-Length pin read the response with one `recv`
  and the JSON body can trail the headers in a second segment; reads to EOF now (the server
  sends `Connection: close`). Passed 5/5 after; it had passed on the day it was written.

## 2026-09-02

- Surface polish on top of the readability pass (mechanical, no behavior change):
  `Handler.log_message` drops its two unused params (`format` also shadowed the builtin) for
  `*_`, so the silenced-access-log override no longer advertises a signature it ignores;
  `_PROMPT_ANSWERS` and rc_status's `GLYPH` become `MappingProxyType` with tuple keystrokes,
  matching `rc_state.RANK` — these are read-only tables and nothing should be able to write
  them at runtime. rc_guard's two vocabulary frozensets, its `parents` set, the `rc_status`
  subprocess call and the still-alive warning go back to the repo's hand-wrapped style from
  the one-item-per-line form a formatter had left. 127 green, ruff clean.

- Gate on the readability pass: the defect subagent found `_git()` had silently added
  `text=True` to snapshot()'s two returncode-only git calls, so an undecodable byte in git's
  stderr would raise `UnicodeDecodeError` out of an unguarded `launch()` — the one place
  the refactor widened an exception surface; those calls take `text=False` now, pinned by
  a kwargs-capturing test. The changed-line ratchet also flagged four pre-existing error
  paths the refactor moved (411 without Content-Length, `launch_reason`, the failed
  `/launch?json=1` reason, the HTML fallback); each got the test it asked for instead of a
  lower number. Panel: 2 of 3 models (xAI returned 403, credits exhausted).
- Readability pass (`/optimize`, quality-only, no behavior change): one `_json_error` for the
  eight JSON refusals (compact separators keep the wire bytes identical to the literals),
  one `_is_files` for the four `/files` predicates, `do_GET`'s six-branch ladder as a `match`,
  and a `_git()` helper beside `_tmux()` for the four `git -C` calls — why: the launcher's HTTP
  surface had eight near-identical error sends and four copies of a confinement predicate, each
  a place the next edit could drift. `_git_state` reads branch/dirty with `next`/`any` instead of
  a two-variable loop; `sweep_rcparts` walks one flattened generator. rc_guard's `live_sess` now
  calls `session_alive` instead of re-inlining `tmux has-session` (two definitions of "is it
  alive" could drift apart), stops shadowing its `parent` parameter, and prints the menu as one
  block. rc_claude's `auth_status` narrows its `try` to the two calls that can fail. Tests: one
  `respond()`/`desk()` in `_harness` replaces six inline pgrep/ps/lsof responders, plus
  `share_dir`/`spawn_ok`/`env` for the setUp boilerplate (127 green before and after, -45 lines).

- Whole-repo audit (`/audit`, four read-only finders + cross-family panel): net A- —
  architecture A-, code A, tests B+, process A-. Tests dropped from A- because 54 targeted
  mutants on shadow copies left 36 green, four of them on load-bearing launcher paths (the
  takeover guard, its SIGTERM grace loop, the `ensure_trusted` call site, `RC_PROJECT` in the
  session env) and one on the resumed-upload truncate that a reproduction showed reporting a
  stale tail as stored. Process found one inverted RUNBOOK claim (Linux takeover "no-op" while
  the code reads /proc) and one half-true invariant (RC_SPAWN "in the plist" that the installer
  overwrites). TODO.md carries the dated paydown (Now 2026-09-05, Next 2026-09-12) and two
  operator decisions (gate `ruff format`; where the refutation record lives in a repo that
  gitignores `.claude/`). Scorecard the same morning: A+ (97.2% over 8 files, 0 lint, 0 debt
  markers) — the countable half cannot see any of the above, which is the point of running both.

## 2026-08-26

- Desk-side launch guard (`rc_guard.py` + `rc_guard.sh`, opt-in like the prompt tag):
  a desk `claude` wrapped with `_rc_guard` no longer launches blind while a phone
  (remote-control) session owns the project — it offers attach (one conversation, desk
  and phone), takeover (close the remote session the way the launcher's stop() does,
  resume the thread at the desk), a separate fresh session, or quit. Hit live: desk
  `claude --continue` under a running rc-* session died with "No conversation found to
  continue"; the launcher guarded the phone->desk direction, nothing guarded desk->phone.
  Started as zsh-only, rebuilt same-day as stdlib Python behind one bash+zsh shim so the
  logic is unit-tested (19 tests incl. a real-pty keypress; in the coverage floor at 99%)
  and one implementation serves every host the launcher runs on. The Tier-H gate's
  panel + defect pass then landed six fixes before it shipped: a missing tmux failed
  CLOSED (traceback -> exit 1 -> shim read "quit" -> every desk launch blocked; now "no
  tmux" means "no rc session"); a trailing slash on `RC_PROJECTS_PARENT` or a symlinked
  parent silently disabled detection (both paths realpath'd now); `[a]` inside an
  existing tmux client used `attach`, which tmux refuses (`switch-client` under `$TMUX`);
  takeover fired kill-session on a fixed 2s timer and returned PROCEED regardless (now
  polls has-session up to 5s, kills only as fallback, refuses to launch if the session
  survives); `claude --version`/`--help`/`mcp …`/`--resume=id` popped the menu; and the
  pty test raced a 50ms timer against `setraw`'s TCSAFLUSH (polls for raw mode now — the
  timer version hung a loaded runner forever). A second panel pass on the fixed code
  added two more: a project dir that is itself a symlink never matched (getcwd() is
  physical; the shell's logical `$PWD` is now tried first when it is the same dir, and
  the parent in both forms — macOS's /var -> /private/var made the test fail before
  the fix did), and `RC_PROJECTS_PARENT=/` produced `//` and matched nothing. Six
  further panel/defect leads were dismissed with an oracle each (recorded in
  `.claude/refutations.md`, local — that dir is gitignored here). No Windows port by
  design: the guard checks local tmux sessions on the launcher host, and the launcher
  (tmux + launchd/systemd) cannot run on Windows — a Windows desk reaches the host over
  SSH, where the shim already applies.
- Every launcher tmux `-t` target is now the exact-match `=name` form: bare `-t`
  prefix-matches, so with `rc-alpha` absent and `rc-alpha-sub` live, alpha's stop()
  C-c'd the sibling and launch() reported "already" (verified against a live tmux;
  found while building the guard — the sibling-prefix class from the kill-scope audit
  pin, one layer down). Pinned by a mutant-verified test.

## 2026-08-17

- *(backfilled 2026-09-04)* `[tool.coverage.run] source` declared in pyproject (f364ea8): a
  bare `coverage run` measures the shipped modules, same as CI's explicit `--source`.
- *(backfilled 2026-09-04)* `uv.lock` gitignored (8e989a7): uv runs the review scripts here
  now and the lock is tool residue, not project state.
- Readability pass, no behavior change: one `_tmux()` wrapper now carries the twelve
  captured tmux control calls, so `capture_output=True` stopped outweighing the commands
  in `_spawn`/`stop`/`launch` (`new-session` stays raw — it is the one call whose stderr
  is meant to reach the launcher log); `path[len("/files"):]` became `removeprefix` at
  four sites, with `_files` binding it once instead of recomputing it; `running()` dropped
  its magic `line[3:]`; and `EVENT_STATE` moved up beside `RANK` in rc_state so the two
  vocabulary tables can be diffed by eye. Polish pass on top: `_upload`'s folder guard
  binds `os.path.dirname` once (walrus), and `RANK`/`EVENT_STATE` are `MappingProxyType`
  now — the shared state vocabulary can no longer be mutated across module boundaries,
  which is the drift rc_state exists to prevent, as a runtime property instead of a
  convention. 105/105 tests unchanged throughout.
- Evening-audit Now trio: pinned the kill-scope cwd boundary (a bare-startswith mutant
  survived while ~/projects holds live sibling-prefix pairs the mutant would cross-kill;
  panel-confirmed High) and the lookalike-binary comm check; pinned the ≥400 log's token
  redaction (reverting it kept the suite green — the last unpinned leg of the token
  remediation); rewrote RUNBOOK's two stale "read-only by design" share claims in the
  name-the-hazard form — the second stale invariant caught in that doc in two days, both
  by the half-true survival mechanism.
- Audit Next batch: one `_desk_claude_pids()` generator is now the single definition of
  "desk claude" for both the badge scan and the kill/takeover paths (two verbatim copies
  could drift scopes apart silently); `_settle_prompt` hoists the interactive-prompt
  policy out of `_spawn` as a data table; and seven knob-level test pins landed (git
  timeout requested, PATH tail exact, JS mid-transfer stall cap — the last unguarded
  spin — hook event trio, `_fill` ordering, TTL expiry on both caches, the real-pgrep
  unit-test leak stubbed + `_desk_cache` restored per test).
- Audit paydown (the 2026-08-17 audit's Now batch): pinned `has_desk_thread`'s two
  load-bearing properties (sdk-cli discrimination + the 256KiB read cap — both were
  mutation-deletable with a green suite) and the `/stop?desk=1` route wiring (the desk ✕
  could phantom-"stop" nothing); added `rc_templates` to the coverage floors; synced the
  three token-era doc claims (RUNBOOK's *don't-undo* list literally instructed re-creating
  the removed leak channel), the stale subcommand launch phrasing, and the ✕/desk-badge
  behavior into README/RUNBOOK; backfilled the two missing 2026-08-16 entries below.

## 2026-08-16

- *(backfilled)* Session-location icons on the launcher: 📱 for launcher-run sessions,
  🖥 for desk sessions — reads faster at a glance than the word tags they replaced.
- *(backfilled)* Graceful desk-session close: the ✕ on a desk-badged row SIGTERMs the
  desk claude via `/stop?desk=1` (transcript flushed, relay archived, thread resumable),
  with the scan cache invalidated so the badge clears on the next poll.

- The idle "Claude is waiting for your input" notification no longer paints a session
  amber "waiting" — under bypassPermissions it is nearly the only Notification that
  fires, so every finished session (often just showing a suggested next prompt) looked
  blocked. Idle ping -> idle; permission/question notifications still read waiting.

- Headless launches no longer hang invisibly at interactive prompts: a huge thread now
  makes claude ask "resume from summary or full?" before it registers with the relay, so
  a phone tap read "launched" while the session sat at a prompt nobody could see (hit
  live on a 9h/833k-token bigproj thread — three taps, zero appearances in the app).
  _spawn now auto-answers the resume-cost prompt with "Resume full session as-is"
  (owner's standing preference: never compact, always full resume) and fails loudly
  with the prompt text for any other confirm-style screen.

- Launcher shows live desk sessions: a blue "desk" dot/tag on projects where a plain
  interactive `claude` is running (auto-paired with the phone app but invisible to the
  launcher's tmux-based dots — the owner hit this twice in one afternoon). Detection is a
  TTL-cached pgrep/cwd scan; bridge-pointer.json was rejected after live testing (desk
  sessions don't reliably write one; stale ones point at dead pids).
- Backlog paydown across four areas (each independently reviewed before merge):
  - Browser upload client rewritten around a tested pure policy (`rc_upload.js`, spliced
    into the files page; 11 node-test cases mirroring the Android `UploadLogicTest`).
    Fixes four client bugs: a failed HEAD probe was treated as offset 0 (restarting the
    PUT truncates the server's kept partial — the exact policy the Kotlin client pins
    against), zero-byte files faked success, a failed upload's message was wiped by the
    unconditional page reload, and the sort tie-break inherited the primary direction.
    CI now runs the JS suite.
  - Launcher: breadcrumb links no longer double-percent-encode (dirs with spaces/# were
    404ing from the crumb), phone sessions get `~/.local/bin` on PATH via a per-session
    tmux `-e` (MCP servers/hooks spawned by name failed remotely; the plist route was
    non-deterministic — tmux sessions inherit the server's env), `has_desk_thread` reads
    only the first 256KB per transcript (one project's transcript is 525MB), `_fill`
    orders keys longest-first, `rows_html` drops a redundant stat syscall, and the
    sort-key test gained a mixed-case fixture.
  - Android share target: zero-byte files now report "empty file — skipped" instead of
    silently faking success (the server rejects total<=0 by design).
  - RUNBOOK: documented the X-Rc-* resumable-upload wire contract (implemented in three
    runtimes with no spec until now), corrected the resume-symmetry claims to the
    post-fix reality, and dropped stray personal traces.
- Skip the resume attempt when nothing is locally resumable: `launch()` now checks for a
  desk (`entrypoint: cli`/`claude-vscode`) transcript up front and goes straight to the
  fresh flag-form launch. The doomed `--continue` on phone-born projects could die *after*
  `_spawn`'s 3s aliveness window — logged as "launched", then the session evaporated with
  `remain-on-exit` already off (observed live on sandbox: "launched" at 13:46, no tmux session,
  app showing disconnected). Also removes the permanent 3s stall those projects paid per tap.
- Fresh same-dir launches use `claude --remote-control <name>` (the top-level flag) instead
  of the `remote-control` subcommand — the subcommand births relay-only threads that neither
  the desk nor the launcher's own resume can ever reopen, so every phone-created project
  silently lost desk continuity (and re-forked a fresh thread on each tap). Proven by A/B:
  sandbox (subcommand-born, 68/68 `sdk-cli` records, unresumable) vs a probe project
  (flag-born, phone conversation recalled at the desk via `claude --continue`).
  Affected projects self-heal on their next tap: the fallback now launches flag-form, so new
  turns land in a desk-resumable transcript (old relay-only history stays app-only). The
  resume fallback also logs the real death reason instead of a hardcoded "no history".
- Remove the auth token from every public and shared channel — a review panel found the
  `RC_TOKEN` GitHub secret was baked into CI debug APKs uploaded as artifacts on this
  public repo (downloadable by any logged-in GitHub user; 23 unexpired copies deleted,
  secret deleted, token rotated):
  - CI builds untokened APKs; the app asks once and keeps the credential in prefs
    (stable signing preserves it across updates). `UploadActivity` now reads the pasted
    credential too instead of the baked field it used to hard-require.
  - A value-independent CI guard fails the Android build if a token field, secret
    wiring, or an `RC_TOKEN` dex symbol ever reappears; artifacts expire after 7 days.
  - The token also left the LaunchAgent plist and systemd unit — the launcher reads the
    0600 token file directly, so service files are secret-free and rotation is
    write-file + kickstart.
  - Closed two local leak channels: the launcher's ≥400 access log now records the path
    only (the app's uploads carry `?token=` in the query), and `install.sh` no longer
    echoes the tokened URL into terminal scrollback.
  - `allowBackup="false"` so the tokened prefs URL stops leaving the device via Google
    Auto Backup; the paste dialog accepts a full URL for a non-default host.
  - `RC_HOST` for CI builds comes from a repo variable, keeping the tracked source on
    the generic placeholder.
- gitignore `coverage.json` (written by the coverage gate) — audit hygiene item.
- Start this changelog (standing per-project rule; first entry is the remediation above).
