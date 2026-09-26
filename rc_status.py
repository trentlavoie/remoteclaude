#!/usr/bin/env python3
"""Local awareness reader: is a remote-control turn live on this working tree?

Default (prompt mode): print a compact tag and exit, silent when nothing remote is
active under the query dir — cheap enough for a zsh RPROMPT. Pass a dir to query it
instead of $PWD.
  --list: show every active remote session (state, project, age, cwd).

Stale entries (older than RC_STATE_TTL, default 1h) are ignored, so a session that
died without a clean SessionEnd eventually stops showing.
"""

import os
import sys
import time
from pathlib import Path
from types import MappingProxyType

from rc_state import RANK, STATE_DIR, valid_states

GLYPH = MappingProxyType({"working": "● rc:working", "waiting": "○ rc:waiting"})


def live() -> list[dict]:
    now = time.time()
    return [d | {"age": now - d["ts"]} for d in valid_states(STATE_DIR, now)]


def shares_tree(here: Path, cwd: str) -> bool:
    if not cwd:  # Path("") is ".", which would match wherever the prompt stands
        return False
    p = Path(cwd).resolve()
    return here == p or p in here.parents or here in p.parents


def main() -> None:
    sessions = live()
    if "--list" in sys.argv[1:]:
        for d in sorted(sessions, key=lambda d: -RANK[d["state"]]):
            name = d.get("project") or Path(d["cwd"]).name
            print(f"{d['state']:<8} {name:<22} {int(d['age'])}s ago  {d['cwd']}")
        if not sessions:
            print("no active remote sessions")
        return

    here = Path(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()).resolve()
    hits = [d for d in sessions if shares_tree(here, d.get("cwd", ""))]
    if hits and (best := max(hits, key=lambda d: RANK[d["state"]])["state"]) in GLYPH:
        print(GLYPH[best])


if __name__ == "__main__":
    main()
