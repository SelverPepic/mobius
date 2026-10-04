"""Unhandled route exceptions must leave a bounded trace agents can read.

uvicorn's own "Exception in ASGI application" report reaches only the
container's stdout. The request-error telemetry middleware, which already sees
every route exception, writes the traceback to the shared chat log once per
route and exception type per window, so a crash loop cannot flood the file.
"""

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import chat_logging, main


class _Capture(logging.Handler):
  def __init__(self):
    super().__init__()
    self.records: list[logging.LogRecord] = []

  def emit(self, record):
    self.records.append(record)


@pytest.fixture
def chat_log(monkeypatch):
  """Route the shared chat-log handler into memory; no database involved."""
  handler = _Capture()
  monkeypatch.setattr(chat_logging, "_chat_log_handler", handler)
  chat_logger = logging.getLogger("moebius.chat")
  monkeypatch.setattr(chat_logger, "handlers", [])
  monkeypatch.setattr(chat_logger, "level", chat_logger.level)
  return handler


def _crashing_client(monkeypatch):
  monkeypatch.setattr(main.activity, "record_request_error", lambda *a: None)
  app = FastAPI()

  @app.get("/crash/{item}")
  def crash(item: str):
    raise RuntimeError(f"boom {item}")

  @app.get("/other")
  def other():
    raise KeyError("missing")

  app.add_middleware(main._RequestErrorTelemetryMiddleware)
  return TestClient(app, raise_server_exceptions=False)


def test_route_crash_writes_its_traceback_to_the_chat_log(chat_log, monkeypatch):
  client = _crashing_client(monkeypatch)

  assert client.get("/crash/secret-name").status_code == 500

  [record] = chat_log.records
  assert record.levelno == logging.ERROR
  # The route template, never the raw path, identifies the failure.
  assert record.getMessage() == "unhandled exception in route GET /crash/{item}"
  assert isinstance(record.exc_info[1], RuntimeError)
  assert "boom secret-name" in chat_log.format(record)


def test_crash_burst_writes_one_traceback_per_route_and_type_per_window(
  chat_log, monkeypatch,
):
  clock = [1000.0]
  monkeypatch.setattr(main, "time", SimpleNamespace(monotonic=lambda: clock[0]))
  client = _crashing_client(monkeypatch)

  for i in range(50):
    client.get(f"/crash/{i}")
  client.get("/other")
  assert [type(r.exc_info[1]) for r in chat_log.records] == [
    RuntimeError, KeyError,
  ]

  clock[0] += main._RequestErrorTelemetryMiddleware._TRACEBACK_WINDOW_SEC
  client.get("/crash/again")
  assert len(chat_log.records) == 3
