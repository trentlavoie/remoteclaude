# shellcheck shell=bash
# Sourced by install.sh (--hook) and uninstall.sh: the one careful way this repo edits
# ~/.claude/settings.json, which belongs to you and to Claude Code, not to remoteclaude.
#
# The JSON merge itself stays single-sourced in rc_state_hook.py's CLI (--install-hook /
# --remove-hook). This wrapper only makes the write safe:
#   - backup first: <settings.json>.rc-backup-<timestamp>, mode 0600, kept (only when the
#     file actually changes);
#   - never edited in place: the CLI edits a private copy (HOME pointed at a staging dir
#     beside the file), which is then renamed over the original -- atomic on one filesystem,
#     so a crash or a full disk leaves the old file, never half of a new one;
#   - a symlinked settings.json (dotfiles repo) is followed: its target is replaced, the
#     link is kept; the original's permissions are kept.
# Needs $PY (python3) and $REPO (this checkout) set by the caller.

edit_claude_settings() { # $1 = --install-hook | --remove-hook
	local link="$HOME/.claude/settings.json" target
	target="$(readlink -f -- "$link" 2>/dev/null || true)"
	[ -n "$target" ] || target="$link"
	mkdir -p -- "$(dirname -- "$target")"
	(
		stage="$(mktemp -d "$(dirname -- "$target")/.rc-settings.XXXXXX")"
		trap 'rm -rf -- "$stage"' EXIT
		mkdir "$stage/.claude"
		staged="$stage/.claude/settings.json"
		[ ! -e "$target" ] || cp -p -- "$target" "$staged"
		if ! out="$(HOME="$stage" "$PY" "$REPO/rc_state_hook.py" "$1" "$REPO" 2>&1)"; then
			echo "!! could not edit $target (left unchanged): $out" >&2
			exit 1
		fi
		if [ ! -e "$staged" ] || { [ -e "$target" ] && cmp -s -- "$target" "$staged"; }; then
			echo "   $target: no change needed"
			exit 0
		fi
		if [ -e "$target" ]; then
			bak="$target.rc-backup-$(date +%Y%m%d-%H%M%S)"
			cp -p -- "$target" "$bak"
			chmod 600 -- "$bak"
			echo "   backup: $bak"
		fi
		mv -f -- "$staged" "$target"
		echo "   $target: ${1#--} done"
	)
}
