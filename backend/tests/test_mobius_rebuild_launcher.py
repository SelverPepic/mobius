"""The frozen host launcher and the worker's self-adoption from official images."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
WORKER_SCRIPT = ROOT / "scripts" / "mobius-rebuild-host.py"
REGISTRY = ROOT / "scripts" / "rebuild-worker-revisions.json"


def _load(name: str, path: Path):
  spec = importlib.util.spec_from_file_location(name, path)
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


launcher = _load("mobius_rebuild_launcher", ROOT / "scripts" / "mobius-rebuild-launcher.py")
host = _load("mobius_rebuild_host_for_launcher", WORKER_SCRIPT)
IMAGE_ID = "sha256:" + "b" * 64


def worker(revision: int | None, body: str = "print('worker')") -> bytes:
  line = f"WORKER_REVISION = {revision}\n" if revision is not None else ""
  return f"#!/usr/bin/env python3\n{line}{body}\n".encode()


@pytest.fixture
def state(tmp_path, monkeypatch):
  """Private worker state owned by the test user instead of root."""
  uid = os.geteuid()
  workers, index, status = tmp_path / "workers", tmp_path / "workers.json", tmp_path / "status.json"
  for module in (launcher, host):
    monkeypatch.setattr(module, "WORKERS", workers)
  monkeypatch.setattr(launcher, "INDEX", index)
  monkeypatch.setattr(launcher, "STATE_DIR", tmp_path)
  monkeypatch.setattr(launcher, "STATUS", status)
  monkeypatch.setattr(host, "WORKER_INDEX", index)

  def private(path, *, directory):
    try:
      info = path.lstat()
    except OSError:
      return False
    kind = 0o040000 if directory else 0o100000
    return (info.st_mode & 0o170000 == kind and info.st_uid == uid
            and info.st_mode & 0o077 == 0)

  monkeypatch.setattr(launcher, "_root_private", private)
  return tmp_path


# --- The shipped worker's revision is tied to its bytes ----------------------

def test_every_worker_change_bumps_its_revision():
  """The launcher adopts only strictly higher revisions, so changed worker
  bytes under an old revision would never reach installed hosts."""
  source = WORKER_SCRIPT.read_bytes()
  revision = host.worker_revision(source)
  registry = json.loads(REGISTRY.read_text())
  assert revision == host.WORKER_REVISION >= 1
  assert registry.get(str(revision)) == hashlib.sha256(source).hexdigest(), (
    "scripts/mobius-rebuild-host.py changed: increase WORKER_REVISION and "
    "record the new revision's sha256 in scripts/rebuild-worker-revisions.json"
  )
  assert sorted(map(int, registry)) == list(range(1, revision + 1))


def test_revision_is_read_as_text():
  assert host.worker_revision(worker(3)) == 3
  assert host.worker_revision(worker(None)) == 0
  assert host.worker_revision(b"WORKER_REVISION = 2\nWORKER_REVISION = 9\n") is None
  assert host.worker_revision(b"WORKER_REVISION = 2 + 7\n") is None
  assert host.worker_revision(b"WORKER_REVISION=4\n") is None


# --- Adoption ---------------------------------------------------------------

def test_adopts_only_strictly_newer_compiling_workers(state):
  assert host.adopt_worker(worker(1), "checkout") == "adopted: revision 1"
  assert host.adopt_worker(worker(1), "checkout") == "current: revision 1"
  assert host.adopt_worker(worker(3), IMAGE_ID) == "adopted: revision 3"
  # An older official image (possibly one a compromised app asked for)
  # never replaces the newest worker.
  assert host.adopt_worker(worker(2), IMAGE_ID).startswith("not adopted")
  assert host.adopt_worker(worker(None), IMAGE_ID).startswith("not adopted")
  assert host.adopt_worker(worker(3, "print(1)"), IMAGE_ID).startswith(
    "rejected: a different worker already claims revision 3",
  )
  assert host.adopt_worker(worker(4, "def broken(:"), IMAGE_ID).startswith(
    "rejected: worker does not compile",
  )
  index = launcher.load_index()
  assert index["active"]["revision"] == 3
  assert index["previous"]["revision"] == 1
  assert index["active"]["path"].read_bytes() == worker(3)
  assert oct(index["active"]["path"].stat().st_mode & 0o777) == "0o700"
  assert host.adopt_worker(worker(4), IMAGE_ID) == "adopted: revision 4"
  assert sorted(p.name.split("-")[0] for p in (state / "workers").glob("*.py")) == ["3", "4"]


def test_a_tampered_worker_or_index_is_never_run(state):
  host.adopt_worker(worker(2), "checkout")
  active = launcher.load_index()["active"]["path"]
  active.write_bytes(worker(2, "print('changed')"))
  active.chmod(0o700)
  assert launcher.load_index() is None
  active.write_bytes(worker(2))
  active.chmod(0o755)
  assert launcher.load_index() is None
  active.chmod(0o700)
  index = json.loads((state / "workers.json").read_text())
  index["active"]["file"] = "../elsewhere.py"
  (state / "workers.json").write_text(json.dumps(index))
  assert launcher.load_index() is None


# --- Launcher recovery ------------------------------------------------------

def _run_launcher(monkeypatch, behaviour: dict, command: str) -> tuple[int, list]:
  """``behaviour`` maps worker revision to (exit code, writes status)."""
  calls = []

  def execute(entry, cmd):
    calls.append((entry["revision"], cmd))
    code, writes = behaviour[entry["revision"]]
    if writes:
      launcher.STATUS.write_text(json.dumps({"by": entry["revision"], "cmd": cmd}))
    return code

  monkeypatch.setattr(launcher, "execute", execute)
  monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
  return launcher.main(["launcher", command]), calls


def test_launcher_runs_the_active_worker(state, monkeypatch):
  host.adopt_worker(worker(1), "checkout")
  host.adopt_worker(worker(2), IMAGE_ID)
  assert _run_launcher(monkeypatch, {2: (0, True)}, "run") == (0, [(2, "run")])


def test_a_worker_that_fails_before_acting_is_set_aside(state, monkeypatch):
  host.adopt_worker(worker(1), "checkout")
  host.adopt_worker(worker(2), IMAGE_ID)
  result, calls = _run_launcher(monkeypatch, {2: (1, False), 1: (0, True)}, "run")
  assert (result, calls) == (0, [(2, "run"), (1, "run")])
  index = launcher.load_index()
  assert index["active"]["revision"] == 1 and index["previous"] is None
  assert index["high_water"] == 2
  # Its exact bytes are never adopted again; a newer revision still is.
  assert host.adopt_worker(worker(2), IMAGE_ID).startswith("not adopted: revision 2 was set aside")
  assert host.adopt_worker(worker(3), IMAGE_ID) == "adopted: revision 3"


def test_an_environment_failure_does_not_blame_the_worker(state, monkeypatch):
  host.adopt_worker(worker(1), "checkout")
  host.adopt_worker(worker(2), IMAGE_ID)
  result, calls = _run_launcher(monkeypatch, {2: (1, False), 1: (1, False)}, "run")
  assert (result, calls) == (1, [(2, "run"), (1, "run")])
  assert launcher.load_index()["active"]["revision"] == 2


def test_a_reported_failure_is_the_worker_doing_its_job(state, monkeypatch):
  host.adopt_worker(worker(1), "checkout")
  host.adopt_worker(worker(2), IMAGE_ID)
  assert _run_launcher(monkeypatch, {2: (1, True)}, "run") == (1, [(2, "run")])
  assert launcher.load_index()["active"]["revision"] == 2


def test_a_failed_reconcile_falls_back_without_setting_aside(state, monkeypatch):
  host.adopt_worker(worker(1), "checkout")
  host.adopt_worker(worker(2), IMAGE_ID)
  result, calls = _run_launcher(monkeypatch, {2: (1, False), 1: (0, True)}, "reconcile")
  assert (result, calls) == (0, [(2, "reconcile"), (1, "reconcile")])
  assert launcher.load_index()["active"]["revision"] == 2


def test_launcher_refuses_without_a_verified_worker(state, monkeypatch):
  monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
  assert launcher.main(["launcher", "run"]) == 1
  assert launcher.main(["launcher", "anything"]) == 2


def test_launcher_runs_workers_isolated(state, monkeypatch):
  host.adopt_worker(worker(1), "checkout")
  seen = {}

  def fake_run(args, **kwargs):
    seen.update(args=args, **kwargs)
    return subprocess.CompletedProcess(args, 0)

  monkeypatch.setattr(launcher.subprocess, "run", fake_run)
  monkeypatch.setenv("DOCKER_HOST", "tcp://attacker:2375")
  monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
  assert launcher.execute(launcher.load_index()["active"], "run") == 0
  assert seen["args"][:3] == ["/usr/bin/python3", "-I", "-S"]
  assert seen["cwd"] == "/"
  assert set(seen["env"]) == {"PATH", "HOME", "LANG", "MOBIUS_REBUILD_LAUNCHER"}


# --- Extraction from the exact verified image -------------------------------

def _archive(name: str, data: bytes, *, kind=tarfile.REGTYPE, extra=False) -> bytes:
  buffer = io.BytesIO()
  with tarfile.open(fileobj=buffer, mode="w") as tar:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
      info.linkname = "/etc/shadow"
    tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    if extra:
      other = tarfile.TarInfo("other.py")
      other.size = 1
      tar.addfile(other, io.BytesIO(b"x"))
  return buffer.getvalue()


def _fake_docker(monkeypatch, archive: bytes) -> list:
  calls = []

  def run(args, **kwargs):
    calls.append(args)
    return subprocess.CompletedProcess(args, 0, "c" * 64 if args[1] == "create" else "", "")

  monkeypatch.setattr(host.subprocess, "run", run)
  monkeypatch.setattr(host, "_bounded_output", lambda args, limit: (
    calls.append(args) or (archive if len(archive) <= limit else
                           (_ for _ in ()).throw(RuntimeError("too large")))
  ))
  return calls


def test_extracts_the_worker_from_the_exact_image_without_starting_it(monkeypatch):
  calls = _fake_docker(monkeypatch, _archive("mobius-rebuild-host.py", worker(5)))
  assert host.worker_from_image(IMAGE_ID) == worker(5)
  create = next(call for call in calls if call[1] == "create")
  assert create[-1] == IMAGE_ID and "--network" in create
  assert ["docker", "rm", "-f", "-v", "c" * 64] in calls
  assert not any(call[1] in {"run", "start", "exec"} for call in calls)


@pytest.mark.parametrize("archive", [
  _archive("mobius-rebuild-host.py", b"", kind=tarfile.SYMTYPE),
  _archive("mobius-rebuild-host.py", b"", kind=tarfile.LNKTYPE),
  _archive("mobius-rebuild-host.py", worker(5), extra=True),
  _archive("../mobius-rebuild-host.py", worker(5)),
  _archive("mobius-rebuild-host.py", b"x" * (host.MAX_WORKER_BYTES + 1)),
  _archive("mobius-rebuild-host.py", worker(5))[:700],
  b"not a tar",
])
def test_refuses_anything_but_one_regular_worker_file(monkeypatch, archive):
  _fake_docker(monkeypatch, archive)
  with pytest.raises(RuntimeError):
    host.worker_from_image(IMAGE_ID)


def test_adoption_never_fails_a_finished_replacement(state, monkeypatch):
  def broken(_image_id):
    raise RuntimeError("docker cp failed")

  monkeypatch.setattr(host, "worker_from_image", broken)
  assert host.adopt_from_image(IMAGE_ID) == "not adopted: docker cp failed"


# --- Request reads ----------------------------------------------------------

def test_request_reads_are_bounded_and_never_follow_links(tmp_path):
  request = tmp_path / "request.json"
  target = tmp_path / "secret.json"
  target.write_text(json.dumps({"version": 1, "expected_sha": "a" * 40}))
  request.symlink_to(target)
  payload, identity = host.read_request(request)
  assert payload is None and identity is not None and identity[2] == b""
  request.unlink()
  os.mkfifo(request)
  assert host.read_request(request)[0] is None  # never blocks on a pipe
  request.unlink()
  request.write_bytes(b"{" + b" " * host.MAX_REQUEST_BYTES + b"}")
  assert host.read_request(request)[0] is None
  request.write_text(json.dumps({"version": 1, "expected_sha": "a" * 40}))
  assert host.read_request(request)[0] == {"version": 1, "expected_sha": "a" * 40}


def test_a_linked_request_is_claimed_and_refused_not_followed(tmp_path):
  request = tmp_path / "request.json"
  claimed = tmp_path / ".request-x.json"
  (tmp_path / "secret").write_text("{}")
  request.symlink_to(tmp_path / "secret")
  _payload, identity = host.read_request(request)
  assert host.claim_request(request, claimed, identity) is True
  assert claimed.is_symlink() and not request.exists()
