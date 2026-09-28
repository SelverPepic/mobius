#!/usr/bin/env python3
"""Ask the self-hosted replacement helper for a container-only upgrade.

An installation whose running image predates the image-owned boot transaction
(``backend/runtime/boot-protocol``) cannot take a release that changes Python
packages: its served code refuses to install source for packages its image
lacks. This moves only the container to a newer official image and leaves the
served source untouched. The new image's boot transaction then checks that
unchanged source against itself (an image newer than the source's release is
allowed) and probes it, with the image's own release as the floor. After that,
Settings' ordinary update installs the release, because its packages are
already in the image.

Run it inside the Möbius container as the ``mobius`` user, from the fetched
upstream so an older installation needs nothing new installed::

    git -C /data/platform fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
    git -C /data/platform show origin/main:scripts/request-container-upgrade.py \
      | python3 - [--check] [--target <40-hex official release>]

It changes nothing but the helper's inbox, and only after these checks pass:
the target is a fetched official release that descends from the running image
and whose history already declares the installed source's Python packages;
no update is prepared or parked; and the helper is installed and idle. The helper then drains chats, replaces the
container, verifies it, and restores the previous container if it is not
healthy. See scripts/CONTAINER-REBUILD.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
PLATFORM = DATA / "platform"
CONTROL = DATA / "mobius-rebuild"
BUILD_INFO = Path(os.environ.get("MOBIUS_BUILD_INFO_PATH", "/app/build-info.json"))
ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}
PYTHON_INPUTS = ("backend/requirements.txt", "backend/requirements.lock")
SHA_RE = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    """A precondition does not hold; nothing was requested."""


def git(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(PLATFORM), *args],
        capture_output=True, text=True, check=False, env=env, timeout=120,
    )


def resolve(ref: str) -> str:
    out = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else ""


def descends(ancestor: str, descendant: str) -> bool:
    return git("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def running_image() -> str:
    try:
        sha = str(json.loads(BUILD_INFO.read_text(encoding="utf-8")).get("sha") or "")
    except (OSError, ValueError):
        sha = ""
    if not SHA_RE.fullmatch(sha):
        raise Refused("this container does not record its own release")
    return sha


def declared_in_history(commit: str, path: str, data: bytes) -> bool:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    blob = subprocess.run(
        ["git", "-C", str(PLATFORM), "hash-object", "--stdin"],
        input=data, capture_output=True, check=True, env=env, timeout=120,
    ).stdout.decode().strip()
    history = subprocess.run(
        ["git", "-C", str(PLATFORM), "log", "--format=", "--raw", "--no-abbrev",
         "--no-renames", commit, "--", path],
        capture_output=True, check=False, env=env, timeout=120,
    )
    return history.returncode == 0 and blob.encode() in history.stdout


def check_target(target: str) -> str:
    image = running_image()
    if target == image:
        raise Refused("this container already runs that release")
    if not descends(target, resolve("refs/remotes/origin/main") or target):
        raise Refused("the target is not part of the fetched official history")
    if not descends(image, target):
        raise Refused("the target is not newer than the running container")
    # The same judgment the new image's boot transaction makes before it
    # serves the unchanged source: every package input the installed source
    # took from its release must appear in the target's own history, so the
    # target's packages are not older than the source's.
    release = resolve("refs/heads/upstream")
    for path in PYTHON_INPUTS:
        try:
            served = (PLATFORM / path).read_bytes()
        except OSError:
            continue
        if declared_in_history(target, path, served):
            continue
        shown = git("show", f"{release}:{path}") if release else None
        if shown is not None and shown.returncode == 0 and shown.stdout.encode() == served:
            raise Refused(
                "the installed source declares newer Python packages than the "
                "target; fetch origin main and use the latest release"
            )
    return image


def check_no_pending_update() -> None:
    for name in (".platform-prepared-update.json", ".platform-conflict"):
        if (DATA / name).exists():
            raise Refused(
                "an update is prepared or waiting for its resolver; finish or "
                "cancel it in Settings first"
            )


def check_helper_idle() -> Path:
    inbox = CONTROL / "inbox"
    try:
        status = json.loads((CONTROL / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused(
            "the host replacement helper is not installed; run "
            "scripts/install-rebuild-helper.sh on the host"
        ) from None
    if not inbox.is_dir() or not os.access(inbox, os.W_OK | os.X_OK):
        raise Refused("the helper inbox is not writable from this container")
    if str(status.get("state") or "idle") in ACTIVE_STATES:
        raise Refused("a container replacement is already running")
    if (inbox / "request.json").exists():
        raise Refused("a container replacement request is already queued")
    return inbox


def write_request(inbox: Path, target: str) -> None:
    """Publish one request without overwriting a request queued meanwhile.

    Version 1 carries only the release: every installed helper accepts it, and
    no prepared update exists for an operation to confirm.
    """
    temp = inbox / f".request-{secrets.token_hex(12)}.tmp"
    payload = json.dumps({"version": 1, "expected_sha": target}, separators=(",", ":"))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, inbox / "request.json")
        except FileExistsError:
            raise Refused("a container replacement request is already queued") from None
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target", help="official release to move the container to (default: origin/main)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="run every check and report, but request nothing",
    )
    args = parser.parse_args(argv)
    try:
        target = resolve(args.target) if args.target else resolve("refs/remotes/origin/main")
        if not target or (args.target and not SHA_RE.fullmatch(args.target)):
            raise Refused("name a fetched, complete 40-character official release")
        image = check_target(target)
        check_no_pending_update()
        inbox = check_helper_idle()
        if args.check:
            print(
                f"Ready: a container-only upgrade from {image[:12]} to "
                f"{target[:12]} would be requested."
            )
            return 0
        write_request(inbox, target)
    except Refused as exc:
        print(f"Container upgrade not requested: {exc}.", file=sys.stderr)
        return 1
    print(
        f"Requested a container-only upgrade from {image[:12]} to {target[:12]}. "
        "Möbius restarts once the helper has pulled and checked the image; "
        "afterwards, install the update from Settings."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
