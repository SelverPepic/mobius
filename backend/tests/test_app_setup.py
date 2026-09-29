"""Restoration is observable and stays outside readiness/settlement."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import app_setup


@pytest.fixture
def setup(monkeypatch, tmp_path):
  monkeypatch.setattr(app_setup, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))
  monkeypatch.setattr(app_setup.platform_update, "read_prepared_update", lambda: None)
  return app_setup.Runner()


@pytest.mark.asyncio
@pytest.mark.parametrize("codes,state", [([0], "ready"), ([1, 0, 0], "ready"), ([2], "conflict"), ([1, 3], "failed"), ([1, 0, 1], "failed")])
async def test_protocol_and_persistence(setup, codes, state):
  calls = []
  async def run(action):
    calls.append(action)
    return codes[len(calls)-1], "diagnostic"
  await setup.step("step", run)
  assert calls == ["check", "apply", "check"][:len(codes)]
  assert app_setup.status() == {"step": {"state": state, "output": "diagnostic"}}


@pytest.mark.asyncio
async def test_script_and_tail(setup, tmp_path):
  script = tmp_path / "restore.sh"
  script.write_text('#!/bin/sh\nif [ "$1" = check ]; then test -f done; else touch done; fi\n')
  async def run(action):
    return await app_setup.command(["/bin/sh", str(script), action], tmp_path)
  await setup.step("script", run)
  assert app_setup.status()["script"]["state"] == "ready"
  code, output = await app_setup.command(["/bin/sh", "-c", "printf 'Inst foo\n%10000s' x"], plan=True)
  assert code == -1 and len(output) == 8192
  code, output = await app_setup.command([sys.executable, "-m", "app.app_python_env", str(tmp_path), "7", str(tmp_path)])
  assert code == 0, output


@pytest.mark.asyncio
async def test_apt_combines_constraints_and_refuses_conflict(setup, monkeypatch):
  command = AsyncMock(side_effect=[(100, "missing lists"), (0, "updated"), (100, "unmet dependencies")])
  monkeypatch.setattr(app_setup, "command", command)
  requirements = ["foo (>= 2)", "foo (<< 2)"]
  await setup.step("apt", lambda action: app_setup.apt(requirements, action))
  assert command.await_count == 3
  assert command.call_args.args[0] == ["apt-get", "--simulate", "--no-remove", "satisfy", *requirements]
  assert app_setup.status()["apt"]["state"] == "conflict"
  command.reset_mock()
  command.side_effect = [(-1, "Inst foo"), (0, "updated"), (-1, "Inst foo"), (0, "installed"), (0, "")]
  await setup.step("apt", lambda action: app_setup.apt(requirements, action))
  assert command.call_args_list[3].args[0] == ["sudo", "-n", "apt-get", "--yes", "--no-remove", "satisfy", *requirements]
  assert app_setup.status()["apt"]["state"] == "ready"


@pytest.mark.asyncio
async def test_nonblocking_readiness_settlement_and_rerun(setup, monkeypatch):
  import httpx
  ready = False
  record = {"state": "swapped", "operation": {"id": "replacement"}}
  async def get(*args, **kwargs):
    return SimpleNamespace(status_code=200 if ready else 503)
  monkeypatch.setattr(httpx.AsyncClient, "get", get)
  monkeypatch.setattr(app_setup.platform_update, "read_prepared_update", lambda: record)
  real_sleep = asyncio.sleep
  async def tick(_):
    await real_sleep(0)
  monkeypatch.setattr(app_setup.asyncio, "sleep", tick)
  ran = asyncio.Event()
  async def reconcile():
    ran.set()
  setup.reconcile = AsyncMock(side_effect=reconcile)
  monkeypatch.setattr(app_setup, "_runner", setup)
  setup.record("interrupted", "pending", "previous output")
  setup.states = {}  # simulate a restarted worker loading its durable status
  task = asyncio.create_task(setup.serve())
  try:
    for _ in range(10):
      await real_sleep(0)
    setup.reconcile.assert_not_awaited()
    assert setup.states["interrupted"]["output"] == "previous output"
    ready = True
    for _ in range(10):
      await real_sleep(0)
    setup.reconcile.assert_not_awaited()
    record = None
    await asyncio.wait_for(ran.wait(), 2)
    assert setup.reconcile.await_count == 1
    ran.clear()
    app_setup.request_run()
    await asyncio.wait_for(ran.wait(), 2)
    assert setup.reconcile.await_count == 2
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_authenticated_status_and_rerun(client, auth, setup, monkeypatch):
  setup.record("old", "pending", "interrupted")
  assert client.get("/api/setup").status_code == 401
  assert client.get("/api/setup", headers=auth).json()["old"]["state"] == "pending"
  calls = []
  monkeypatch.setattr(app_setup, "request_run", lambda: calls.append(True))
  assert client.post("/api/setup/rerun", headers=auth).status_code == 202
  assert calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_reconcile_accepted_and_instance_steps_sequentially(setup, monkeypatch, tmp_path, db, malformed):
  from app import models
  accepted = tmp_path / "accepted"
  accepted.mkdir()
  instance = tmp_path / "customizations"
  instance.mkdir()
  for root, package in [(accepted, "foo (>= 2)"), (instance, "bar")]:
    (root / "mobius.json").write_text(json.dumps({
      "setup": {"steps": ["restore.sh"], "apt": [package]},
      "source_files": ["restore.sh"],
    }))
    (root / "restore.sh").write_text("#!/bin/sh\nexit 0\n")
  row = models.App(name="Test", slug="setup-test", source_dir="/never-read-dirty", runtime_revision="a" * 64)
  db.add(row)
  db.commit()
  monkeypatch.setattr(app_setup.applied_app_runtime, "runtime_root", lambda app: accepted)
  if malformed:
    (accepted / "mobius.json").write_text("[]")
  events = []
  async def apt(requirements, action):
    events.append(("apt", requirements, action))
    return 0, ""
  async def command(argv, cwd):
    events.append(("script", cwd, argv[-1]))
    return 0, ""
  monkeypatch.setattr(app_setup, "apt", apt)
  monkeypatch.setattr(app_setup, "command", command)
  await setup.reconcile()
  if malformed:
    assert events == [("script", instance, "check")]
    assert app_setup.status()["apt"]["state"] == "failed"
  else:
    assert events == [("apt", ["foo (>= 2)", "bar"], "check"),
                      ("script", accepted, "check"), ("script", instance, "check")]


@pytest.mark.asyncio
async def test_new_update_binding_prevents_apply(setup, monkeypatch):
  bound = None
  checked = asyncio.Event()
  applied = asyncio.Event()
  monkeypatch.setattr(app_setup.platform_update, "read_prepared_update", lambda: bound)
  async def run(action):
    nonlocal bound
    if action == "check":
      if not applied.is_set():
        bound = {"state": "swapped", "operation": {"id": "update"}}
        checked.set()
        return 1, ""
    else:
      applied.set()
    return 0, ""
  task = asyncio.create_task(setup.step("script", run))
  try:
    await checked.wait()
    await asyncio.sleep(0.02)
    assert not applied.is_set()
    bound = None
    await asyncio.wait_for(task, 2)
    assert applied.is_set()
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_reaps_running_step(tmp_path):
  pid_file = tmp_path / "pid"
  task = asyncio.create_task(app_setup.command(["/bin/sh", "-c", f"echo $$ > {pid_file}; sleep 600"]))
  try:
    while not pid_file.exists():
      await asyncio.sleep(0.01)
  finally:
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 12)
  with pytest.raises(ProcessLookupError):
    os.kill(int(pid_file.read_text()), 0)
