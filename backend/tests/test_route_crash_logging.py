"""Unhandled route exceptions must leave a trace agents can read.

uvicorn logs "Exception in ASGI application" on `uvicorn.error`, whose only
handler writes to the container's stdout. Startup copies those errors into
the shared chat log so a one-off 500 (for example a failed archive) can be
diagnosed from inside Möbius.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app import chat_logging, startup
from app.startup import DatabaseBootResult, StartupContext, run_startup_plan

_ROUTED_LOGGERS = (
  "uvicorn.error",
  "app.providers.models",
  "moebius.memory",
  "app.helper_hosts",
  "app.claude_helper_host",
)


class _Capture(logging.Handler):
  def __init__(self):
    super().__init__()
    self.records: list[logging.LogRecord] = []

  def emit(self, record):
    self.records.append(record)


@pytest.fixture
def capture(monkeypatch):
  """Route the chat log into memory and restore every touched logger."""
  handler = _Capture()
  monkeypatch.setattr(chat_logging, "_chat_log_handler", handler)
  saved = {
    name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level)
    for name in _ROUTED_LOGGERS
  }
  uvicorn_errors = logging.getLogger("uvicorn.error")
  uvicorn_errors.handlers = [
    h for h in uvicorn_errors.handlers
    if not isinstance(h, startup._ErrorsToChatLog)
  ]
  uvicorn_errors.setLevel(logging.INFO)
  try:
    yield handler
  finally:
    for name, (handlers, level) in saved.items():
      logging.getLogger(name).handlers = handlers
      logging.getLogger(name).setLevel(level)


def _forwarders():
  return [
    h for h in logging.getLogger("uvicorn.error").handlers
    if isinstance(h, startup._ErrorsToChatLog)
  ]


def test_route_crash_reaches_chat_log_without_quieting_uvicorn(capture):
  uvicorn_errors = logging.getLogger("uvicorn.error")
  startup._route_diagnostics_to_chat_log(None)
  startup._route_diagnostics_to_chat_log(None)  # idempotent across calls
  assert len(_forwarders()) == 1

  uvicorn_errors.info("Application startup complete.")
  try:
    raise RuntimeError("boom")
  except RuntimeError as exc:
    uvicorn_errors.error("Exception in ASGI application\n", exc_info=exc)

  assert [r.getMessage() for r in capture.records] == [
    "Exception in ASGI application\n",
  ]
  assert capture.records[0].exc_info is not None
  # Console lifecycle lines still pass uvicorn's own level gate.
  assert uvicorn_errors.isEnabledFor(logging.INFO)


def test_shutdown_cancellation_is_not_recorded_as_a_crash(capture):
  startup._route_diagnostics_to_chat_log(None)
  try:
    raise asyncio.CancelledError()
  except asyncio.CancelledError as exc:
    logging.getLogger("uvicorn.error").error(
      "Exception in ASGI application\n", exc_info=exc,
    )

  assert capture.records == []


@pytest.mark.asyncio
async def test_forwarder_is_attached_when_database_boot_is_degraded(
  capture, monkeypatch,
):
  def fail_init():
    raise RuntimeError("broken migration")

  ctx = StartupContext(
    app=SimpleNamespace(state=SimpleNamespace()),
    settings=SimpleNamespace(data_dir="/tmp"),
    boot_id="test-boot",
    init_db=fail_init,
    install_pm_commit_launcher=lambda _source, _target: False,
    assert_provider_defaults=lambda _names: None,
    logger=logging.getLogger("test.startup"),
  )
  # Keep the production routing task itself, but skip unrelated process work.
  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", tuple(
    task for task in startup.PROCESS_STARTUP_TASKS
    if task.name in {"route diagnostics to chat log", "initialize database"}
  ))
  monkeypatch.setattr(startup, "record_memory_checkpoint", lambda _name: None)

  result = await run_startup_plan(ctx)

  assert result == DatabaseBootResult(
    failure_reason="database_initialization_failed",
  )
  assert len(_forwarders()) == 1
