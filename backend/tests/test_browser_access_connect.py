"""A recipient's Connect commands retain grant lineage across reconnects."""

import asyncio

import pytest
from fastapi import HTTPException

from app import connect_runner
from app import auth
from app.browser_access import BrowserLineage, create_invitation, revoke_grant
from app.deps import Principal, get_principal, require_installation_owner_control
from app.models import Owner
from app.routes import connect


@pytest.fixture(autouse=True)
def _clear_connect_state():
  connect._channels.clear()
  connect._commands.clear()
  yield
  connect._channels.clear()
  connect._commands.clear()


def _owner(db):
  owner = Owner(username="browser-connect-owner", hashed_password="unused")
  db.add(owner)
  db.commit()
  return owner


def _host():
  host_id = connect._new_id()
  connect._save_host({
    "id": host_id,
    "name": "Box",
    "runner_protocol": connect_runner.RUNNER_PROTOCOL_VERSION,
    "runner_transport": "sse",
    "runner_capabilities": ["parallel"],
    "token_sha256": "paired",
    "active_commands": [],
  })
  channel = connect._Channel()
  connect._channels[host_id] = channel
  return host_id, channel


async def _start(host_id, channel, request_id, principal):
  task = asyncio.create_task(connect.exec_on_host(
    host_id, connect.ExecBody(cmd="printf safe", request_id=request_id, stream=True),
    _owner=principal.owner, _principal=principal,
  ))
  event = await asyncio.wait_for(channel.queue.get(), 1)
  assert event["type"] == "exec"
  connect._mark_command_started(host_id, request_id)
  await asyncio.wait_for(task, 1)
  return connect._find_command(host_id, request_id)


@pytest.mark.asyncio
async def test_revoke_cancels_only_recipient_commands_not_owner_commands(db):
  owner = _owner(db)
  grant, _ = create_invitation(db, owner, "laptop")
  guest = Principal(owner=owner, app_id=None, browser=BrowserLineage(grant.id))
  own = Principal(owner=owner, app_id=None)
  host_id, channel = _host()
  guest_command = await _start(host_id, channel, "a" * 16, guest)
  owner_command = await _start(host_id, channel, "b" * 16, own)
  assert guest_command.record()["browser_grant_id"] == grant.id
  assert owner_command.browser_grant_id is None
  revoke_grant(db, grant.id, owner.id)
  unfinished = connect.cancel_browser_grant_commands(grant.id)
  assert unfinished == [{
    "host_id": host_id, "request_id": "a" * 16,
    "state": "canceling", "remote_confirmed": False,
  }]
  assert guest_command.state == "canceling"
  assert owner_command.state == "running"
  assert (await channel.queue.get())["request_id"] == "a" * 16
  assert host_id in connect._channels
  # Admission trusts the request principal, which no revoked bearer can obtain.
  bearer = auth.create_access_token(
    {"sub": owner.username}, token_epoch=owner.token_epoch, browser=guest.browser,
  )
  with pytest.raises(HTTPException) as denied:
    get_principal(bearer, db)
  assert denied.value.status_code == 401


@pytest.mark.asyncio
async def test_cancel_pending_survives_restart_and_reconnect_without_replay(db):
  owner = _owner(db)
  grant, _ = create_invitation(db, owner, "laptop")
  host_id, _ = _host()
  # A record persisted before the grant epoch retired stays readable.
  command = connect._ActiveCommand.from_record({
    "id": "a" * 16, "timeout": 60, "cmd": "printf safe", "state": "running",
    "started_at": 1.0, "browser_grant_id": grant.id, "browser_grant_epoch": 0,
    "browser_owner_id": owner.id, "browser_owner_token_epoch": owner.token_epoch,
  })
  assert command.browser_grant_id == grant.id
  assert set(command.record()) & {
    "browser_grant_epoch", "browser_owner_id", "browser_owner_token_epoch",
  } == set()
  connect._host_commands(host_id)[command.request_id] = command
  connect._persist_commands(host_id)
  revoke_grant(db, grant.id, owner.id)
  connect.cancel_browser_grant_commands(grant.id)
  connect._commands.clear()
  replacement = connect._Channel()
  connect._channels[host_id] = replacement
  await connect._reconcile_runner(host_id, replacement, {
    "active_request_ids": [command.request_id], "pending_result_ids": [],
  })
  assert (await replacement.queue.get()) == {
    "type": "cancel", "request_id": command.request_id,
  }
  assert replacement.queue.empty()
  assert connect._find_command(host_id, command.request_id).state == "canceling"


@pytest.mark.asyncio
async def test_revocation_before_startup_reconciliation_blocks_guest_replay(db):
  owner = _owner(db)
  grant, _ = create_invitation(db, owner, "laptop")
  host_id, _ = _host()
  guest_command = connect._ActiveCommand(
    "a" * 16, 60, cmd="printf guest", browser_grant_id=grant.id,
  )
  owner_command = connect._ActiveCommand("b" * 16, 60, cmd="printf owner")
  connect._host_commands(host_id).update({
    guest_command.request_id: guest_command,
    owner_command.request_id: owner_command,
  })
  connect._persist_commands(host_id)
  revoke_grant(db, grant.id, owner.id)
  connect._commands.clear()
  replacement = connect._Channel()
  await connect._reconcile_runner(host_id, replacement, {
    "active_request_ids": [], "pending_result_ids": [],
  })
  event = await replacement.queue.get()
  assert event["type"] == "exec" and event["request_id"] == owner_command.request_id
  assert replacement.queue.empty()
  assert connect._find_command(host_id, guest_command.request_id) is None


def test_guest_cannot_mint_installation_connect_authority(db):
  owner = _owner(db)
  grant, _ = create_invitation(db, owner, "laptop")
  guest = Principal(owner=owner, app_id=None, browser=BrowserLineage(grant.id))
  with pytest.raises(HTTPException) as denied:
    require_installation_owner_control(guest)
  assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_shared_finished_retry_never_reexecutes_or_adopts_another_grant(db):
  owner = _owner(db)
  first, _ = create_invitation(db, owner, "first")
  second, _ = create_invitation(db, owner, "second")
  a = Principal(owner=owner, app_id=None, browser=BrowserLineage(first.id))
  b = Principal(owner=owner, app_id=None, browser=BrowserLineage(second.id))
  host_id, channel = _host()
  rid = "f" * 16
  command = await _start(host_id, channel, rid, a)
  connect._runner_result(host_id, connect.ResultBody(request_id=rid, stdout="shared result"))
  revoke_grant(db, first.id, owner.id)
  reply = await connect.exec_on_host(
    host_id, connect.ExecBody(cmd="printf safe", request_id=rid, stream=True),
    _owner=owner, _principal=b,
  )
  assert reply["state"] == "finished"
  result = await connect.exec_on_host(
    host_id, connect.ExecBody(cmd="printf safe", request_id=rid),
    _owner=owner, _principal=b,
  )
  assert result["stdout"] == "shared result"
  assert command.browser_grant_id == first.id
  assert channel.queue.empty()
