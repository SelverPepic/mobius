"""The local MCP broker cannot outlive a shared-browser grant."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import connectors, models
from app.browser_access import create_invitation, revoke_grant
from app.routes import connectors as routes


def test_bound_capability_rechecks_owner_and_browser_grant(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'broker.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = create_invitation(db, owner, 'recipient')
    cap = connectors.mint_broker_capability(
      7, 'x' * 64, owner_id=owner.id, owner_epoch=owner.token_epoch,
      browser_grant_id=grant.id, browser_grant_epoch=grant.epoch,
    )
    claims = connectors.verify_broker_capability(cap, 7, 'x' * 64, db=db)
    assert claims['browser_grant_id'] == grant.id
    with pytest.raises(connectors.ConnectorError):
      connectors.verify_broker_capability(cap, 7, 'x' * 64)
    revoke_grant(db, grant.id, owner.id)
    with pytest.raises(connectors.ConnectorError):
      connectors.verify_broker_capability(cap, 7, 'x' * 64, db=db)

    # Pre-upgrade owner capabilities remain accepted for their remaining TTL.
    legacy = connectors.mint_broker_capability(7, 'x' * 64)
    connectors.verify_broker_capability(legacy, 7, 'x' * 64, db=db)


def test_owner_epoch_rotates_new_broker_capabilities(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'owner.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    cap = connectors.mint_broker_capability(
      7, 'x' * 64, owner_id=owner.id, owner_epoch=owner.token_epoch,
    )
    connectors.verify_broker_capability(cap, 7, 'x' * 64, db=db)
    owner.token_epoch += 1
    db.commit()
    with pytest.raises(connectors.ConnectorError):
      connectors.verify_broker_capability(cap, 7, 'x' * 64, db=db)


@pytest.mark.asyncio
async def test_broker_stream_closes_after_grant_revoke_without_remote_io(monkeypatch):
  active = True
  closed = []

  class Resource:
    headers = {}
    status_code = 200

    async def aclose(self):
      closed.append(self)

  client, upstream = Resource(), Resource()
  snapshot = routes._BrokerSnapshot(
    url='https://unused.example/mcp', auth_header='Authorization', secret='unused',
    generation='x' * 64, lineage={'browser_grant_id': 'guest'},
  )
  monkeypatch.setattr(routes, '_require_loopback', lambda request: 'cap')
  monkeypatch.setattr(routes, '_snapshot_broker_row',
                      lambda db, connector_id, capability: snapshot)
  monkeypatch.setattr(routes, '_broker_lineage_active',
                      lambda connector_id, snapshot: active)

  async def fake_open(request, snapshot):
    return client, upstream

  async def chunks(upstream, snapshot):
    yield b'first'
    yield b'second'

  monkeypatch.setattr(routes, '_open_broker_upstream', fake_open)
  monkeypatch.setattr(routes, '_redacted_broker_stream', chunks)
  db = type('Db', (), {'close': lambda self: None})()
  request = Request({'type': 'http', 'method': 'GET', 'path': '/',
                     'headers': [], 'client': ('127.0.0.1', 1234)})
  response = await routes.broker_connector(7, request, db)
  body = response.body_iterator
  assert await anext(body) == b'first'
  active = False
  with pytest.raises(StopAsyncIteration):
    await anext(body)
  assert client in closed and upstream in closed


def test_turn_plan_issues_bound_broker_token_from_run_session(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'plan.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = create_invitation(db, owner, 'recipient')
    connector = models.Connector(
      slug='docs', name='Docs', url='https://docs.example/mcp',
      enabled=True, status='ok', tools_json=[], est_tokens=0,
    )
    db.add(connector)
    db.commit()
    plan = connectors.build_turn_plan(
      db, include_owner_connectors=True,
      owner_id=owner.id, owner_epoch=owner.token_epoch,
      browser_grant_id=grant.id, browser_grant_epoch=grant.epoch,
    )
    assert plan is not None
    server = next(iter(plan.claude_servers.values()))
    token = server['headers']['Authorization'].removeprefix('Bearer ')
    claims = connectors.verify_broker_capability(
      token, connector.id, connector.capability_id, db=db,
    )
    assert claims['owner_id'] == owner.id
    assert claims['browser_grant_id'] == grant.id
    revoke_grant(db, grant.id, owner.id)
    with pytest.raises(connectors.ConnectorError):
      connectors.verify_broker_capability(
        token, connector.id, connector.capability_id, db=db,
      )


@pytest.mark.asyncio
async def test_broker_upload_stops_forwarding_after_revoke(monkeypatch):
  active = True
  snapshot = routes._BrokerSnapshot(
    url='https://unused.example/mcp', auth_header='Authorization', secret='unused',
    connector_id=7, generation='x' * 64,
    lineage={'browser_grant_id': 'guest'},
  )
  monkeypatch.setattr(routes, '_broker_lineage_active',
                      lambda connector_id, snapshot: active)

  class Upload:
    async def stream(self):
      yield b'first'
      yield b'second'

  iterator = routes._revocable_broker_upload(Upload(), 7, snapshot)
  assert await anext(iterator) == b'first'
  active = False
  from fastapi import HTTPException
  with pytest.raises(HTTPException) as error:
    await anext(iterator)
  assert error.value.status_code == 401


def test_open_stream_lineage_recheck_sees_fresh_revocation(tmp_path, monkeypatch):
  from sqlalchemy.orm import sessionmaker

  eng = create_engine(f"sqlite:///{tmp_path / 'stream-revoke.db'}")
  models.Base.metadata.create_all(eng)
  factory = sessionmaker(bind=eng)
  monkeypatch.setattr(routes, 'SessionLocal', factory)
  with factory() as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = create_invitation(db, owner, 'recipient')
    connector = models.Connector(
      slug='docs', name='Docs', url='https://docs.example/mcp',
      enabled=True, status='ok', tools_json=[], est_tokens=0,
    )
    db.add(connector)
    db.commit()
    snapshot = routes._BrokerSnapshot(
      url=connector.url, auth_header=None, secret=None,
      connector_id=connector.id, generation=connector.capability_id,
      lineage={'owner_id': owner.id, 'owner_epoch': owner.token_epoch,
               'browser_grant_id': grant.id, 'browser_grant_epoch': grant.epoch},
    )
    assert routes._broker_lineage_active(connector.id, snapshot)
    revoke_grant(db, grant.id, owner.id)
    assert not routes._broker_lineage_active(connector.id, snapshot)
