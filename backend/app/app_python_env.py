"""Per-app Python environments built from a declared, hash-pinned lock.

Without a declaration, app services, preload hosts, and Python jobs run on the
platform's interpreter and borrow its libraries. An app may instead declare
``"python": {"lock": "<path>"}`` in mobius.json: a complete
``pip-compile --generate-hashes`` lock inside its source. The platform then
builds one virtual environment WITHOUT system site-packages under
``<data>/app-envs/<app id>/<key>`` and runs that app's Python processes with
its interpreter. Only /data survives container replacement, so this is the one
place an app's dependencies can live.

Lifecycle:

- Build (``prepare_env`` / ``publish_env``): during Apply, after the accepted
  runtime tree is prepared and before its pointer is published. A fresh venv
  installs the lock wheels-only (no package build code runs), passes
  ``pip check`` and a smoke run of the app's Python entry, then is renamed
  into place. A matching env is reused. A failure fails the Apply, so the
  previous revision stays live.
- Use (``interpreter``): the key combines the interpreter/platform
  compatibility with the lock digest. An image replacement that changes the
  interpreter changes the key, so the old env is never used and the platform
  interpreter is never substituted: the process fails with
  ``PythonEnvUnavailable``, whose message tells the owner to Apply the app
  again, which rebuilds it (with network).
- GC (``prune_envs``): ``applied_app_runtime.prune_runtime`` calls it under the
  runtime reader/job locks with the runtime trees it keeps, so an env lives
  exactly as long as a kept (current, previous, or pinned) runtime needs it.

This module is imported by the job runner, so it takes ``data_dir``
explicitly and imports nothing from the web application.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.manifest_contract import ManifestContractError, job_interpreter


ENVS_DIRNAME = "app-envs"
_STAGING_DIRNAME = ".staging"
VENV_TIMEOUT_SECONDS = 120
INSTALL_TIMEOUT_SECONDS = 900
CHECK_TIMEOUT_SECONDS = 120
SMOKE_TIMEOUT_SECONDS = 60
_DIAGNOSTIC_CHARS = 2000
# Environment passed to pip: enough to locate an index or proxy, never the
# backend's own credentials.
_PIP_ENV_NAMES = frozenset({
  "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR",
  "SSL_CERT_FILE", "SSL_CERT_DIR",
  "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
_PYTHON_PROGRAM = re.compile(r"python(?:[0-9]+(?:\.[0-9]+)?)?")

# Run with the new env's interpreter. A service entry runs its module-level
# setup (everything but its ``__main__`` block, as the preload host does), so
# the imports and framework construction a request needs are exercised. A job
# is a whole program, so only its top-level imports are loaded.
_SMOKE_PROGRAM = r"""
import ast, importlib, os, runpy, sys
kind, path = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
if kind == "service":
  runpy.run_path(path, run_name="__mobius_env_check__")
else:
  with open(path, "rb") as handle:
    tree = ast.parse(handle.read(), path)
  for node in tree.body:
    if isinstance(node, ast.Import):
      names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
      names = [node.module]
    else:
      continue
    for name in names:
      importlib.import_module(name)
"""


class PythonEnvUnavailable(RuntimeError):
  """A declaring app's environment is missing for the current interpreter."""


class PythonEnvBuildError(RuntimeError):
  """The declared lock could not be built into a validated environment."""


@dataclass(frozen=True)
class StagedEnv:
  """A validated env awaiting ``publish_env``; ``reused`` ones already live there."""

  key: str
  root: Path
  reused: bool


def envs_parent(data_dir: Path | str, app_id: int) -> Path:
  return Path(data_dir) / ENVS_DIRNAME / str(int(app_id))


def _runtime_manifest(runtime_root: Path) -> dict:
  path = runtime_root / "mobius.json"
  if path.is_symlink() or not path.is_file():
    return {}
  try:
    manifest = json.loads(path.read_bytes())
  except (OSError, ValueError):
    return {}
  return manifest if isinstance(manifest, dict) else {}


def declared_lock(runtime_root: Path) -> str | None:
  """The lock path an accepted runtime tree declares, or None."""
  python = _runtime_manifest(runtime_root).get("python")
  lock = python.get("lock") if isinstance(python, dict) else None
  return lock if isinstance(lock, str) and lock else None


def _lock_file(runtime_root: Path, relative: str) -> Path:
  lock = runtime_root / relative
  try:
    inside = lock.resolve().is_relative_to(runtime_root.resolve())
  except (OSError, RuntimeError):
    inside = False
  if not inside or lock.is_symlink() or not lock.is_file():
    raise PythonEnvBuildError(
      f"The declared Python lock `{relative}` is missing from the app source."
    )
  return lock


@functools.cache
def compatibility_tag() -> str:
  """What a built env's binaries depend on in the running interpreter.

  The readable prefix names the ABI and platform; the digest also covers the
  base executable's location (a venv links to it) and the C library version,
  so moving or upgrading either in a new image selects a new env.
  """
  base = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)
  libc = "-".join(platform.libc_ver())
  identity = "\0".join((
    sys.implementation.cache_tag or sys.implementation.name,
    sysconfig.get_platform(),
    sysconfig.get_config_var("SOABI") or "",
    base,
    libc,
  ))
  readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", (
    f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
  ))
  return f"{readable}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"


def env_key(lock_bytes: bytes) -> str:
  return f"{compatibility_tag()}-{hashlib.sha256(lock_bytes).hexdigest()}"


@functools.lru_cache(maxsize=256)
def _declared_key(runtime_root: Path) -> str | None:
  # Accepted runtime trees are immutable and content-addressed by path, so a
  # tree's key never changes while this process runs.
  relative = declared_lock(runtime_root)
  if relative is None:
    return None
  try:
    return env_key(_lock_file(runtime_root, relative).read_bytes())
  except (OSError, PythonEnvBuildError):
    return ""


def _usable(env: Path) -> bool:
  # A venv's interpreter is a link to the base executable; a new image that
  # moved it leaves a dangling link, which exists() reports as missing.
  return (env / "pyvenv.cfg").is_file() and (env / "bin" / "python").exists()


def interpreter(data_dir: Path | str, app_id: int, runtime_root: Path) -> str:
  """The Python executable an app's accepted runtime must run with.

  Undeclared apps keep the platform interpreter. A declaring app gets its
  env's interpreter or ``PythonEnvUnavailable``, never a fallback.
  """
  key = _declared_key(runtime_root)
  if key is None:
    return sys.executable
  env = envs_parent(data_dir, app_id) / key if key else None
  if env is None or not _usable(env):
    raise PythonEnvUnavailable(
      "This app's Python environment is not built for the current platform "
      "interpreter. Apply the app again to rebuild it (this needs network "
      "access to download its locked packages)."
    )
  return str(env / "bin" / "python")


def _python_arguments(declared: tuple[str, ...]) -> list[str] | None:
  """Interpreter arguments of a Python shebang, or None for any other program.

  Recognized forms are ``/path/to/pythonX[.Y] [args]`` and
  ``/usr/bin/env [-S] pythonX[.Y] [args]``.
  """
  if Path(declared[0]).name == "env":
    rest = list(declared[1:])
    if rest[:1] == ["-S"]:
      rest = rest[1:]
    if rest and _PYTHON_PROGRAM.fullmatch(rest[0]) is not None:
      return rest[1:]
    return None
  if _PYTHON_PROGRAM.fullmatch(Path(declared[0]).name) is not None:
    return list(declared[1:])
  return None


def job_interpreter_in_env(
  declared: tuple[str, ...], data_dir: Path | str, app_id: int, runtime_root: Path,
) -> tuple[str, ...]:
  """Point a Python job's shebang at the app's env; other jobs are unchanged."""
  arguments = _python_arguments(declared)
  if arguments is None:
    return declared
  python = interpreter(data_dir, app_id, runtime_root)
  if python == sys.executable:
    return declared
  return (python, *arguments)


def _tail(*outputs: str) -> str:
  lines = [line for output in outputs for line in output.splitlines() if line.strip()]
  errors = [line for line in lines if line.lstrip().startswith("ERROR")]
  text = "\n".join(errors or lines[-12:])
  return text[-_DIAGNOSTIC_CHARS:] or "no diagnostics"


def _run(command: list[str], *, step: str, timeout: int, **kwargs) -> None:
  try:
    completed = subprocess.run(
      command, capture_output=True, text=True, timeout=timeout,
      stdin=subprocess.DEVNULL, **kwargs,
    )
  except subprocess.TimeoutExpired as exc:
    raise PythonEnvBuildError(f"{step} exceeded {timeout} seconds.") from exc
  except OSError as exc:
    raise PythonEnvBuildError(f"{step} could not start: {exc}") from exc
  if completed.returncode != 0:
    raise PythonEnvBuildError(
      f"{step} failed:\n{_tail(completed.stdout, completed.stderr)}"
    )


def _smoke_target(runtime_root: Path) -> tuple[str, Path] | None:
  manifest = _runtime_manifest(runtime_root)
  service = manifest.get("service")
  entry = service.get("entry") if isinstance(service, dict) else None
  if isinstance(entry, str) and (runtime_root / entry).is_file():
    return "service", runtime_root / entry
  schedule = manifest.get("schedule")
  job = schedule.get("job") if isinstance(schedule, dict) else None
  if isinstance(job, str) and (runtime_root / job).is_file():
    try:
      declared = job_interpreter((runtime_root / job).read_bytes())
    except ManifestContractError:
      return None
    if _python_arguments(declared) is not None:
      return "job", runtime_root / job
  return None


def _build(staged: Path, runtime_root: Path, lock: Path, relative: str) -> None:
  _run(
    [sys.executable, "-m", "venv", str(staged)],
    step="Creating the Python environment", timeout=VENV_TIMEOUT_SECONDS,
  )
  python = str(staged / "bin" / "python")
  pip_env = {
    name: value for name, value in os.environ.items()
    if name in _PIP_ENV_NAMES or name.startswith("PIP_")
  }
  pip_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
  _run(
    [
      python, "-m", "pip", "install", "--no-input",
      "--require-hashes", "--no-deps", "--only-binary=:all:",
      "-r", str(lock),
    ],
    step=f"Installing `{relative}` (wheels only, hash-checked)",
    timeout=INSTALL_TIMEOUT_SECONDS, env=pip_env, cwd=str(runtime_root),
  )
  _run(
    [python, "-m", "pip", "check"],
    step=f"`pip check` of `{relative}` (the lock must be complete)",
    timeout=CHECK_TIMEOUT_SECONDS, env=pip_env,
  )
  target = _smoke_target(runtime_root)
  if target is None:
    return
  kind, path = target
  with tempfile.TemporaryDirectory(prefix="mobius-env-check-") as storage:
    # The app's usual variables are present with inert values: setup must not
    # need a live token, and must not touch the app's real storage here.
    smoke_env = {
      name: value for name, value in os.environ.items()
      if name in {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}
    }
    smoke_env.update({
      "APP_ID": "0", "APP_SLUG": "", "APP_TOKEN": "",
      "APP_STORAGE_DIR": storage, "API_BASE_URL": "http://127.0.0.1:9",
    })
    _run(
      [python, "-c", _SMOKE_PROGRAM, kind, str(path)],
      step=f"Starting the app's {kind} `{path.name}` with `{relative}`",
      timeout=SMOKE_TIMEOUT_SECONDS, env=smoke_env, cwd=str(runtime_root),
    )


def prepare_env(
  data_dir: Path | str, app_id: int | None, runtime_root: Path,
) -> StagedEnv | None:
  """Build and validate the env an accepted runtime tree declares.

  Returns None for an undeclared tree. ``app_id`` is None for an app that has
  no row yet; it cannot have an env to reuse. Raises PythonEnvBuildError with
  pip's own diagnostics, which name the failing package.
  """
  relative = declared_lock(runtime_root)
  if relative is None:
    return None
  lock = _lock_file(runtime_root, relative)
  key = env_key(lock.read_bytes())
  if app_id is not None:
    existing = envs_parent(data_dir, app_id) / key
    if _usable(existing):
      return StagedEnv(key=key, root=existing, reused=True)
  staging = Path(data_dir) / ENVS_DIRNAME / _STAGING_DIRNAME
  staging.mkdir(parents=True, exist_ok=True)
  staged = Path(tempfile.mkdtemp(prefix="env-", dir=staging))
  try:
    _build(staged, runtime_root, lock, relative)
  except BaseException:
    shutil.rmtree(staged, ignore_errors=True)
    raise
  return StagedEnv(key=key, root=staged, reused=False)


def publish_env(data_dir: Path | str, app_id: int, staged: StagedEnv) -> Path:
  """Atomically move a validated env to its keyed location."""
  target = envs_parent(data_dir, app_id) / staged.key
  if staged.reused:
    return target
  target.parent.mkdir(parents=True, exist_ok=True)
  try:
    # A venv's interpreter locates its prefix from pyvenv.cfg beside it, so
    # the renamed env is fully usable through bin/python.
    staged.root.rename(target)
  except OSError:
    if not _usable(target):
      raise
    shutil.rmtree(staged.root)
  return target


def discard_env(staged: StagedEnv | None) -> None:
  if staged is not None and not staged.reused:
    shutil.rmtree(staged.root, ignore_errors=True)


def discard_interrupted_builds(data_dir: Path | str) -> None:
  """Remove builds a crash left staged; call only before Apply can run."""
  shutil.rmtree(Path(data_dir) / ENVS_DIRNAME / _STAGING_DIRNAME, ignore_errors=True)


def prune_envs(data_dir: Path | str, app_id: int, kept_runtimes) -> int:
  """Remove an app's envs that no kept runtime tree references.

  The caller holds the runtime reader and job locks exclusively, so no
  process is running from a removed env. Envs for a previous interpreter are
  never referenced again and go with the first prune after replacement.
  """
  parent = envs_parent(data_dir, app_id)
  if not parent.is_dir():
    return 0
  referenced = {_declared_key(Path(root)) for root in kept_runtimes}
  removed = 0
  for env in parent.iterdir():
    if env.name not in referenced and env.is_dir() and not env.is_symlink():
      shutil.rmtree(env)
      removed += 1
  return removed
