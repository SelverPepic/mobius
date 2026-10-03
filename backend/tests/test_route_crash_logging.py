"""Unhandled route exceptions must leave a trace agents can read.

uvicorn logs "Exception in ASGI application" on `uvicorn.error`, whose only
handler writes to the container's stdout. Startup copies those errors into
the shared chat log so a one-off 500 (for example a failed archive) can be
diagnosed from inside Möbius.
"""

import logging

from app import chat_logging, startup


class _Capture(logging.Handler):
  def __init__(self):
    super().__init__()
    self.records: list[logging.LogRecord] = []

  def emit(self, record):
    self.records.append(record)


def test_route_crash_reaches_chat_log_without_quieting_uvicorn(monkeypatch):
  capture = _Capture()
  monkeypatch.setattr(chat_logging, "_chat_log_handler", capture)
  uvicorn_errors = logging.getLogger("uvicorn.error")
  previous_handlers = list(uvicorn_errors.handlers)
  previous_level = uvicorn_errors.level
  uvicorn_errors.handlers = [
    h for h in previous_handlers if not isinstance(h, startup._ErrorsToChatLog)
  ]
  uvicorn_errors.setLevel(logging.INFO)
  try:
    startup._route_diagnostics_to_chat_log(None)
    startup._route_diagnostics_to_chat_log(None)  # idempotent across calls
    assert sum(
      isinstance(h, startup._ErrorsToChatLog) for h in uvicorn_errors.handlers
    ) == 1

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
  finally:
    uvicorn_errors.handlers = previous_handlers
    uvicorn_errors.setLevel(previous_level)
