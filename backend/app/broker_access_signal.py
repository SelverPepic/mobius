"""Wake open MCP broker exchanges only when their access may have changed.

A brokered MCP exchange can stream for minutes. Its access depends on four
kinds of rows: the connection itself (enabled, healthy, same generation), the
owner's sign-in epoch, and, for a shared browser, one browser grant plus the
owner's mobius.you account link that an account grant is bound to. Polling
those rows per open stream costs a database query every tick on the event
loop, so instead every committed session write that touches one of those
tables advances one in-process revision. An open exchange waits on that
revision and rechecks its lineage only after it moves.

Inserts never revoke an existing exchange, so only updates and deletes count;
an unrelated column change on a watched row merely costs one extra recheck.
Writes from another process (an operator script or direct SQL) cannot reach
this signal, so callers keep a slow safety recheck to bound that delay.
"""

from __future__ import annotations

import asyncio
import threading

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app import models
from app.browser_access import BrowserAccessGrant

# Every table the broker lineage check reads must be listed here, or a change
# to it would go unseen until the slow safety recheck.
ACCESS_TABLES = frozenset({
  models.Connector.__tablename__,
  models.Owner.__tablename__,
  BrowserAccessGrant.__tablename__,
  models.IdentityAccountLink.__tablename__,
})
_PENDING_KEY = "mobius_broker_access_changed"

_lock = threading.Lock()
_revision = 0
_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []


def current_revision() -> int:
  """Revision to capture before validating access an exchange will rely on."""
  with _lock:
    return _revision


def notify_access_changed() -> None:
  """Advance the revision and wake every waiting exchange, from any thread."""
  global _revision
  with _lock:
    _revision += 1
    waiters = list(_waiters)
    _waiters.clear()
  for loop, future in waiters:
    try:
      loop.call_soon_threadsafe(_wake, future)
    except RuntimeError:
      pass  # That loop already closed; nothing on it is waiting any more.


def _wake(future: asyncio.Future) -> None:
  if not future.done():
    future.set_result(None)


async def wait_for_change(seen: int, timeout: float) -> int:
  """Return the revision once it differs from ``seen``, or after ``timeout``."""
  loop = asyncio.get_running_loop()
  future = loop.create_future()
  entry = (loop, future)
  with _lock:
    if _revision != seen:
      return _revision
    _waiters.append(entry)
  try:
    await asyncio.wait({future}, timeout=timeout)
  finally:
    with _lock:
      if entry in _waiters:
        _waiters.remove(entry)
  return current_revision()


def _touches_access(instance) -> bool:
  table = getattr(inspect(instance).mapper.local_table, "name", None)
  return table in ACCESS_TABLES


@event.listens_for(Session, "after_flush")
def _mark_flushed_access_change(session, _flush_context) -> None:
  changed = any(_touches_access(obj) for obj in session.deleted) or any(
    _touches_access(obj) and session.is_modified(obj, include_collections=False)
    for obj in session.dirty
  )
  if changed:
    session.info[_PENDING_KEY] = True


@event.listens_for(Session, "do_orm_execute")
def _mark_statement_access_change(state) -> None:
  if not (state.is_update or state.is_delete):
    return
  table = getattr(state.statement, "table", None)
  if getattr(table, "name", None) in ACCESS_TABLES:
    state.session.info[_PENDING_KEY] = True


@event.listens_for(Session, "after_commit")
def _publish_committed_access_change(session) -> None:
  # SQLAlchemy also fires this when a savepoint is released; the change is not
  # visible to other sessions until the outermost transaction commits.
  if session.in_nested_transaction():
    return
  if session.info.pop(_PENDING_KEY, False):
    notify_access_changed()


@event.listens_for(Session, "after_transaction_end")
def _forget_uncommitted_access_change(session, transaction) -> None:
  # Only the outermost transaction ends the unit of work; a rolled-back
  # savepoint must not drop a change its committed parent still carries.
  if transaction.parent is None:
    session.info.pop(_PENDING_KEY, None)
