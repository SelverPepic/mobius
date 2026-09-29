"""Sequential, post-readiness restoration of accepted dependency declarations."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import signal
import sys

from app import app_python_env, applied_app_runtime, models, platform_update
from app.config import get_settings
from app.database import SessionLocal
from app.manifest_contract import job_interpreter, validate_setup
from app.storage_io import atomic_write

log = logging.getLogger(__name__)
_runner = None


def request_run() -> None:
  if _runner is not None:
    _runner.wake.set()


def status() -> dict:
  path = Path(get_settings().data_dir) / "setup-status.json"
  return json.loads(path.read_text()) if path.exists() else {}


async def command(argv, cwd=None, *, plan=False) -> tuple[int, str]:
  """Keep only an output tail; reap the whole process group on shutdown."""
  process = await asyncio.create_subprocess_exec(
    *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.STDOUT, start_new_session=True,
    env={**os.environ, "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"},
  )
  tail = b""
  changes = False
  try:
    while chunk := await process.stdout.read(4096):
      tail = (tail + chunk)[-8192:]
      changes |= any(line.startswith((b"Inst ", b"Conf ")) for line in tail.splitlines())
    code = await process.wait()
    return (-1 if plan and changes and code == 0 else code), app_python_env.redact(tail.decode(errors="replace"))
  finally:
    try:
      os.killpg(process.pid, signal.SIGKILL if process.returncode is not None else signal.SIGTERM)
    except ProcessLookupError:
      pass
    try:
      await asyncio.wait_for(process.wait(), 10)
    except asyncio.TimeoutError:
      os.killpg(process.pid, signal.SIGKILL)
      await process.wait()


async def apt(requirements, action) -> tuple[int, str]:
  # One solver transaction for ALL managed requirements. Never remove packages
  # or opt into forced downgrades/held-package changes.
  args = ["apt-get", "--no-remove", "satisfy", *requirements]
  simulation = [*args[:1], "--simulate", *args[1:]]
  if action == "check":
    code, output = await command(simulation, plan=True)
    return (0 if code == 0 else 1), output
  # Fresh containers intentionally have no package lists. Refresh only when
  # not already satisfied, then prove the combined request is solvable before
  # installing anything. A repository/network failure is not a conflict.
  code, output = await command(["sudo", "-n", "apt-get", "update"])
  if code:
    return 3, output
  code, output = await command(simulation, plan=True)
  if code not in (0, -1):
    return 2, output
  return await command(["sudo", "-n", *args[:1], "--yes", *args[1:]])


class Runner:
  def __init__(self):
    self.wake = asyncio.Event()
    self.wake.set()
    self.states = {}

  def record(self, key, state, output=""):
    self.states[key] = {"state": state, "output": output[-8192:]}
    path = Path(get_settings().data_dir) / "setup-status.json"
    atomic_write(path, json.dumps(self.states), mode=0o600)

  async def step(self, key, run):
    self.record(key, "pending")
    try:
      await self.wait_settled()
      code, output = await run("check")
      if code == 1:
        await self.wait_settled()
        code, output = await run("apply")
        if code == 0:
          code, output = await run("check")
      self.record(key, "ready" if code == 0 else "conflict" if code == 2 else "failed", output)
    except Exception as exc:
      self.record(key, "failed", app_python_env.redact(str(exc)))

  async def reconcile(self):
    self.states = {}  # each pass replaces statuses of removed declarations
    data = Path(get_settings().data_dir)
    with SessionLocal() as db:
      apps = db.query(models.App).filter(models.App.deleted_at.is_(None)).order_by(models.App.id).all()
    with ExitStack() as pins:
      declarations = []
      complete = True
      for app in apps:
        if not app.runtime_revision:
          continue
        try:
          pins.enter_context(applied_app_runtime.hold_runtime(app.id))
          root = await asyncio.to_thread(applied_app_runtime.runtime_root, app)
          path = root / "mobius.json"
          manifest = json.loads(path.read_text()) if path.exists() else {}
          validate_setup(manifest)
          declarations.append((f"app:{app.id}", root, manifest, app))
        except Exception as exc:
          complete = False
          self.record(f"app:{app.id}", "failed", str(exc))
      root = data / "customizations"
      path = root / "mobius.json"
      if path.exists():
        try:
          manifest = json.loads(path.read_text())
          validate_setup(manifest)
          declarations.append(("instance", root, manifest, None))
        except Exception as exc:
          complete = False
          self.record("instance", "failed", str(exc))
      requirements = [r for _, _, m, _ in declarations for r in m.get("setup", {}).get("apt", [])]
      if not complete:
        self.record("apt", "failed", "Cannot solve all requirements: a declaration is unreadable")
      elif requirements:
        await self.step("apt", lambda action: apt(requirements, action))
      else:
        self.record("apt", "ready")
      for owner, root, manifest, app in declarations:
        for script in manifest.get("setup", {}).get("steps", []):
          async def run(action):
            path = root / script
            if not path.resolve().is_relative_to(root.resolve()):
              raise ValueError("Setup script escapes its source")
            # Like scheduled jobs, use the shipped shebang; Store files need
            # not preserve executable mode bits.
            interpreter = job_interpreter(path.read_bytes())
            return await command([*interpreter, str(path), action], root)
          await self.step(f"{owner}:{script}", run)
        if app is not None and manifest.get("python"):
          key = f"{owner}:python"
          self.record(key, "pending")
          try:
            await self.wait_settled()
            code, output = await command([
              sys.executable, "-m", "app.app_python_env", str(data), str(app.id), str(root),
            ])
            self.record(key, "ready" if code == 0 else "failed", output)
          except Exception as exc:
            self.record(key, "failed", app_python_env.redact(str(exc)))

  async def wait_settled(self):
    # A timed-out observer is NOT a settlement decision. Keep waiting for
    # actual settlement/cancellation, and recheck before each mutation.
    while True:
      record = await asyncio.to_thread(platform_update.read_prepared_update)
      if not record or not (record["operation"] or record["state"] == "swapped"):
        return
      await asyncio.sleep(1)

  async def serve(self):
    import httpx
    try:
      self.states = status()
    except (OSError, ValueError):
      log.warning("Could not load setup status; rebuilding it")
    # A real HTTP probe proves lifespan has yielded AND the server is serving.
    async with httpx.AsyncClient(trust_env=False) as client:
      while True:
        try:
          response = await client.get(f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/api/ready")
          if response.status_code == 200:
            break
        except httpx.HTTPError:
          pass
        await asyncio.sleep(1)
    while True:
      await self.wake.wait()
      await self.wait_settled()
      self.wake.clear()
      try:
        await self.reconcile()
      except Exception:
        log.exception("Dependency restoration failed")


def start() -> asyncio.Task:
  global _runner
  _runner = Runner()
  return asyncio.create_task(_runner.serve(), name="dependency-restoration")
