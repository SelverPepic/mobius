"""Short provider probes own the same post-exit cache lifecycle as turns."""
import asyncio
import subprocess
import threading
from types import SimpleNamespace

import pytest

from app import file_cache, provider_usage, providers
from app.routes import settings


@pytest.mark.parametrize('outcome', ['success', 'error', 'timeout', 'missing'])
def test_version_probe_reclaims_after_exit_without_changing_result(monkeypatch, outcome):
  events = []
  monkeypatch.setattr(settings.shutil, 'which', lambda _: None if outcome == 'missing' else '/tool')

  def run(*args, **kwargs):
    events.append('exit')
    if outcome == 'timeout':
      raise subprocess.TimeoutExpired('codex', 2)
    return SimpleNamespace(returncode=1 if outcome == 'error' else 0, stdout='version\n')

  monkeypatch.setattr(settings.subprocess, 'run', run)
  monkeypatch.setattr(file_cache, 'reclaim_provider_cache_sync', lambda p: events.append(p))
  assert settings._cli_version('codex') == ('version' if outcome == 'success' else None)
  assert events == ([] if outcome == 'missing' else ['exit', 'codex'])


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'error', 'invalid', 'timeout', 'cancel'])
async def test_catalog_probe_reaps_before_cache_advice_on_every_exit(monkeypatch, outcome):
  events = []

  class Process:
    returncode = None

    async def communicate(self):
      if outcome == 'timeout':
        raise asyncio.TimeoutError()
      if outcome == 'cancel':
        raise asyncio.CancelledError()
      self.returncode = 1 if outcome == 'error' else 0
      events.append('exit')
      return (b'bad' if outcome == 'invalid' else b'{"models":[{"slug":"test-model"}]}', b'failed')

    def kill(self):
      events.append('kill')

    async def wait(self):
      self.returncode = -9
      events.append('reaped')

  proc = Process()

  async def spawn(*args, **kwargs):
    return proc

  async def reclaim(provider):
    assert proc.returncode is not None
    events.append(provider)

  monkeypatch.setattr(providers.shutil, 'which', lambda _: '/codex')
  monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
  monkeypatch.setattr(file_cache, 'reclaim_provider_cache', reclaim)
  call = providers._fetch_codex_models_from_cli('/unused')
  if outcome == 'success':
    assert await call == [{'id': 'test-model'}]
  else:
    with pytest.raises(asyncio.CancelledError if outcome == 'cancel' else RuntimeError):
      await call
  assert events == (['kill', 'reaped', 'codex'] if outcome in ('timeout', 'cancel') else ['exit', 'codex'])


@pytest.mark.asyncio
@pytest.mark.parametrize('probe', ['usage', 'interaction'])
@pytest.mark.parametrize('outcome', ['success', 'error', 'timeout', 'cancel'])
async def test_account_probe_closes_then_reclaims_on_owned_worker(monkeypatch, tmp_path, probe, outcome):
  import openai_codex.client as sdk

  events = []
  closed = threading.Event()

  class Client:
    def __init__(self, _config):
      pass

    def close(self):
      events.append('closed')
      closed.set()

  def work(client):
    if outcome == 'error':
      raise RuntimeError('original')
    if outcome == 'cancel':
      raise asyncio.CancelledError()
    if outcome == 'timeout':
      assert closed.wait(2)
    return ('account', 'limits')

  def reclaim(provider):
    assert closed.is_set()
    assert threading.current_thread().name.startswith('mobius-codex-usage')
    events.append('cache')

  async def acquire(_):
    return SimpleNamespace(release=lambda: events.append('released'))

  monkeypatch.setattr(sdk, 'CodexClient', Client)
  monkeypatch.setattr(provider_usage.shutil, 'which', lambda _: '/codex')
  monkeypatch.setattr(provider_usage, '_PROVIDER_TIMEOUT_SECONDS', .02 if outcome == 'timeout' else 2)
  monkeypatch.setattr(provider_usage, 'normalize_codex_usage', lambda *a, **kw: 'normalized')
  monkeypatch.setattr(provider_usage, '_codex_plan_type', lambda _: None)
  monkeypatch.setattr(file_cache, 'reclaim_provider_cache_sync', reclaim)
  monkeypatch.setattr('app.codex_session_lock.acquire_codex_session_activity_async', acquire)

  class Limits:
    def model_dump(self, **kwargs):
      return {}

  def read(client):
    work(client)
    return None, Limits()

  monkeypatch.setattr(provider_usage, '_read_codex_client', read)
  call = (provider_usage._fetch_codex_usage(str(tmp_path)) if probe == 'usage' else
          provider_usage._run_on_codex_client(str(tmp_path), work, timeout_error='timeout'))
  if outcome == 'success':
    assert await call == ('normalized' if probe == 'usage' else ('account', 'limits'))
  else:
    with pytest.raises(asyncio.CancelledError if outcome == 'cancel' else RuntimeError):
      await call
  assert events[-2:] == (['cache', 'released'] if probe == 'usage' else ['closed', 'cache'])


def test_sync_advice_failure_never_masks_probe_result(monkeypatch):
  def fail(_):
    raise OSError('unavailable')
  monkeypatch.setattr(file_cache, 'provider_tool_paths', fail)
  assert file_cache.reclaim_provider_cache_sync('codex') is None
