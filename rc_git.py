"""git state for the launcher page: branch + dirty per project, and the opt-in snapshot.

The phone shows these before you tap Launch — same-dir lands a remote turn on the very
tree you edit locally — so both calls are bounded (a timeout on status, a TTL cache in
front of it) rather than allowed to hold a page worker.
"""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import rc_config as cfg


# The launcher runs git in every repo under PARENT on each /status poll — outside any
# claude permission prompt. Repo-local config must not turn that into code execution:
# core.fsmonitor names a command `git status` runs (verified: a planted fsmonitor fires on
# a plain status), and hooks (reference-transaction on snapshot's update-ref) likewise.
# Empty fsmonitor = off on every git version (old ones read it as a hook path).
_NO_REPO_EXEC = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")
SNAPSHOT_TIMEOUT = 15.0


def _git(path: str, *args: str, text: bool = True, **kw) -> subprocess.CompletedProcess:
    """A git call against path's work tree, output captured — the git counterpart of
    tmux(). `-C` rather than cwd= so the argv carries its own root, and no OSError
    guard: each caller decides what a missing/failing git means (_git_state drops the
    badge, snapshot declines to checkpoint). --no-optional-locks: `git status` otherwise
    refreshes the index under index.lock, and now that /status polls every repo it would
    race a desk `git add`/`commit` with "Unable to create index.lock". text=False for
    callers that only read the
    return code: decoding is strict, and an undecodable byte in git's stderr would
    otherwise raise out of an unguarded launch()."""
    return subprocess.run(
        [cfg.GIT, "--no-optional-locks", *_NO_REPO_EXEC, "-C", path, *args],
        capture_output=True,
        text=text,
        **kw,
    )


def _git_state(proj: str) -> dict | None:
    """{'b': branch, 'd': dirty} for proj's working tree, or None when it isn't a git repo.
    One `git status --porcelain=v2 --branch` yields both: the '# branch.head' header names the
    branch; any non-'#' line (a tracked change or an untracked file) means the tree is dirty."""
    path = cfg.project_dir(proj)
    try:
        out = _git(
            path, "status", "--porcelain=v2", "--branch", timeout=cfg.GIT_STATUS_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        # git missing under the minimal launchd PATH, or a hung/slow repo (index.lock, slow
        # disk): drop the badge rather than block a page() worker forever. TimeoutExpired is
        # NOT an OSError, so it must be named explicitly.
        return None
    if out.returncode:  # not a work tree (or a git error) — treat as "no git info"
        return None
    lines = out.stdout.splitlines()
    branch = next(
        (
            ln.removeprefix("# branch.head ")
            for ln in lines
            if ln.startswith("# branch.head ")
        ),
        "",
    )
    return {"b": branch, "d": any(ln and not ln.startswith("#") for ln in lines)}


# Cached because the phone re-reads the badges on every page load / pull-to-refresh and
# each miss forks a git; .invalidate() drops every project's entry.
git_state = cfg.ttl_cached(lambda: cfg.GIT_TTL)(_git_state)


def git_states(projs: list[str] | None = None) -> dict[str, dict]:
    """{project: {'b': branch, 'd': dirty}} for every project that is a git repo, so the phone
    can show branch + dirty before you tap Launch — same-dir lands a remote turn on the very
    tree you edit locally, so knowing what's there first pairs with the RC_SNAPSHOT net.
    Fanned out across projects and cached (RC_GIT_TTL) so dozens of repos don't refork git on
    every page load / pull-to-refresh."""
    projs = cfg.projects() if projs is None else projs
    if not projs:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return {p: s for p, s in zip(projs, ex.map(git_state, projs), strict=True) if s}


def snapshot(proj: str) -> str | None:
    """Non-destructive git checkpoint before a remote turn can touch the tree.

    Opt-in via RC_SNAPSHOT — same-dir means a phone-driven turn lands on the same
    working tree you edit locally, so this parks the current tree+index as a stash
    commit under refs/rc-snapshots/ (kept off `git stash list`). Recover with
    `git stash apply <ref>`. Returns the ref, or None if off / clean / not a repo — or
    when git is missing or hangs: a checkpoint is best-effort, never a failed launch.
    """
    if not os.environ.get("RC_SNAPSHOT"):
        return None
    path = cfg.project_dir(proj)
    t = SNAPSHOT_TIMEOUT
    try:
        if _git(
            path, "rev-parse", "--is-inside-work-tree", text=False, timeout=t
        ).returncode:
            return None
        sha = _git(path, "stash", "create", timeout=t).stdout.strip()
        if not sha:
            return None
        ref = f"refs/rc-snapshots/{proj}/{int(time.time())}"
        msg = f"rc-snapshot {proj}"
        _git(path, "update-ref", "-m", msg, ref, sha, text=False, timeout=t)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    return ref
