#!/usr/bin/env bash
# Unload the launcher + watchdog, end the phone-launched sessions, and remove the state hook
# from ~/.claude/settings.json if it was registered (backed up first, replaced atomically).
# Keeps the token and the env file. Works on macOS or Linux. Never runs sudo.
set -euo pipefail
umask 077

REPO="$(cd "$(dirname "$0")" && pwd)"
OS="$(uname -s)"
PY="$(command -v python3 || true)"

case "$OS" in
  Darwin)
    for l in com.matt.rc-launcher com.matt.rc-healthcheck; do
      launchctl bootout "gui/$(id -u)/${l}" 2>/dev/null || true
      rm -f "$HOME/Library/LaunchAgents/${l}.plist"
    done
    # stop any live RC sessions (on macOS they share your default tmux server)
    TMUX_BIN="${RC_TMUX_BIN:-$(command -v tmux || true)}"
    if [ -n "$TMUX_BIN" ] && [ -x "$TMUX_BIN" ]; then
      # `|| true`: no server / no rc-* session must not abort the rest (set -e + pipefail)
      { "$TMUX_BIN" list-sessions -F '#{session_name}' 2>/dev/null || true; } |
        { grep '^rc-' || true; } |
        while read -r s; do "$TMUX_BIN" kill-session -t "=$s"; done
    fi
    ;;
  Linux)
    U="$HOME/.config/systemd/user"
    # launcher first, so nothing starts a session while the server goes down
    systemctl --user disable --now rc-healthcheck.timer rc-launcher.service 2>/dev/null || true
    # Stopping rc-tmux ends every phone-launched session: systemd SIGTERMs each claude, which
    # flushes its transcript and deregisters from the relay (the thread stays resumable).
    # Your own tmux server (sessions you started yourself) is never touched.
    systemctl --user disable --now rc-tmux.service 2>/dev/null || true
    rm -f "$U/rc-launcher.service" "$U/rc-tmux.service" \
      "$U/rc-healthcheck.service" "$U/rc-healthcheck.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user reset-failed rc-launcher.service rc-tmux.service \
      rc-healthcheck.service rc-healthcheck.timer 2>/dev/null || true
    ;;
esac

# remove the turn-state hook, only if it is there: settings.json is not rewritten otherwise
SETTINGS="$(readlink -f -- "$HOME/.claude/settings.json" 2>/dev/null || echo "$HOME/.claude/settings.json")"
if [ -n "$PY" ] && [ -f "$SETTINGS" ] && grep -qF "$REPO/rc_state_hook.py" "$SETTINGS"; then
  # shellcheck source=deploy/claude-settings.sh
  . "$REPO/deploy/claude-settings.sh"
  echo "==> removing the turn-state hook from ~/.claude/settings.json"
  edit_claude_settings --remove-hook || echo "!! hook left in place; remove it by hand"
fi

echo "rc-launcher unloaded; rc-* sessions stopped."
echo "(kept: ~/.config/rc-launcher/token and rc-launcher.env; delete them to rotate / reset)"
if command -v tailscale >/dev/null 2>&1; then
  echo "(the tailnet URL now answers 502; to remove it too: $REPO/deploy/tailscale-serve.sh --off)"
fi
