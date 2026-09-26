#!/usr/bin/env bash
# Setup for the Remote Control launcher. Runs on macOS (launchd) or Linux (systemd --user).
# Idempotent: safe to re-run. Never runs sudo and never edits ~/.claude/settings.json unless
# you ask for it (--hook); host-level steps that need root are printed, not run.
#
#   ./install.sh               install or update the services
#   ./install.sh --reload      restart only the launcher to pick up edited rc_*.py
#                              (Linux: the Claude sessions live in rc-tmux.service and survive)
#   ./install.sh --hook        opt in to the turn-state hook (working/waiting dots): registers
#                              it in ~/.claude/settings.json, backed up first and replaced
#                              atomically (deploy/claude-settings.sh); uninstall.sh removes it
#   ./install.sh --render DIR  Linux: write the unit files and env file it WOULD install into
#                              DIR and stop. Touches nothing else (review, CI, tests).
#
# Linux settings live in ~/.config/rc-launcher/rc-launcher.env (0600; every key is described
# in deploy/rc-launcher.env.example). Precedence per key: this invocation's environment
# (RC_SPAWN=worktree ./install.sh), then that file, then the defaults below.
set -euo pipefail
umask 077 # everything created here (token, env file, units, backups) is owner-only

REPO="$(cd "$(dirname "$0")" && pwd)"
OS="$(uname -s)"
MODE="${1:-install}"
CONF_DIR="$HOME/.config/rc-launcher"
TOKEN_FILE="$CONF_DIR/token"
ENV_FILE="$CONF_DIR/rc-launcher.env"
UNIT_DIR="$HOME/.config/systemd/user"
UNITS="rc-tmux.service rc-launcher.service rc-healthcheck.service rc-healthcheck.timer"

die() {
  echo "!! $*" >&2
  exit 1
}

case "$MODE" in
  install | --reload | --hook) ;;
  --render) [ -n "${2:-}" ] || die "usage: ./install.sh --render DIR" ;;
  *) die "unknown option: $MODE (expected --reload, --hook or --render DIR)" ;;
esac
[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root (these are per-user services)"

# --reload: pick up edited rc_*.py by restarting ONLY the launcher service — no unit rewrite.
# Confirm the new code is live via the unauthenticated /version (its stamp hashes rc_*.py).
PORT="${RC_LAUNCHER_PORT:-8787}"
if [ "$MODE" = "--reload" ]; then
  case "$OS" in
    Darwin) launchctl kickstart -k "gui/$(id -u)/com.matt.rc-launcher" || die "kickstart failed" ;;
    Linux) systemctl --user restart rc-launcher.service || die "restart failed" ;;
    *) die "unsupported OS for --reload: $OS" ;;
  esac
  echo "==> reloaded; curl -s localhost:${PORT}/version to confirm the new build stamp"
  exit 0
fi

# python3 for the services: RC_PYTHON, else the distro's (not whatever venv is active in
# this shell — the unit would silently pin it), else PATH. The code needs 3.12+.
PY="${RC_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ "$OS" = Linux ] && [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; else PY="$(command -v python3 || true)"; fi
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then die "python3 not found (set RC_PYTHON)"; fi
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "$PY is older than Python 3.12"

if [ "$MODE" = "--hook" ]; then
  # shellcheck source=deploy/claude-settings.sh
  . "$REPO/deploy/claude-settings.sh"
  echo "==> registering the turn-state hook in ~/.claude/settings.json"
  echo "    (it runs on every Claude Code event but exits at once unless RC_REMOTE is set,"
  echo "     i.e. only phone-launched sessions write their working/waiting state)"
  edit_claude_settings --install-hook
  exit 0
fi

# ---------------------------------------------------------------------------------------
# Linux: the env file's settings. Parsed, never sourced (it is data, not a script).
KEYS=(RC_PROJECTS_PARENT RC_PROJECT_GROUPS RC_PROJECT_ROOTS RC_MODEL RC_SPAWN RC_RESUME
  RC_TAKEOVER RC_SNAPSHOT RC_CLAUDE_BIN RC_LAUNCHER_PORT RC_ALLOWED_HOSTS
  RC_TAILSCALE_USERS RC_SHARE_ENABLED RC_SHARE_DIR RC_NOTIFY_URL)
PINNED="RC_LAUNCHER_BIND RC_TMUX_BIN" # set by rc-launcher.service itself; ignored if found here
EXTRA=()                             # other RC_* keys found in the file: carried over as-is

load_env_file() {
  local line key val
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | '#'*) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    [[ "$key" =~ ^RC_[A-Z0-9_]+$ ]] || continue
    case " $PINNED " in *" $key "*)
      echo "   note: $key in $1 is ignored (rc-launcher.service pins it)" >&2
      continue
      ;;
    esac
    val="${val%\"}"
    val="${val#\"}"
    case " ${KEYS[*]} " in *" $key "*) ;; *) EXTRA+=("$key") ;; esac
    [ -n "${!key+x}" ] || printf -v "$key" '%s' "$val" # the invocation's env wins
  done <"$1"
}

render_env() { # $1 = destination
  local k v safe='^[A-Za-z0-9._/:,@%+=?&~-]*$'
  {
    echo "# rc-launcher settings for the rc-launcher and rc-healthcheck systemd --user units."
    echo "# Written by install.sh; re-run it (RC_X=value ./install.sh) or edit this file and run"
    echo "#   systemctl --user restart rc-launcher"
    echo "# KEY=value per line: no quotes, no spaces, no inline comments. Every key is described"
    echo "# in deploy/rc-launcher.env.example. The token is NOT here (its own 0600 file), and"
    echo "# RC_LAUNCHER_BIND / RC_TMUX_BIN are pinned by rc-launcher.service."
    for k in "${KEYS[@]}" ${EXTRA[@]+"${EXTRA[@]}"}; do
      v="${!k-}"
      [[ "$v" =~ $safe ]] || die "$k has characters an env file can't carry safely: $v"
      if [ -n "$v" ]; then printf '%s=%s\n' "$k" "$v"; else printf '# %s=\n' "$k"; fi
    done
  } >"$1"
}

render_units() { # $1 = destination dir
  local u p
  # the paths land unquoted in unit files and in sed's replacement: keep them boring
  for p in "$REPO" "$PY"; do
    [[ "$p" =~ ^/[A-Za-z0-9._/+-]+$ ]] || die "path unsafe for a unit file (spaces/specials): $p"
  done
  for u in $UNITS; do
    sed -e "s|@REPO@|$REPO|g" -e "s|@PYTHON@|$PY|g" "$REPO/deploy/systemd/$u" >"$1/$u"
  done
}

# install_file SRC DEST [backup]: move SRC over DEST (an atomic rename) unless identical;
# with "backup", the previous DEST is kept as DEST.bak. Returns 1 when nothing changed.
install_file() {
  if [ -f "$2" ] && cmp -s "$1" "$2"; then
    rm -f "$1"
    echo "   unchanged: $2"
    return 1
  fi
  if [ "${3:-}" = backup ] && [ -f "$2" ]; then cp -p "$2" "$2.bak"; fi
  mv -f "$1" "$2"
  chmod 600 "$2"
  echo "   wrote:     $2"
}

if [ "$OS" = Linux ] || [ "$MODE" = "--render" ]; then
  load_env_file "$ENV_FILE"
  : "${RC_PROJECTS_PARENT:=$HOME/projects}"
  : "${RC_SPAWN:=same-dir}"   # same-dir | worktree | session
  : "${RC_RESUME:=continue}"  # continue | fork | off
  : "${RC_TAKEOVER:=1}"       # 1 = close the project's desktop session first
  : "${RC_CLAUDE_BIN:=$HOME/.local/bin/claude}"
  : "${RC_LAUNCHER_PORT:=8787}"
  : "${RC_SHARE_ENABLED:=0}"  # the /files share is opt-in on this fork
  : "${RC_SHARE_DIR:=$HOME/rc-share}"
  PORT="$RC_LAUNCHER_PORT"
fi

if [ "$MODE" = "--render" ]; then
  mkdir -p "$2"
  render_units "$2"
  render_env "$2/rc-launcher.env"
  echo "==> rendered $UNITS rc-launcher.env into $2 (nothing installed)"
  exit 0
fi

echo "==> rc-launcher install (repo: $REPO, os: $OS)"

# 1. tmux holds each session so it survives the request returning. Not installed from here:
#    that would need sudo, which this script never runs.
# (Linux ignores RC_TMUX_BIN: the units pin deploy/rc-tmux, and a shell that exports
#  RC_TMUX_BIN for the desk guard points it at that shim, not at tmux itself)
if [ "$OS" = Linux ]; then TMUX_BIN="$(command -v tmux || true)"; else TMUX_BIN="${RC_TMUX_BIN:-$(command -v tmux || true)}"; fi
if [ -z "$TMUX_BIN" ]; then
  case "$OS" in
    Darwin) brew install tmux && TMUX_BIN="$(command -v tmux)" ;;
    *) die "tmux not found. Install it (e.g. sudo apt-get install tmux), then re-run" ;;
  esac
fi
if [ "$OS" = Linux ]; then
  # rc-tmux.service runs `tmux -D` (3.2+) found through the units' PATH
  case "$(dirname "$TMUX_BIN")" in
    "$HOME/.local/bin" | /usr/local/bin | /usr/bin | /bin) ;;
    *) die "tmux at $TMUX_BIN is not in ~/.local/bin, /usr/local/bin, /usr/bin or /bin (the units' PATH)" ;;
  esac
  tv="$("$TMUX_BIN" -V)"
  tv="${tv#tmux }"
  printf '3.2\n%s\n' "${tv%%[!0-9.]*}" | sort -V -C || die "tmux $tv is too old (need 3.2+ for -D)"
fi

# 2. claude binary present (a leading ~ from the env file is expanded here; the code does too)
CLAUDE_BIN="${RC_CLAUDE_BIN:-$HOME/.local/bin/claude}"
[ -x "${CLAUDE_BIN/#\~/$HOME}" ] || die "claude not found at $CLAUDE_BIN (set RC_CLAUDE_BIN)"

# 3. token (generate once, reuse thereafter; never in the repo, never printed). Only the 0600
#    file holds it — the launcher reads it directly, so the service files stay secret-free and
#    rotation is delete-file + re-run (or write-file + --reload). Written via a temp + rename,
#    so it is never world-readable and never half-written.
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
if [ ! -s "$TOKEN_FILE" ]; then
  tmp="$(mktemp "$CONF_DIR/.token.XXXXXX")"
  "$PY" -c "import secrets;print(secrets.token_urlsafe(24))" >"$tmp"
  mv -f "$tmp" "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE" # heal a token file created looser by hand or by an older install

# 4. service + health watchdog, per OS
install_launchd() {
  local L="com.matt.rc-launcher" H="com.matt.rc-healthcheck"
  local PLIST="$HOME/Library/LaunchAgents/${L}.plist"
  local HC="$HOME/Library/LaunchAgents/${H}.plist"
  local PROJECTS_PARENT="${RC_PROJECTS_PARENT:-$HOME/projects}"
  local SHARE_DIR="${RC_SHARE_DIR:-$HOME/rc-share}"
  # dedicated file-share drop dir — served at /files and, if you enable SMB by hand,
  # mountable as a Windows drive. A dir of its own, never ~/projects.
  mkdir -p "$SHARE_DIR"
  chmod 700 "$SHARE_DIR"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${L}</string>
  <key>ProgramArguments</key><array>
    <string>${PY}</string><string>${REPO}/rc_launcher.py</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${HOME}</string>
    <key>PATH</key><string>$(dirname "$PY"):$(dirname "$TMUX_BIN"):/usr/bin:/bin</string>
    <key>RC_PROJECTS_PARENT</key><string>${PROJECTS_PARENT}</string>
    <key>RC_CLAUDE_BIN</key><string>${CLAUDE_BIN}</string>
    <key>RC_TMUX_BIN</key><string>${TMUX_BIN}</string>
    <key>RC_LAUNCHER_PORT</key><string>${PORT}</string>
    <key>RC_SHARE_DIR</key><string>${SHARE_DIR}</string>
    <key>RC_RESUME</key><string>${RC_RESUME:-continue}</string>
    <key>RC_TAKEOVER</key><string>${RC_TAKEOVER:-1}</string>
    <key>RC_SPAWN</key><string>${RC_SPAWN:-same-dir}</string>
    <key>RC_PROJECT_GROUPS</key><string>${RC_PROJECT_GROUPS:-}</string>
    <key>RC_PROJECT_ROOTS</key><string>${RC_PROJECT_ROOTS:-}</string>
    <key>RC_SNAPSHOT</key><string>${RC_SNAPSHOT:-}</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/tmp/rc-launcher.log</string>
  <key>StandardErrorPath</key><string>/tmp/rc-launcher.err</string>
</dict></plist>
EOF
  cat >"$HC" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${H}</string>
  <key>ProgramArguments</key><array>
    <string>${PY}</string><string>${REPO}/rc_healthcheck.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${HOME}</string>
    <key>RC_CLAUDE_BIN</key><string>${CLAUDE_BIN}</string>
    <key>RC_NOTIFY_URL</key><string>${RC_NOTIFY_URL:-}</string>
    <key>RC_LAUNCHER_PORT</key><string>${PORT}</string>
    <key>RC_SHARE_DIR</key><string>${SHARE_DIR}</string>
  </dict>
  <key>RunAtLoad</key><true/><key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>/tmp/rc-healthcheck.log</string>
  <key>StandardErrorPath</key><string>/tmp/rc-healthcheck.err</string>
</dict></plist>
EOF
  for x in "$L:$PLIST" "$H:$HC"; do
    launchctl bootout "gui/$(id -u)/${x%%:*}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "${x#*:}"
    launchctl enable "gui/$(id -u)/${x%%:*}"
  done
}

install_systemd() {
  systemctl --user show-environment >/dev/null 2>&1 ||
    die "no systemd --user manager reachable (log in over SSH as $(id -un), not via sudo/su)"
  # the share dir exists either way (0700, empty); the launcher serves it only when
  # RC_SHARE_ENABLED is on
  mkdir -p "${RC_SHARE_DIR/#\~/$HOME}"
  chmod 700 "${RC_SHARE_DIR/#\~/$HOME}"
  [ -d "${RC_PROJECTS_PARENT/#\~/$HOME}" ] || echo "   warning: RC_PROJECTS_PARENT=$RC_PROJECTS_PARENT does not exist yet"

  local u tmux_changed=0
  # staged beside the destinations (same filesystem), so each mv below is an atomic rename
  STAGE="$(mktemp -d "$CONF_DIR/.stage.XXXXXX")"
  trap 'rm -rf "$STAGE"' EXIT
  render_units "$STAGE"
  render_env "$STAGE/rc-launcher.env"
  install_file "$STAGE/rc-launcher.env" "$ENV_FILE" backup || true
  mkdir -p "$UNIT_DIR"
  for u in $UNITS; do
    if install_file "$STAGE/$u" "$UNIT_DIR/$u" && [ "$u" = rc-tmux.service ]; then tmux_changed=1; fi
  done

  systemctl --user daemon-reload
  systemctl --user enable rc-tmux.service rc-launcher.service rc-healthcheck.timer
  # start, never restart: restarting rc-tmux would end every live Claude session
  systemctl --user start rc-tmux.service
  if [ "$tmux_changed" = 1 ] && systemctl --user is-active --quiet rc-tmux.service; then
    echo "   rc-tmux.service changed; the running server keeps its old settings until you choose"
    echo "   to restart it (ends every session): systemctl --user restart rc-tmux"
  fi
  # safe at any time: the launcher holds no session
  systemctl --user restart rc-launcher.service
  systemctl --user start rc-healthcheck.timer

  # lingering keeps these services up with nobody logged in, and starts them at boot.
  # Only checked here: enabling it needs root.
  if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" != yes ]; then
    echo "   !! lingering is off: the services stop when you log out and don't start at boot."
    echo "      Fix (needs sudo, run it yourself):  sudo loginctl enable-linger $(id -un)"
  fi
  sleep 2
  if "$PY" -c 'import sys,urllib.request as u; u.urlopen(sys.argv[1], timeout=3)' \
    "http://127.0.0.1:${PORT}/version" 2>/dev/null; then
    echo "   launcher answering on 127.0.0.1:${PORT}"
  else
    echo "   !! launcher not answering yet: journalctl --user -u rc-launcher -n 50"
  fi
}

case "$OS" in
  Darwin) install_launchd ;;
  Linux) install_systemd ;;
  *) die "unsupported OS: $OS (expected Darwin or Linux)" ;;
esac

# 5. phone URL + host-specific manual steps. The token is never echoed: a printed token lands
#    in terminal scrollback and transcripts; the phone stores it once as a cookie.
echo
if [ "$OS" = Darwin ]; then
  SHARE_DIR="${RC_SHARE_DIR:-$HOME/rc-share}"
  IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<host-ip>')"
  echo "==> launcher loaded. Bookmark / Add-to-Home-Screen on your phone:"
  echo "    http://${IP}:${PORT}/?token=<token>     (token: cat $TOKEN_FILE)"
  echo
  echo "==> do these by hand (see RUNBOOK.md):"
  echo "    one-time:  $CLAUDE_BIN   then /login   (caches the OAuth token RC needs)"
  echo "    sudo pmset -a autorestart 1 sleep 0 disksleep 0   (survive power loss, stay awake)"
  echo "    System Settings -> Users & Groups: temporary auto-login (loads keychain at boot)"
  echo "    System Settings -> General -> Sharing -> Remote Login (optional SSH fallback)"
  echo "    Windows drive mount (optional): Sharing -> File Sharing, share ONLY ${SHARE_DIR}"
  echo "      over SMB (guest off; tick your user 'On' in Options), then on Windows by IP:"
  printf '      net use Z: \\\\%s\\%s\n' "$IP" "$(basename "$SHARE_DIR")"
  echo "    reach it: same LAN, or a VPN / tailnet subnet route to ${IP}"
  echo "    optional: RC_NOTIFY_URL=https://ntfy.sh/your-topic ./install.sh  (phone push on login lapse)"
  echo "    optional: ./install.sh --hook  (working/waiting dots: registers the turn-state hook)"
else
  echo "==> installed: rc-tmux (sessions) + rc-launcher (web, 127.0.0.1:${PORT} only) + watchdog timer"
  echo "    settings:  $ENV_FILE    token: $TOKEN_FILE (never printed)"
  echo "    logs:      journalctl --user -u rc-launcher -u rc-healthcheck"
  echo "    sessions:  $REPO/deploy/rc-tmux ls   (attach -t rc-<project> to watch one)"
  echo
  echo "==> next (docs/TAILSCALE.md has the details):"
  echo "    1. one-time:  $CLAUDE_BIN   then /login   (caches the OAuth token RC needs)"
  echo "    2. Tailscale on this host, from an SSH shell (sudo):  bash deploy/install-tailscale.sh"
  echo "       then in the admin console: MagicDNS + HTTPS Certificates on; ACL for port 443"
  echo "    3. publish it on the tailnet (no sudo):  deploy/tailscale-serve.sh"
  echo "       it prints https://<host>.<tailnet>.ts.net/ and the RC_ALLOWED_HOSTS /"
  echo "       RC_TAILSCALE_USERS lines for $ENV_FILE (then: ./install.sh --reload)"
  echo "    4. phone:  https://<host>.<tailnet>.ts.net/?token=<token>  -> Add to Home Screen"
  if [ -z "${RC_NOTIFY_URL:-}" ]; then
    echo "    alerts: RC_NOTIFY_URL is unset, so watchdog alerts only reach the journal"
    echo "      (journalctl --user -u rc-healthcheck -p warning). For a phone push:"
    echo "      RC_NOTIFY_URL=https://ntfy.sh/<long-random-topic> ./install.sh"
  fi
  echo "    optional: ./install.sh --hook  (working/waiting dots: registers the turn-state hook)"
fi
