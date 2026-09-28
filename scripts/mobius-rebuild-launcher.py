#!/usr/bin/env python3
"""Frozen root launcher for the self-hosted Möbius replacement worker.

Installed once as ``/usr/local/libexec/mobius-rebuild-host`` and never changed
by updates. It holds no replacement logic. It runs a copy of
``scripts/mobius-rebuild-host.py`` recorded in ``workers.json``:

- ``active``: the proven worker. It always runs ``reconcile`` and runs a
  replacement when there is no candidate.
- ``candidate``: a newer worker the active one took from a verified official
  image after a successful replacement. It runs the next replacement. If that
  replacement succeeds it becomes active; any other outcome drops it for good
  and the proven worker handles the retry. So worker changes ship in releases
  and activate without a host command, and a faulty one costs one attempt.

Only a change to this file needs a reinstall
(``deployment/self-hosted-helper.required``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

LAUNCHER_REVISION = 1
STATE_DIR = Path("/var/lib/mobius-rebuild")
WORKERS = STATE_DIR / "workers"
INDEX = STATE_DIR / "workers.json"
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / "replace.lock"
PYTHON = "/usr/bin/python3"
ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "MOBIUS_REBUILD_LAUNCHER": str(LAUNCHER_REVISION),
}
PROVEN = {"succeeded", "no_change"}
# Outcomes that say nothing about the worker that reported them.
NEUTRAL_CODES = {"withdrawn", "already_running"}


def _root_private(path: Path, *, directory: bool) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    kind = 0o040000 if directory else 0o100000
    return (
        info.st_mode & 0o170000 == kind and info.st_uid == 0
        and info.st_mode & 0o077 == 0
    )


def _worker(entry) -> dict | None:
    """One recorded worker, only if its file is private and unchanged."""
    try:
        path = WORKERS / str(entry["file"])
        digest = str(entry["sha256"])
    except (KeyError, TypeError):
        return None
    if path.parent != WORKERS or not _root_private(path, directory=False):
        return None
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return None
    except OSError:
        return None
    return {**entry, "path": path}


def load_index() -> dict | None:
    if not (_root_private(WORKERS, directory=True)
            and _root_private(INDEX, directory=False)):
        return None
    try:
        index = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(index, dict) or index.get("version") != 1:
        return None
    active = _worker(index.get("active"))
    if active is None:
        return None
    candidate = _worker(index["candidate"]) if index.get("candidate") else None
    return {**index, "active": active, "candidate": candidate}


def _status() -> bytes:
    try:
        return STATUS.read_bytes()
    except OSError:
        return b""


def execute(worker: dict, command: str) -> int:
    return subprocess.run(
        [PYTHON, "-I", "-S", str(worker["path"]), command],
        env=ENV, cwd="/", check=False,
    ).returncode


def settle_candidate(ran: dict, result: int, before: bytes, replaced: str) -> None:
    """Promote the candidate that just ran if it proved itself, else drop it.

    ``replaced`` is the active worker's digest when the candidate started. A
    newer worker installed meanwhile (the installer waits only for the
    worker's lock) is never superseded by this older decision."""
    after = _status()
    if after == before and result == 0:
        return  # nothing was queued: the candidate has not been tried
    try:
        reported = json.loads(after) if after != before else {}
    except ValueError:
        reported = {}
    if reported.get("code") in NEUTRAL_CODES:
        return
    proven = reported.get("state") in PROVEN
    entry = {key: value for key, value in ran.items() if key != "path"}
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = json.loads(INDEX.read_text(encoding="utf-8"))
        if (stored.get("candidate") or {}).get("sha256") == ran["sha256"]:
            stored["candidate"] = None
        if not proven:
            stored["rejected"] = sorted({*stored.get("rejected", []), ran["sha256"]})
        elif (stored.get("active") or {}).get("sha256") == replaced:
            stored["previous"] = stored.get("active")
            stored["active"] = entry
        _publish(stored)


def _publish(index: dict) -> None:
    fd, name = tempfile.mkstemp(dir=STATE_DIR, prefix=".workers.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(index, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, INDEX)
        directory = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    os.umask(0o077)
    if (os.geteuid() != 0 or len(argv) != 2
            or argv[1] not in {"run", "reconcile"}):
        print("invalid invocation", file=sys.stderr)
        return 2
    index = load_index()
    if index is None:
        print("no verified replacement worker is installed; rerun "
              "scripts/install-rebuild-helper.sh", file=sys.stderr)
        return 1
    if argv[1] == "reconcile":
        return execute(index["active"], "reconcile")
    ran = index["candidate"] or index["active"]
    before = _status()
    result = execute(ran, "run")
    if ran is index["candidate"]:
        settle_candidate(ran, result, before, index["active"]["sha256"])
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
