import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import app.schema_migrations as migrations
from app import models
from app.config import get_settings
from app.schema_migrations import _move_chat_media_out_of_generated


def _chat_root(chat_id: str) -> Path:
  return Path(get_settings().data_dir) / "chats" / chat_id


def test_moves_files_and_rewrites_urls(db, chat):
  old_url = f"/api/chats/{chat.id}/generated/old.png"
  new_url = f"/api/chats/{chat.id}/media/old.png"
  chat.messages = [{"role": "assistant", "content": f"![image]({old_url})"}]
  chat.pending_messages = [{"content": {"preview": old_url}}]
  db.commit()

  old_dir = _chat_root(chat.id) / "generated"
  old_dir.mkdir(parents=True)
  (old_dir / "old.png").write_bytes(b"old-image")

  _move_chat_media_out_of_generated(db.get_bind())
  db.refresh(chat)

  assert not old_dir.exists()
  assert (_chat_root(chat.id) / "media" / "old.png").read_bytes() == b"old-image"
  assert chat.messages[0]["content"] == f"![image]({new_url})"
  assert chat.pending_messages[0]["content"]["preview"] == new_url


def test_leaves_links_to_other_chats_untouched(db, chat):
  foreign = "/api/chats/someone-else/generated/example.png"
  chat.messages = [{"role": "assistant", "content": f"Discussing {foreign}"}]
  db.commit()

  _move_chat_media_out_of_generated(db.get_bind())
  db.refresh(chat)

  assert chat.messages[0]["content"] == f"Discussing {foreign}"
  assert not (_chat_root(chat.id) / "media").exists()


def test_rewrites_legacy_url_without_old_directory(db, chat):
  old_url = f"/api/chats/{chat.id}/generated/already-moved.png"
  new_url = f"/api/chats/{chat.id}/media/already-moved.png"
  chat.messages = [{"role": "assistant", "content": old_url}]
  db.commit()

  _move_chat_media_out_of_generated(db.get_bind())
  db.refresh(chat)

  assert chat.messages[0]["content"] == new_url


def test_retry_after_interrupted_cleanup_finishes_the_move(db, chat):
  """A crash after the link commit leaves both copies; a retry settles it."""
  new_url = f"/api/chats/{chat.id}/media/old.png"
  chat.messages = [{"role": "assistant", "content": new_url}]
  db.commit()
  for name in ("generated", "media"):
    directory = _chat_root(chat.id) / name
    directory.mkdir(parents=True)
    (directory / "old.png").write_bytes(b"old-image")

  _move_chat_media_out_of_generated(db.get_bind())
  db.refresh(chat)

  assert not (_chat_root(chat.id) / "generated").exists()
  assert (_chat_root(chat.id) / "media" / "old.png").read_bytes() == b"old-image"
  assert chat.messages[0]["content"] == new_url


def test_conflicting_file_fails_before_any_change(db, chat):
  old_url = f"/api/chats/{chat.id}/generated/same.png"
  chat.messages = [{"role": "assistant", "content": old_url}]
  db.commit()

  old_dir = _chat_root(chat.id) / "generated"
  media_dir = _chat_root(chat.id) / "media"
  old_dir.mkdir(parents=True)
  media_dir.mkdir(parents=True)
  (old_dir / "same.png").write_bytes(b"old")
  (media_dir / "same.png").write_bytes(b"different")

  with pytest.raises(RuntimeError, match="Conflicting chat media file"):
    _move_chat_media_out_of_generated(db.get_bind())

  db.refresh(chat)
  assert chat.messages[0]["content"] == old_url
  assert (old_dir / "same.png").read_bytes() == b"old"
  assert (media_dir / "same.png").read_bytes() == b"different"


def test_upgrade_runs_the_move_once_and_later_boots_skip_the_scan(
  tmp_path, monkeypatch,
):
  """An instance that never ran the move is fixed once, then never rescanned."""
  data_dir = tmp_path / "data"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
  models.Base.metadata.create_all(eng)
  migrations._ensure_migration_ledger(eng)
  for version, _migration in migrations._SCHEMA_MIGRATIONS:
    if version != "0083_chat_media_directory":
      migrations._record_migration(eng, version)

  chat_id = str(uuid.uuid4())
  old_url = f"/api/chats/{chat_id}/generated/old.png"
  with Session(eng) as session:
    session.add(models.Chat(
      id=chat_id,
      title="Legacy",
      messages=[{"role": "assistant", "content": old_url}],
    ))
    session.commit()
  old_dir = data_dir / "chats" / chat_id / "generated"
  old_dir.mkdir(parents=True)
  (old_dir / "old.png").write_bytes(b"old-image")

  migrations.run_migrations(eng)

  def stored_content() -> str:
    with Session(eng) as session:
      return session.get(models.Chat, chat_id).messages[0]["content"]

  assert stored_content() == f"/api/chats/{chat_id}/media/old.png"
  assert (data_dir / "chats" / chat_id / "media" / "old.png").exists()
  assert not old_dir.exists()
  assert "0083_chat_media_directory" in {
    row["version"] for row in migrations.schema_migration_history(eng)
  }

  # A legacy link that appears after completion stays as written: the
  # recorded migration is not replayed, so no boot scans transcripts again.
  with eng.begin() as conn:
    conn.execute(text(
      "UPDATE chats SET messages = :messages WHERE id = :chat_id"
    ), {"messages": f'[{{"content": "{old_url}"}}]', "chat_id": chat_id})
  migrations.run_migrations(eng)
  assert stored_content() == old_url
