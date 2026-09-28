#!/usr/bin/env python3
"""Frozen root launcher for the self-hosted Möbius replacement worker.

Installed once as ``/usr/local/libexec/mobius-rebuild-host`` and never changed
by updates. It holds no replacement logic: it runs the active worker (a copy of
``scripts/mobius-rebuild-host.py``) that the worker itself adopted from a
verified official image after a successful replacement. Worker changes
therefore ship in releases and activate without a host command. Only a change
to this file needs a reinstall (``deployment/self-hosted-helper.required``).

Recovery: a worker that fails before changing anything (its status untouched)
is set aside and the previous worker runs the same command; its bytes are never
adopted again. A worker that fails mid-operation is reconciled by the previous
worker when its own reconcile fails. Neither path lowers the adopted revision.
"""

from __future__ import annotations

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
PYTHON = "/usr/bin/python3"
ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "MOBIUS_REBUILD_LAUNCHER": str(LAUNCHER_REVISION),
}


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
    previous = _worker(index.get("previous")) if index.get("previous") else None
    return {**index, "active": active, "previous": previous}


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def set_aside(index: dict) -> None:
    """Make the previous worker active and never adopt the failed bytes again."""
    stored = json.loads(INDEX.read_text(encoding="utf-8"))
    failed = index["active"]["sha256"]
    stored["active"] = stored["previous"]
    stored["previous"] = None
    stored["rejected"] = sorted({*stored.get("rejected", []), failed})
    fd, name = tempfile.mkstemp(dir=STATE_DIR, prefix=".workers.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(stored, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, INDEX)
        _fsync_dir(STATE_DIR)
    finally:
        Path(name).unlink(missing_ok=True)


def execute(worker: dict, command: str) -> int:
    return subprocess.run(
        [PYTHON, "-I", "-S", str(worker["path"]), command],
        env=ENV, cwd="/", check=False,
    ).returncode


def _status() -> bytes:
    try:
        return STATUS.read_bytes()
    except OSError:
        return b""


def main(argv: list[str]) -> int:
    os.umask(0o077)
    if (os.geteuid() != 0 or len(argv) != 2
            or argv[1] not in {"run", "reconcile"}):
        print("invalid invocation", file=sys.stderr)
        return 2
    command = argv[1]
    index = load_index()
    if index is None:
        print("no verified replacement worker is installed; rerun "
              "scripts/install-rebuild-helper.sh", file=sys.stderr)
        return 1
    before = _status()
    result = execute(index["active"], command)
    previous = index["previous"]
    if result == 0 or previous is None:
        return result
    if command == "reconcile":
        return execute(previous, "reconcile")
    if _status() != before:
        # The worker reported its own outcome, or stopped mid-operation;
        # systemd's ExecStopPost reconcile settles the latter.
        return result
    # The worker failed before changing anything. If the previous worker
    # handles the same request, the new one is at fault: set it aside.
    fallback = execute(previous, "run")
    if fallback == 0 or _status() != before:
        set_aside(index)
    return fallback


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
