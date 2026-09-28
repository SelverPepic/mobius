"""An app's declared Python lock becomes its own environment on /data."""

import base64
import hashlib
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import app_python_env, app_services, applied_app_runtime, models
from app.config import get_settings
from app.manifest_contract import ManifestContractError, validate_manifest_contract


@pytest.fixture(autouse=True)
def _fresh_key_cache():
  app_python_env._declared_key.cache_clear()
  yield
  app_python_env._declared_key.cache_clear()


def _data_dir() -> Path:
  return Path(get_settings().data_dir)


def _manifest(**extra) -> dict:
  return {
    "id": "deps-demo", "name": "Deps", "version": "0.1.0",
    "description": "Declares its Python.", "entry": "index.jsx",
    "permissions": {}, **extra,
  }


def _tiny_wheel(directory: Path, name: str = "tinypkg", version: str = "1.0") -> Path:
  """A pure-Python wheel pip installs offline from a local find-links dir."""
  directory.mkdir(parents=True, exist_ok=True)
  info = f"{name}-{version}.dist-info"
  files = {
    f"{name}/__init__.py": b"VALUE = 42\n",
    f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
    f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
  }
  record = []
  for path, content in files.items():
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    record.append(f"{path},sha256={digest},{len(content)}")
  record.append(f"{info}/RECORD,,")
  files[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode()
  wheel = directory / f"{name}-{version}-py3-none-any.whl"
  with zipfile.ZipFile(wheel, "w") as archive:
    for path, content in files.items():
      archive.writestr(path, content)
  return wheel


def _lock_for(wheel: Path, name: str = "tinypkg", version: str = "1.0") -> str:
  digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
  return f"{name}=={version} \\\n    --hash=sha256:{digest}\n"


def _offline_index(monkeypatch, wheels: Path) -> None:
  wheels.mkdir(parents=True, exist_ok=True)
  monkeypatch.setenv("PIP_NO_INDEX", "1")
  monkeypatch.setenv("PIP_FIND_LINKS", str(wheels))


SERVICE = b'''import json, sys
import tinypkg

if __name__ == "__main__":
  json.load(sys.stdin)
  try:
    import fastapi  # the platform's own library
    borrowed = True
  except ImportError:
    borrowed = False
  print(json.dumps({"status": 200, "body": {
    "value": tinypkg.VALUE, "prefix": sys.prefix, "borrowed": borrowed,
  }}))
'''


def _runtime_tree(root: Path, *, lock: str | None, service: bytes | None = SERVICE,
                  job: bytes | None = None) -> Path:
  root.mkdir(parents=True)
  extra = {"source_files": []}
  if service is not None:
    (root / "service.py").write_bytes(service)
    extra["service"] = {"entry": "service.py"}
    extra["source_files"].append("service.py")
  if job is not None:
    (root / "job.py").write_bytes(job)
    extra["schedule"] = {"job": "job.py"}
  if lock is not None:
    (root / "requirements.lock").write_text(lock)
    extra["python"] = {"lock": "requirements.lock"}
    extra["source_files"].append("requirements.lock")
  (root / "mobius.json").write_text(json.dumps(_manifest(**extra)))
  return root


def _fake_env(env: Path) -> None:
  """What a venv looks like to the resolver, without building one."""
  (env / "bin").mkdir(parents=True)
  (env / "pyvenv.cfg").write_text("home = /usr/local/bin\n")
  (env / "bin" / "python").symlink_to(sys.executable)


# --- Manifest contract -------------------------------------------------------

def test_manifest_accepts_a_python_lock_listed_as_a_source_file():
  validate_manifest_contract(_manifest(
    python={"lock": "requirements.lock"}, source_files=["requirements.lock"],
  ))


@pytest.mark.parametrize(("python", "sources", "message"), [
  ({"lock": "requirements.lock"}, [], "listed in `source_files`"),
  ({"lock": "requirements.lock"}, None, "listed in `source_files`"),
  ({"lock": "../outside.lock"}, ["../outside.lock"], "'..'"),
  ({"lock": "/etc/requirements.lock"}, ["/etc/requirements.lock"], "relative path"),
  ({"lock": "requirements.lock", "index": "x"}, ["requirements.lock"], "only `lock`"),
  ("requirements.lock", ["requirements.lock"], "only `lock`"),
])
def test_manifest_rejects_a_python_lock_outside_the_reviewed_source(python, sources, message):
  extra = {"python": python}
  if sources is not None:
    extra["source_files"] = sources
  with pytest.raises(ManifestContractError, match=message):
    validate_manifest_contract(_manifest(**extra))


# --- Environment key and resolution ------------------------------------------

def test_env_key_changes_with_the_lock_and_with_the_interpreter(monkeypatch):
  first = app_python_env.env_key(b"a==1\n")
  assert first == app_python_env.env_key(b"a==1\n")
  assert first != app_python_env.env_key(b"a==2\n")
  assert first.startswith(sys.implementation.cache_tag)
  assert hashlib.sha256(b"a==1\n").hexdigest() in first
  monkeypatch.setattr(app_python_env, "compatibility_tag", lambda: "cpython-399-other")
  assert app_python_env.env_key(b"a==1\n") != first


def test_undeclared_app_keeps_the_platform_interpreter(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock=None)
  assert app_python_env.interpreter(tmp_path, 7, root) == sys.executable
  assert app_python_env.prepare_env(tmp_path, 7, root) is None


def test_declared_app_never_falls_back_to_the_platform_interpreter(tmp_path, monkeypatch):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  with pytest.raises(app_python_env.PythonEnvUnavailable, match="Apply the app again"):
    app_python_env.interpreter(tmp_path, 7, root)

  env = app_python_env.envs_parent(tmp_path, 7) / app_python_env.env_key(b"a==1\n")
  _fake_env(env)
  assert app_python_env.interpreter(tmp_path, 7, root) == str(env / "bin" / "python")

  # A replaced image with another interpreter selects another key: the old
  # env is not used and nothing substitutes the platform interpreter.
  monkeypatch.setattr(app_python_env, "compatibility_tag", lambda: "cpython-399-other")
  app_python_env._declared_key.cache_clear()
  with pytest.raises(app_python_env.PythonEnvUnavailable):
    app_python_env.interpreter(tmp_path, 7, root)


def test_env_whose_base_interpreter_vanished_is_unavailable(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  env = app_python_env.envs_parent(tmp_path, 7) / app_python_env.env_key(b"a==1\n")
  (env / "bin").mkdir(parents=True)
  (env / "pyvenv.cfg").write_text("home = /gone\n")
  (env / "bin" / "python").symlink_to(tmp_path / "gone" / "python3")
  with pytest.raises(app_python_env.PythonEnvUnavailable):
    app_python_env.interpreter(tmp_path, 7, root)


@pytest.mark.parametrize(("shebang", "arguments"), [
  (("/usr/bin/env", "python3"), ()),
  (("/usr/bin/env", "-S", "python3", "-u"), ("-u",)),
  (("/usr/local/bin/python3.12",), ()),
  (("/usr/bin/python", "-X", "utf8"), ("-X", "utf8")),
])
def test_python_job_in_a_declaring_app_runs_with_its_env(tmp_path, shebang, arguments):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  env = app_python_env.envs_parent(tmp_path, 7) / app_python_env.env_key(b"a==1\n")
  _fake_env(env)
  assert app_python_env.job_interpreter_in_env(shebang, tmp_path, 7, root) == (
    str(env / "bin" / "python"), *arguments,
  )


@pytest.mark.parametrize("shebang", [
  ("/usr/bin/env", "bash"), ("/bin/sh",), ("/usr/bin/env", "node"),
])
def test_non_python_job_is_unchanged_even_without_a_built_env(tmp_path, shebang):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  assert app_python_env.job_interpreter_in_env(shebang, tmp_path, 7, root) == shebang


def test_python_job_without_a_declaration_is_unchanged(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock=None)
  shebang = ("/usr/bin/env", "python3")
  assert app_python_env.job_interpreter_in_env(shebang, tmp_path, 7, root) == shebang


def _load_runner():
  path = Path(__file__).resolve().parent.parent / "scripts" / "app-job-runner.py"
  spec = importlib.util.spec_from_file_location("app_job_runner_env", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_job_runner_selects_the_env_for_python_jobs_only(tmp_path, monkeypatch):
  runner = _load_runner()
  monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
  root = _runtime_tree(
    tmp_path / "rev", lock="a==1\n", job=b"#!/usr/bin/env python3\nprint(1)\n",
  )
  (root / "job.sh").write_bytes(b"#!/usr/bin/env bash\necho hi\n")
  with pytest.raises(app_python_env.PythonEnvUnavailable):
    runner._job_command(root / "job.py", 7)
  assert runner._job_command(root / "job.sh", 7) == [
    "/usr/bin/env", "bash", str(root / "job.sh"), "7",
  ]
  env = app_python_env.envs_parent(tmp_path, 7) / app_python_env.env_key(b"a==1\n")
  _fake_env(env)
  assert runner._job_command(root / "job.py", 7) == [
    str(env / "bin" / "python"), str(root / "job.py"), "7",
  ]


# --- Services and preload hosts ----------------------------------------------

def test_service_without_its_env_fails_visibly_instead_of_borrowing(tmp_path, monkeypatch):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  app = models.App(id=7, slug="deps-demo")
  with pytest.raises(HTTPException) as caught:
    app_services.service_interpreter(app, root / "service.py")
  assert caught.value.status_code == 503
  assert "Apply the app again" in caught.value.detail


@pytest.mark.asyncio
async def test_spawned_request_and_preload_host_both_use_the_resolved_interpreter(
  monkeypatch, tmp_path,
):
  from app import service_preload
  launched = []

  async def record(program, *args, **kwargs):
    launched.append(program)
    raise OSError("recorded")

  monkeypatch.setattr(app_services.asyncio, "create_subprocess_exec", record)
  with pytest.raises(HTTPException):
    await app_services._run_spawned("/envs/7/bin/python", tmp_path / "s.py", {}, b"{}", 1)
  with pytest.raises(OSError):
    await service_preload.start((7, "rev"), "demo", "/envs/7/bin/python", tmp_path / "s.py", {})
  assert launched == ["/envs/7/bin/python", "/envs/7/bin/python"]


# --- Build ---------------------------------------------------------------------

def test_real_build_installs_the_lock_in_an_isolated_env_and_serves_with_it(
  client, auth, db, monkeypatch, tmp_path,
):
  wheels = tmp_path / "wheels"
  _offline_index(monkeypatch, wheels)
  lock = _lock_for(_tiny_wheel(wheels))
  source = _data_dir() / "apps" / "deps-demo"
  source.mkdir(parents=True)
  app = models.App(
    name="Deps", slug="deps-demo", description="", source_dir=str(source),
    jsx_source="export default () => null",
    capability_contract={"schema": 6, "service": {
      "entry": "service.py", "access": "self", "protocol": "json-v1",
    }},
  )
  db.add(app)
  db.commit()
  revision = "b" * 64
  root = _runtime_tree(applied_app_runtime.runtime_parent(app.id) / revision, lock=lock)

  staged = app_python_env.prepare_env(_data_dir(), app.id, root)
  assert staged is not None and not staged.reused
  assert staged.root.parent == _data_dir() / "app-envs" / ".staging"
  env = app_python_env.publish_env(_data_dir(), app.id, staged)
  assert env == app_python_env.envs_parent(_data_dir(), app.id) / staged.key
  assert not staged.root.exists()
  reused = app_python_env.prepare_env(_data_dir(), app.id, root)
  assert reused.reused and reused.root == env

  app.runtime_revision = revision
  db.commit()
  response = client.post(f"/api/apps/{app.id}/service/probe", headers=auth, json={})
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["value"] == 42
  assert Path(body["prefix"]) == env
  # Without system site-packages the app cannot silently borrow platform libraries.
  assert body["borrowed"] is False


def test_build_failure_names_the_package_and_leaves_nothing_behind(monkeypatch, tmp_path):
  wheels = tmp_path / "wheels"
  _offline_index(monkeypatch, wheels)
  wheel = _tiny_wheel(tmp_path / "elsewhere", name="missingpkg")
  root = _runtime_tree(tmp_path / "rev", lock=_lock_for(wheel, name="missingpkg"))
  with pytest.raises(app_python_env.PythonEnvBuildError, match="missingpkg"):
    app_python_env.prepare_env(tmp_path, 7, root)
  assert list((tmp_path / "app-envs" / ".staging").iterdir()) == []
  assert not app_python_env.envs_parent(tmp_path, 7).exists()


def test_build_rejects_a_lock_whose_hash_does_not_match(monkeypatch, tmp_path):
  wheels = tmp_path / "wheels"
  _offline_index(monkeypatch, wheels)
  _tiny_wheel(wheels)
  lock = "tinypkg==1.0 \\\n    --hash=sha256:" + "0" * 64 + "\n"
  root = _runtime_tree(tmp_path / "rev", lock=lock)
  with pytest.raises(app_python_env.PythonEnvBuildError, match="HASH|hash"):
    app_python_env.prepare_env(tmp_path, 7, root)


def test_build_smoke_runs_the_service_setup_and_names_a_missing_import(
  monkeypatch, tmp_path,
):
  wheels = tmp_path / "wheels"
  _offline_index(monkeypatch, wheels)
  lock = _lock_for(_tiny_wheel(wheels))
  service = b"import tinypkg\nimport notlocked\n"
  root = _runtime_tree(tmp_path / "rev", lock=lock, service=service)
  with pytest.raises(app_python_env.PythonEnvBuildError, match="notlocked"):
    app_python_env.prepare_env(tmp_path, 7, root)


def test_missing_declared_lock_fails_the_build_clearly(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  (root / "requirements.lock").unlink()
  with pytest.raises(app_python_env.PythonEnvBuildError, match="missing from the app source"):
    app_python_env.prepare_env(tmp_path, 7, root)


def test_interrupted_builds_are_discarded(tmp_path):
  staging = tmp_path / "app-envs" / ".staging" / "env-crashed"
  staging.mkdir(parents=True)
  app_python_env.discard_interrupted_builds(tmp_path)
  assert not staging.exists()


# --- Apply -----------------------------------------------------------------

def _source(lock: str | None) -> Path:
  root = _data_dir() / "apps" / "deps-demo"
  root.mkdir(parents=True, exist_ok=True)
  extra = {"source_files": []}
  if lock is not None:
    (root / "requirements.lock").write_text(lock)
    extra = {"python": {"lock": "requirements.lock"}, "source_files": ["requirements.lock"]}
  (root / "mobius.json").write_text(json.dumps(_manifest(**extra)))
  (root / "index.jsx").write_text("export default function App() { return <div>x</div> }\n")
  return root


def test_apply_build_failure_keeps_the_previous_revision_live(
  client, auth, db, monkeypatch, tmp_path,
):
  _offline_index(monkeypatch, tmp_path / "wheels")
  source = _source(lock=None)
  first = client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)
  assert first.status_code == 200, first.text
  app_id = first.json()["app"]["id"]
  previous = db.get(models.App, app_id).runtime_revision

  wheel = _tiny_wheel(tmp_path / "elsewhere", name="missingpkg")
  _source(lock=_lock_for(wheel, name="missingpkg"))
  failed = client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)

  assert failed.status_code == 422, failed.text
  detail = failed.json()["detail"]
  assert detail["code"] == "python_env_failed"
  assert "missingpkg" in detail["message"]
  db.expire_all()
  assert db.get(models.App, app_id).runtime_revision == previous
  assert not app_python_env.envs_parent(_data_dir(), app_id).exists()


def test_apply_publishes_the_env_before_the_runtime_and_rebuilds_after_image_change(
  client, auth, db, monkeypatch,
):
  builds = []

  def fake_build(staged, runtime_root, lock, relative):
    builds.append(relative)
    staged.rmdir()
    _fake_env(staged)

  monkeypatch.setattr(app_python_env, "_build", fake_build)
  source = _source(lock="a==1\n")
  created = client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)
  assert created.status_code == 200, created.text
  row = db.get(models.App, created.json()["app"]["id"])
  root = applied_app_runtime.runtime_root(row)
  first_env = app_python_env.envs_parent(_data_dir(), row.id) / app_python_env.env_key(b"a==1\n")
  assert app_python_env.interpreter(_data_dir(), row.id, root) == str(first_env / "bin" / "python")
  assert builds == ["requirements.lock"]

  again = client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)
  assert again.status_code == 200, again.text
  assert builds == ["requirements.lock"]  # a matching env is reused

  # A new image's interpreter: the env is reported missing until Apply rebuilds it.
  monkeypatch.setattr(app_python_env, "compatibility_tag", lambda: "cpython-399-other")
  app_python_env._declared_key.cache_clear()
  with pytest.raises(app_python_env.PythonEnvUnavailable):
    app_python_env.interpreter(_data_dir(), row.id, root)
  rebuilt = client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)
  assert rebuilt.status_code == 200, rebuilt.text
  assert len(builds) == 2
  assert app_python_env.interpreter(_data_dir(), row.id, root).startswith(
    str(app_python_env.envs_parent(_data_dir(), row.id) / "cpython-399-other")
  )


# --- GC --------------------------------------------------------------------------

def test_env_gc_follows_kept_runtimes_and_waits_for_pins(db, tmp_path):
  source = _data_dir() / "apps" / "gc-demo"
  source.mkdir(parents=True)
  row = models.App(name="GC", slug="gc-demo", description="", source_dir=str(source),
                   jsx_source="export default () => null")
  db.add(row)
  db.commit()
  revisions = [str(index) * 64 for index in range(1, 4)]
  envs = {}
  for index, revision in enumerate(revisions):
    lock = f"a=={index}\n"
    _runtime_tree(applied_app_runtime.runtime_parent(row.id) / revision, lock=lock)
    envs[revision] = app_python_env.envs_parent(_data_dir(), row.id) / app_python_env.env_key(lock.encode())
    _fake_env(envs[revision])
  stale_interpreter = app_python_env.envs_parent(_data_dir(), row.id) / "cpython-311-old-abc"
  _fake_env(stale_interpreter)
  row.runtime_revision = revisions[-1]
  db.commit()

  pin = applied_app_runtime.hold_runtime(row.id)
  try:
    applied_app_runtime.prune_runtime(row, previous_revision=revisions[-2])
    assert all(env.is_dir() for env in envs.values()) and stale_interpreter.is_dir()
  finally:
    pin.close()

  assert applied_app_runtime.prune_runtime(row, previous_revision=revisions[-2]) == 1
  assert {path.name for path in app_python_env.envs_parent(_data_dir(), row.id).iterdir()} == {
    envs[revisions[-2]].name, envs[revisions[-1]].name,
  }
