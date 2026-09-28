"""Serve immutable, chat-scoped agent deliverables by their recorded name."""

import json
import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import generated_files, models
from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.deps import resolve_media_or_header_owner
from app.path_utils import validate_chat_id

router = APIRouter(prefix="/api/chats", tags=["generated-files"])

_VIEWED_RASTER_TYPES = generated_files.VIEWED_RASTER_MIME_TYPES

# Unknown and active-content formats always download. A small explicit set of
# browser-native document/media formats may opt into inline viewing; nosniff
# keeps an agent-authored payload from changing that reviewed type boundary.


class _AnchoredFileResponse(FileResponse):
  """Serve an already-open inode without letting ASGI reopen a mutable path."""

  def __init__(self, file_fd: int, **kwargs):
    self._file_fd = file_fd
    super().__init__(f"/proc/self/fd/{file_fd}", **kwargs)

  async def __call__(self, scope, receive, send):
    # A pathsend-capable server may resolve the path after this app coroutine
    # returns. Force FileResponse's ordinary range-aware read while our held
    # descriptor is still alive.
    extensions = scope.get("extensions") or {}
    if "http.response.pathsend" in extensions:
      scope = {**scope, "extensions": {
        key: value for key, value in extensions.items()
        if key != "http.response.pathsend"
      }}
    try:
      await super().__call__(scope, receive, send)
    finally:
      os.close(self._file_fd)


def _viewed_inbox_name(
  chat, tool_use_id: str, data_dir: str,
) -> tuple[str, bool, str | None] | None:
  """Only a completed image-view event grants access to its exact inbox file."""
  inbox = Path(data_dir) / "chats" / chat.id / "deliverables" / "inbox"
  sources = [(chat.live_assistant, True)]
  sources.extend((message, False) for message in reversed(chat.messages or []))
  for message, live in sources:
    if not isinstance(message, dict):
      continue
    for block in reversed(message.get("blocks") or []):
      if (
        not isinstance(block, dict)
        or block.get("type") != "tool"
        or block.get("tool_use_id") != tool_use_id
        or block.get("tool") not in {"ViewImage", "view_image", "Read"}
        or block.get("status") != "done"
      ):
        continue
      raw = block.get("input")
      if not isinstance(raw, str):
        return None
      if raw.lstrip().startswith("{"):
        try:
          raw = json.loads(raw).get("path")
        except (ValueError, AttributeError):
          return None
      if not isinstance(raw, str):
        return None
      path = Path(raw)
      if path.parent != inbox or path.name in {"", ".", ".."}:
        return None
      if mimetypes.guess_type(path.name)[0] not in _VIEWED_RASTER_TYPES:
        return None
      # None is a pre-binding transcript. An empty digest is a new view whose
      # bytes could not be safely read, so it must never fall back by name.
      digest = block.get("viewed_image_sha256")
      if digest is not None and (
        not isinstance(digest, str)
        or (digest and (
          len(digest) != 64
          or any(c not in "0123456789abcdef" for c in digest)
        ))
      ):
        return None
      if digest is None and not any(
        isinstance(other, dict)
        and other.get("type") == "generated_files"
        and any(
          isinstance(entry, dict) and entry.get("name") == path.name
          for entry in (other.get("files") or [])
        )
        for other in message.get("blocks", [])
      ):
        return None
      return path.name, live, digest
  return None


@router.get("/{chat_id}/viewed-generated-images/{tool_use_id}")
def serve_viewed_generated_image(
  chat_id: str,
  tool_use_id: str,
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Show an image the agent actually viewed, before or after inbox capture."""
  validate_chat_id(chat_id)
  resolve_media_or_header_owner(
    token_src.token, db, chat_id=chat_id, from_query=token_src.from_query,
  )
  chat = db.get(models.Chat, chat_id)
  if chat is None:
    raise HTTPException(status_code=404, detail="Image not found.")
  viewed = _viewed_inbox_name(chat, tool_use_id, get_settings().data_dir)
  if viewed is None:
    raise HTTPException(status_code=404, detail="Image not found.")
  name, live, digest = viewed
  if digest == "":
    raise HTTPException(status_code=404, detail="Image not found.")
  data_dir = get_settings().data_dir
  row = db.query(models.GeneratedFile).filter_by(chat_id=chat_id, name=name).first()
  if row is not None and row.mime_type not in _VIEWED_RASTER_TYPES:
    row = None

  # Current views may use either matching source, preferring the inbox while
  # live and the frozen attachment after turn completion. Legacy views lack a
  # fingerprint: serve only their same-turn published attachment, never a
  # mutable inbox file now occupying that name.
  frozen = ("files", row.path if row else None)
  inbox = ("inbox", name)
  if digest is None:
    candidates = (frozen,)
  else:
    candidates = (inbox, frozen) if live else (frozen, inbox)
  for collection, candidate in candidates:
    if candidate is None:
      continue
    try:
      opener = generated_files.open_inbox_file if collection == "inbox" else generated_files.open_stored_file
      file_fd, file_stat = opener(data_dir, chat_id, candidate)
    except OSError:
      continue
    if digest is not None:
      actual = generated_files.sha256_open_file(file_fd, file_stat)
      if actual is None or actual != digest:
        os.close(file_fd)
        continue
    media_type = mimetypes.guess_type(name)[0]
    try:
      return _AnchoredFileResponse(
        file_fd,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        stat_result=file_stat,
      )
    except Exception:
      os.close(file_fd)
      raise
  raise HTTPException(status_code=404, detail="Image not found.")


@router.get("/{chat_id}/generated-files/{name}")
def serve_generated_file(
  chat_id: str,
  name: str = PathParam(...),
  preview: bool = False,
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Serves an agent-generated file. Auth mirrors uploads.py's serve_upload:
  JWT from header or a short-lived media token on ?token= (never an
  unscoped owner JWT in a query string — those leak into logs/history/
  Referer)."""
  validate_chat_id(chat_id)
  resolve_media_or_header_owner(
    token_src.token, db, chat_id=chat_id, from_query=token_src.from_query,
  )

  row = db.query(models.GeneratedFile).filter(
    models.GeneratedFile.chat_id == chat_id,
    models.GeneratedFile.name == name,
  ).first()
  if row is None:
    raise HTTPException(status_code=404, detail="File not found.")

  recorded_path = Path(row.path)
  if recorded_path.is_absolute() or ".." in recorded_path.parts:
    raise HTTPException(status_code=400, detail="Invalid path.")

  data_dir = get_settings().data_dir
  try:
    # Hold the verified inode for the whole response. FileResponse reopens
    # this descriptor through procfs, so a later path/symlink swap cannot
    # change which bytes are served while retaining range-request support.
    file_fd, file_stat = generated_files.open_stored_file(
      data_dir, chat_id, str(recorded_path),
    )
  except OSError:
    raise HTTPException(status_code=404, detail="File not found.")

  inline = preview and generated_files.previewable_mime_type(row.mime_type)
  try:
    return _AnchoredFileResponse(
      file_fd,
      media_type=row.mime_type,
      filename=row.name,
      content_disposition_type="inline" if inline else "attachment",
      headers={"X-Content-Type-Options": "nosniff"},
      stat_result=file_stat,
    )
  except Exception:
    os.close(file_fd)
    raise
