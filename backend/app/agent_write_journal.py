"""Durable result-independent write intents, called only by the chat writer.

The actor supplies the already-authorized physical ChatRun. This journal owns
ordered admission/claim/settlement, not credentials, tool dispatch or model
continuation. Committing an executing state precedes the external effect; a
lost worker therefore becomes unknown and is never automatically replayed.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
import hashlib
from sqlalchemy import or_, case

from app import models
from app.agent_write_channel import WriteIntent

MAX_RUN_WRITES = 128
MAX_RUN_BYTES = 1024 * 1024
MAX_DIAGNOSTICS = 32


def _commit(db):
  try:
    db.commit()
  except Exception:
    db.rollback()
    raise


def _stream(db, run):
  stream = db.get(models.AgentWriteStream, run.id)
  if stream is None:
    stream = models.AgentWriteStream(run_id=run.id, chat_id=run.chat_id,
      sealed=False, accepted_count=0, accepted_bytes=0, diagnostics=[], item_receipts={})
    db.add(stream)
    db.flush()
  return stream


def _view(row):
  return {"id": row.operation_id, "tool": row.tool,
          "arguments": json.loads(row.arguments_json), "status": row.status,
          "run_id": row.source_run_id, "stage": row.stage, "reason": row.reason}


def _append_diagnostic(stream, diagnostic):
  """Bound details without making later failures look already delivered."""
  diagnostics = list(stream.diagnostics or [])
  if len(diagnostics) < MAX_DIAGNOSTICS:
    diagnostics.append(diagnostic)
  else:
    # Preserve the first bounded details, including records from older runs.
    # The count changes the report fingerprint even after acknowledgment.
    last = dict(diagnostics[-1])
    last["additional_diagnostics_omitted"] = last.get("additional_diagnostics_omitted", 0) + 1
    diagnostics[-1] = last
  stream.diagnostics = diagnostics
  stream.failure_delivered_by = None


def _reject(db, stream, reason, *, item_id=None, fingerprint=None, signature=None):
  # Fixed reasons only: argument values and exception text do not enter errors.
  _append_diagnostic(stream, {"stage": "admission", "reason": reason, **(
    {"item_id": item_id} if item_id else {})})
  receipt = {"status": "rejected", "reason": reason}
  if item_id and item_id not in (stream.item_receipts or {}) and len(stream.item_receipts or {}) < 256:
    stream.item_receipts = {**(stream.item_receipts or {}), item_id: {
      "fingerprint": fingerprint, "signature": signature, "receipt": receipt}}
  _commit(db)
  return receipt


def admit(db, run, *, item_id: str, fingerprint: str, writes: tuple[WriteIntent, ...]):
  stream = _stream(db, run)
  if (not isinstance(item_id, str) or not item_id or len(item_id) > 256
      or len(fingerprint) != 64
      or any(c not in "0123456789abcdef" for c in fingerprint)
      or not writes or len(writes) > 8):
    return _reject(db, stream, "invalid_item")
  if any(not isinstance(write, WriteIntent) for write in writes):
    return _reject(db, stream, "invalid_intent")
  signature = hashlib.sha256(json.dumps([(w.id, w.tool, w._arguments_json)
    for w in writes]).encode()).hexdigest()
  identity = {"item_id": item_id, "fingerprint": fingerprint, "signature": signature}
  prior_item = (stream.item_receipts or {}).get(item_id)
  if prior_item:
    if prior_item["fingerprint"] != fingerprint:
      return _reject(db, stream, "changed_item", **identity)
    if prior_item["signature"] != signature:
      return _reject(db, stream, "changed_intent", **identity)
    return prior_item["receipt"]
  if stream.sealed:
    return _reject(db, stream, "intake_closed", **identity)
  if len(stream.item_receipts or {}) >= 256:
    return _reject(db, stream, "item_capacity", **identity)
  root = run.root_run_id or run.id
  new = []
  negative_replays = []
  seen = set()
  for write in writes:
    if not isinstance(write, WriteIntent) or write.id in seen:
      return _reject(db, stream, "duplicate_or_invalid_intent", **identity)
    seen.add(write.id)
    old = db.get(models.AgentWriteIntent, (root, write.id))
    if old is not None:
      if old.chat_id != run.chat_id or old.tool != write.tool or old.arguments_json != write._arguments_json:
        return _reject(db, stream, "changed_intent", **identity)
      if old.status in {"failed", "unknown", "cancelled"}:
        negative_replays.append({"id": old.operation_id, "status": old.status,
                                "source_run_id": old.source_run_id})
      continue  # Includes failed/unknown outcomes: replay never resets them.
    new.append(write)
  size = sum(len(w._arguments_json.encode("utf-8")) for w in new)
  if stream.accepted_count + len(new) > MAX_RUN_WRITES or stream.accepted_bytes + size > MAX_RUN_BYTES:
    return _reject(db, stream, "run_capacity", **identity)
  now = datetime.now(UTC)
  for offset, write in enumerate(new):
    db.add(models.AgentWriteIntent(root_run_id=root, operation_id=write.id,
      chat_id=run.chat_id, source_run_id=run.id,
      ordinal=stream.accepted_count + offset, item_id=item_id,
      item_fingerprint=fingerprint, tool=write.tool, arguments_json=write._arguments_json,
      status="queued", stage="admission", created_at=now, updated_at=now))
  stream.accepted_count += len(new)
  stream.accepted_bytes += size
  receipt = {"status": "accepted", "new_count": len(new), "ids": [w.id for w in writes]}
  if negative_replays:
    receipt["negative_replays"] = negative_replays
    _append_diagnostic(stream, {"stage": "admission", "reason": "replayed_unsuccessful_write",
                               "item_id": item_id, "writes": negative_replays})
  stream.item_receipts = {**(stream.item_receipts or {}), item_id: {
    "fingerprint": fingerprint, "signature": signature, "receipt": receipt}}
  _commit(db)
  return receipt


def claim(db, run):
  # The actor serializes this check+transition+commit. No second worker can
  # receive a different write while this run's prior external effect executes.
  active = db.query(models.AgentWriteIntent.operation_id).filter_by(
    source_run_id=run.id, status="executing").first()
  if active:
    return {"status": "busy"}
  row = db.query(models.AgentWriteIntent).filter_by(
    source_run_id=run.id, status="queued").order_by(models.AgentWriteIntent.ordinal).first()
  if row is None:
    return {"status": "empty"}
  row.status = "executing"
  row.stage = "dispatch"
  row.updated_at = datetime.now(UTC)
  _commit(db)
  return {"status": "claimed", "write": _view(row)}


def settle(db, run, *, operation_id: str, status: str, reason: str | None):
  if status not in {"succeeded", "failed", "unknown"}:
    raise ValueError("Invalid write outcome")
  row = db.get(models.AgentWriteIntent, (run.root_run_id or run.id, operation_id))
  if row is None or row.source_run_id != run.id or row.chat_id != run.chat_id:
    return {"status": "not_owned"}
  if row.status not in {"executing", "unknown"}:
    return {"status": "settled", "outcome": row.status}
  row.status = status
  row.stage = "completion"
  # Callers must provide a bounded, redacted explanation, never tool payloads.
  row.reason = reason[:500] if reason else None
  row.updated_at = datetime.now(UTC)
  if status != "succeeded":
    _stream(db, run).failure_delivered_by = None
  _commit(db)
  return {"status": "settled", "outcome": status}


def seal(db, run):
  _stream(db, run).sealed = True
  _commit(db)
  return {"status": "sealed"}


def interrupt(db, *, chat_id: str, run_ids: tuple[str, ...], reason: str):
  """Stage recovery with the owning run transition; the caller commits it.

  Intents do not confer authority after Stop or worker loss. Keep every record,
  but never turn an ambiguous external effect back into queued work.
  """
  if not run_ids:
    return
  changed = db.query(models.AgentWriteIntent.source_run_id).filter(
    models.AgentWriteIntent.chat_id == chat_id,
    models.AgentWriteIntent.source_run_id.in_(run_ids),
    models.AgentWriteIntent.status.in_(("queued", "executing")),
  ).distinct().all()
  if changed:
    db.query(models.AgentWriteStream).filter(
      models.AgentWriteStream.run_id.in_([row[0] for row in changed]),
    ).update({"failure_delivered_by": None}, synchronize_session=False)
  db.query(models.AgentWriteStream).filter(
    models.AgentWriteStream.chat_id == chat_id,
    models.AgentWriteStream.run_id.in_(run_ids),
  ).update({"sealed": True}, synchronize_session=False)
  for old, new in (("queued", "cancelled"), ("executing", "unknown")):
    db.query(models.AgentWriteIntent).filter(
      models.AgentWriteIntent.chat_id == chat_id,
      models.AgentWriteIntent.source_run_id.in_(run_ids),
      models.AgentWriteIntent.status == old,
    ).update({"status": new, "stage": "interruption", "reason": reason,
              "updated_at": datetime.now(UTC)}, synchronize_session=False)


def outcomes(db, run):
  rows = db.query(models.AgentWriteIntent).filter_by(source_run_id=run.id).order_by(
    models.AgentWriteIntent.ordinal).all()
  stream = db.get(models.AgentWriteStream, run.id)
  return {"status": "ok", "writes": [_view(row) for row in rows],
          "diagnostics": list(stream.diagnostics or []) if stream else []}


def record_failure(db, run, *, stage: str, reason: str):
  stream = _stream(db, run)
  _append_diagnostic(stream, {"stage": stage[:40], "reason": reason[:500]})
  _commit(db)
  return {"status": "recorded"}


def failure_report(db, run):
  """Only negative outcomes enter model context; never arguments or success receipts."""
  intent = models.AgentWriteIntent
  rows = db.query(intent.operation_id, intent.tool, intent.status, intent.stage, intent.reason).filter(
    intent.source_run_id == run.id, intent.status.in_(("failed", "unknown", "cancelled")),
  ).order_by(intent.ordinal).all()
  failed = [{"id": row.operation_id, "tool": row.tool, "status": row.status,
             "stage": row.stage, "reason": row.reason} for row in rows]
  stream = db.get(models.AgentWriteStream, run.id)
  diagnostics = list(stream.diagnostics or []) if stream else []
  for row in failed:
    row["details_url"] = f"/api/chats/{run.chat_id}/write-outcomes/{run.id}/{row['id']}"
  if not failed and not diagnostics:
    return None
  report = {"source_run_id": run.id, "writes": failed, "diagnostics": diagnostics}
  report["fingerprint"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
  return report


def pending_failure_reports(db, *, chat_id: str, exclude_run_id: str, priority_run_id=None):
  """Bounded oldest-first backlog; unselected reports remain undelivered.

  A stopped/card-held run never wakes itself. Its next authorized attempt
  carries the retained failures and acknowledges them only after model use.
  """
  rows = (db.query(models.AgentWriteStream, models.ChatRun)
    .join(models.ChatRun, models.ChatRun.id == models.AgentWriteStream.run_id)
    .filter(models.AgentWriteStream.chat_id == chat_id,
            models.AgentWriteStream.run_id != exclude_run_id,
            models.AgentWriteStream.failure_delivered_by.is_(None),
            or_(models.AgentWriteStream.diagnostics != [],
              db.query(models.AgentWriteIntent.source_run_id).filter(
                models.AgentWriteIntent.source_run_id == models.AgentWriteStream.run_id,
                models.AgentWriteIntent.status.in_(("failed", "unknown", "cancelled")),
              ).exists()))
    .order_by(case((models.ChatRun.id == priority_run_id, 0), else_=1),
              models.ChatRun.started_at, models.ChatRun.id))
  reports, size = [], 0
  for stream, run in rows.yield_per(32):
    report = failure_report(db, run)
    if report is None:
      continue
    encoded = json.dumps(report, ensure_ascii=False)
    # Always include one complete report; one physical run is bounded at
    # admission. Do not truncate a report then falsely acknowledge all of it.
    if reports and (len(reports) >= 8 or size + len(encoded) > 32000):
      break
    reports.append(report)
    size += len(encoded)
  return reports


def acknowledge_failures(db, run, *, reports: tuple[tuple[str, str], ...]):
  if len(reports) > 8:
    raise ValueError("Too many failure reports")
  for source, fingerprint in reports:
    stream = db.get(models.AgentWriteStream, source)
    if stream is None or stream.chat_id != run.chat_id or source == run.id:
      raise ValueError("Failure report is outside this chat")
    current = failure_report(db, db.get(models.ChatRun, source))
    if current is not None and current["fingerprint"] == fingerprint:
      stream.failure_delivered_by = run.id
  _commit(db)
  return {"status": "acknowledged"}
