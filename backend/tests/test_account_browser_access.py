"""Account sign-in is a separate, browser-bound and revocable grant kind."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException

from app import account_browser_access as account, browser_access as access, models
from app.config import get_settings


def _grant(db, monkeypatch):
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  owner = models.Owner(username="owner", hashed_password="unused")
  db.add(owner)
  db.commit()
  db.add(models.IdentityAccountLink(owner_id=owner.id,
    access_token_encrypted="fixture-token-ciphertext", scopes_json=["identity:read", "identity:write"]))
  db.commit()
  grant = access.BrowserAccessGrant(
    id="g" * 32, owner_id=owner.id, label="Owner's instance", kind="account",
    issuer=account.issuer_origin(), subject="user_123", recipient_handle="alice",
    origin="https://shared.example", remote_status="active", epoch=0,
    grantor_binding=access.account_binding(db, owner.id),
  )
  db.add(grant)
  db.commit()
  return owner, grant


def _denied(fn):
  with pytest.raises(HTTPException) as error:
    fn()
  assert error.value.status_code == 401


def test_account_pending_binds_cookie_state_epoch_and_replay(db, monkeypatch):
  owner, grant = _grant(db, monkeypatch)
  cookie, url = account.start(db, grant.id)
  query = parse_qs(urlsplit(url).query)
  state = query["state"][0]
  pending, found = account.pending_for_callback(db, state, cookie)
  assert found.id == grant.id
  assert query["nonce"] == [pending.nonce]
  assert query["origin"] == [grant.origin]
  _denied(lambda: account.pending_for_callback(db, state, "wrong" * 10))
  _denied(lambda: account.pending_for_callback(db, "wrong" * 10, cookie))
  account.mark_verified(db, pending.id,
    (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
  secret, session, _, _ = account.complete(db, pending.id)
  assert session.grant_id == grant.id
  assert access.renew_session(db, secret)[0].id == grant.id
  _denied(lambda: account.complete(db, pending.id))
  access.revoke_grant(db, grant.id, owner.id)
  _denied(lambda: access.renew_session(db, secret))
  _denied(lambda: account.start(db, grant.id))


def test_account_pending_rechecks_owner_epoch_and_never_reissues_invitation(db, monkeypatch):
  owner, grant = _grant(db, monkeypatch)
  cookie, url = account.start(db, grant.id)
  state = parse_qs(urlsplit(url).query)["state"][0]
  _denied(lambda: access.reissue_invitation(db, owner, grant.id))
  owner.token_epoch += 1
  db.commit()
  _denied(lambda: account.pending_for_callback(db, state, cookie))


@pytest.mark.asyncio
async def test_proof_requires_exact_issuer_subject_origin_nonce_and_audience(db, monkeypatch):
  _, grant = _grant(db, monkeypatch)
  cookie, url = account.start(db, grant.id)
  pending, grant = account.pending_for_callback(db, parse_qs(urlsplit(url).query)["state"][0], cookie)
  proof = {"iss": grant.issuer, "sub": grant.subject, "aud": "mobius-shared-browser",
           "origin": grant.origin, "grant_id": grant.id, "nonce": pending.nonce,
           "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}
  class Client:
    def __init__(self, *args, **kwargs):
      assert kwargs["follow_redirects"] is False
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def post(self, url, **kwargs):
      assert url == grant.issuer + "/shared-access/token"
      assert kwargs["json"]["grant_id"] == grant.id
      return httpx.Response(200, json=proof)
  monkeypatch.setattr(account.httpx, "AsyncClient", Client)
  assert await account.exchange_code(pending, grant, "c" * 32) == proof
  for key, wrong in (("iss", "https://evil.example"), ("sub", "other"),
                     ("aud", "owner"), ("origin", "https://evil.example"),
                     ("grant_id", "other"), ("nonce", "other")):
    expected = proof[key]
    proof[key] = wrong
    with pytest.raises(HTTPException) as error:
      await account.exchange_code(pending, grant, "c" * 32)
    assert error.value.status_code == 401
    proof[key] = expected


def test_account_route_registration_retry_and_revocation_cleanup(client, auth, db, monkeypatch):
  from app.routes import browser_access as routes
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  client.headers["Origin"] = "https://shared.example"
  owner = db.query(models.Owner).one()
  db.add(models.IdentityAccountLink(owner_id=owner.id,
    access_token_encrypted="fixture-token-ciphertext", scopes_json=["identity:read", "identity:write"]))
  db.commit()
  ids = []
  calls = 0
  async def remote(db, owner_id, method, suffix, payload=None):
    nonlocal calls
    calls += 1
    if method == "POST":
      ids.append(payload["grant_id"])
      if calls == 1:
        raise HTTPException(502, "Lost response")
      return httpx.Response(200, json={"issuer": account.issuer_origin(),
        "subject": "user_alice", "handle": "alice", "grant_id": payload["grant_id"],
        "origin": "https://shared.example", "open_url": "https://shared.example/ignored"})
    if method == "DELETE":
      return httpx.Response(503)
    return httpx.Response(200, json={"instances": []})
  monkeypatch.setattr(routes, "_issuer_request", remote)
  body = {"recipient_handle": "@Alice", "instance_name": "Test instance"}
  assert client.post("/api/connect/browser-access/accounts", json=body, headers=auth).status_code == 502
  reply = client.post("/api/connect/browser-access/accounts", json=body, headers=auth)
  assert reply.status_code == 200, reply.text
  grant_id = reply.json()["grant"]["id"]
  assert ids == [grant_id, grant_id]
  repeated = client.post("/api/connect/browser-access/accounts", json=body, headers=auth)
  assert repeated.status_code == 200
  assert repeated.json()["grant"]["id"] == grant_id
  assert ids == [grant_id, grant_id]
  assert reply.json()["grant"]["kind"] == "account"
  assert client.post(f"/api/connect/browser-access/{grant_id}/invitation", headers=auth).status_code == 401
  assert client.get("/api/connect/browser-access/shared", headers=auth).json() == {"instances": []}
  response = client.delete(f"/api/connect/browser-access/{grant_id}", headers=auth)
  assert response.status_code == 202
  assert response.json()["directory_cleanup_pending"] is True
  assert db.get(access.BrowserAccessGrant, grant_id).revoked_at is not None
  assert client.get("/api/connect/browser-access", headers=auth).json()["grants"][0]["directory_cleanup_pending"] is True


def test_callback_requires_same_origin_finalize_and_redirects_without_code(client, db, monkeypatch):
  from app.routes import browser_access as routes
  owner, grant = _grant(db, monkeypatch)
  _, old_invite = access.create_invitation(db, owner, "old browser")
  old_secret, _, _, _ = access.redeem_invitation(db, old_invite)
  client.cookies.set("mobius_shared_browser", old_secret,
    path="/api/connect/browser-access/session")
  client.base_url = "https://shared.example"
  cookie, url = account.start(db, grant.id)
  state = parse_qs(urlsplit(url).query)["state"][0]
  client.cookies.set("mobius_shared_account_pending", cookie, path="/api/connect/browser-access/session/account")
  async def verified(pending, found, code):
    assert code == "c" * 32
    assert found.id == grant.id
    return {"expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}
  monkeypatch.setattr(account, "exchange_code", verified)
  response = client.get("/api/connect/browser-access/session/account/callback", params={
    "code": "c" * 32, "state": state,
  }, follow_redirects=False)
  assert response.status_code == 303
  assert response.headers["location"].startswith("/shell/shared#account-finalize=")
  assert "code" not in response.headers["location"]
  pending_id = response.headers["location"].split("=", 1)[1]
  denied = client.post("/api/connect/browser-access/session/account/finalize", json={
    "pending_id": pending_id,
  }, headers={"Origin": "https://evil.example"})
  assert denied.status_code == 403
  finalized = client.post("/api/connect/browser-access/session/account/finalize", json={
    "pending_id": pending_id,
  }, headers={"Origin": "https://shared.example"})
  assert finalized.status_code == 200, finalized.text
  assert any("mobius_shared_browser=" in value for value in finalized.headers.get_list("set-cookie"))
  _denied(lambda: access.renew_session(db, old_secret))
  assert client.get("/api/connect/browser-access/session/account/callback", params={
    "code": "c" * 32, "state": state,
  }, follow_redirects=False).status_code == 401


def test_shared_directory_rebuilds_link_and_rejects_unsafe_origin(client, auth, db, monkeypatch):
  from app.routes import browser_access as routes
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  client.headers["Origin"] = "https://shared.example"
  owner = db.query(models.Owner).one()
  db.add(models.IdentityAccountLink(owner_id=owner.id,
    access_token_encrypted="fixture-token-ciphertext", scopes_json=["identity:read", "identity:write"]))
  db.commit()
  row = {"grant_id": "g" * 32, "name": "<b>not HTML</b>",
         "origin": "https://other.example", "owner_handle": "owner",
         "open_url": "https://evil.example/steal", "status": "invited", "unread": True}
  async def remote(*args, **kwargs):
    return httpx.Response(200, json={"instances": [row]})
  monkeypatch.setattr(routes, "_issuer_request", remote)
  response = client.get("/api/connect/browser-access/shared", headers=auth)
  assert response.status_code == 200
  instance = response.json()["instances"][0]
  assert instance["name"] == "<b>not HTML</b>"
  assert instance["open_url"] == "https://other.example/api/connect/browser-access/session/account/start?grant_id=" + "g" * 32
  row["origin"] = "https://other.example@evil.example"
  assert client.get("/api/connect/browser-access/shared", headers=auth).status_code == 502


def test_link_generation_change_invalidates_existing_account_session(db, monkeypatch):
  owner, grant = _grant(db, monkeypatch)
  cookie, url = account.start(db, grant.id)
  state = parse_qs(urlsplit(url).query)["state"][0]
  pending, _ = account.pending_for_callback(db, state, cookie)
  account.mark_verified(db, pending.id,
    (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
  secret, session, _, _ = account.complete(db, pending.id)
  assert access.validate_session(db, session.id, grant.id, owner.id).id == session.id
  db.delete(db.get(models.IdentityAccountLink, owner.id))
  db.commit()
  _denied(lambda: access.validate_session(db, session.id, grant.id, owner.id))
  _denied(lambda: access.renew_session(db, secret))
  _denied(lambda: account.start(db, grant.id))


def test_explicit_identity_unlink_revokes_account_grants_before_link_removal(client, auth, db, monkeypatch):
  from app.routes import browser_access as routes
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  client.headers["Origin"] = "https://shared.example"
  owner = db.query(models.Owner).one()
  db.add(models.IdentityAccountLink(owner_id=owner.id,
    access_token_encrypted="fixture-token-ciphertext", scopes_json=["identity:read", "identity:write"]))
  db.commit()
  grant = access.BrowserAccessGrant(
    id="u" * 32, owner_id=owner.id, label="Test", kind="account",
    issuer=account.issuer_origin(), subject="user_alice", recipient_handle="alice",
    origin="https://shared.example", remote_status="active", epoch=0,
    grantor_binding=access.account_binding(db, owner.id),
  )
  db.add(grant)
  db.commit()
  order = []
  async def remote(db, owner_id, method, suffix, payload=None):
    assert method == "DELETE"
    assert db.get(access.BrowserAccessGrant, grant.id).revoked_at is not None
    assert db.get(models.IdentityAccountLink, owner.id) is not None
    order.append("grant-delete")
    return httpx.Response(204)
  monkeypatch.setattr(routes, "_issuer_request", remote)
  import app.routes.identity as identity
  monkeypatch.setattr(identity, "_open", lambda value: "fake-token")
  original = httpx.AsyncClient
  def handle(request):
    assert str(request.url).endswith("/api/account-links/revoke")
    assert order == ["grant-delete"]
    return httpx.Response(204)
  monkeypatch.setattr(identity.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
  response = client.delete("/api/identity/link", headers=auth)
  assert response.status_code == 204, response.text
  assert db.get(access.BrowserAccessGrant, grant.id).revoked_at is not None
  assert db.get(models.IdentityAccountLink, owner.id) is None


def test_finalization_expires_with_proof_not_ten_minute_pending(db, monkeypatch):
  _, grant = _grant(db, monkeypatch)
  cookie, url = account.start(db, grant.id)
  state = parse_qs(urlsplit(url).query)["state"][0]
  pending, _ = account.pending_for_callback(db, state, cookie)
  account.mark_verified(db, pending.id,
    (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat())
  pending.verified_expires_at = account.now_naive_utc() - timedelta(seconds=1)
  db.commit()
  _denied(lambda: account.verified_for_cookie(db, cookie))
  _denied(lambda: account.complete(db, pending.id))


def test_unlink_failure_still_closes_every_local_account_grant(client, auth, db, monkeypatch):
  from app.routes import browser_access as routes
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  client.headers["Origin"] = "https://shared.example"
  owner = db.query(models.Owner).one()
  db.add(models.IdentityAccountLink(owner_id=owner.id,
    access_token_encrypted="fixture-token-ciphertext", scopes_json=[]))
  db.commit()
  binding = access.account_binding(db, owner.id)
  for grant_id in ("a" * 32, "b" * 32):
    db.add(access.BrowserAccessGrant(
      id=grant_id, owner_id=owner.id, label="Shared", kind="account",
      issuer=account.issuer_origin(), subject="user_alice", recipient_handle="alice",
      origin="https://shared.example", remote_status="active", epoch=0,
      grantor_binding=binding,
    ))
  db.commit()
  attempted = []
  async def remote(db, owner_id, method, suffix, payload=None):
    attempted.append(suffix)
    return httpx.Response(503)
  monkeypatch.setattr(routes, "_issuer_request", remote)
  response = client.delete("/api/identity/link", headers=auth)
  assert response.status_code == 502
  grants = db.query(access.BrowserAccessGrant).filter_by(kind="account").all()
  assert len(grants) == 2 and all(g.revoked_at is not None for g in grants)
  assert all(g.remote_status == "cleanup_pending" for g in grants)
  assert len(attempted) == 2
  assert db.get(models.IdentityAccountLink, owner.id) is not None


def test_registration_response_cannot_reactivate_locally_revoked_grant(client, auth, db, monkeypatch):
  from app.routes import browser_access as routes
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  client.headers["Origin"] = "https://shared.example"
  owner = db.query(models.Owner).one()
  db.add(models.IdentityAccountLink(owner_id=owner.id,
    access_token_encrypted="fixture-token-ciphertext", scopes_json=[]))
  db.commit()
  calls = []
  async def remote(db, owner_id, method, suffix, payload=None):
    calls.append(method)
    if method == "POST":
      access.revoke_grant(db, payload["grant_id"], owner_id)
      return httpx.Response(200, json={"issuer": account.issuer_origin(),
        "subject": "user_alice", "handle": "alice", "grant_id": payload["grant_id"],
        "origin": "https://shared.example"})
    return httpx.Response(204)
  monkeypatch.setattr(routes, "_issuer_request", remote)
  response = client.post("/api/connect/browser-access/accounts", headers=auth,
    json={"recipient_handle": "alice"})
  assert response.status_code == 409
  assert calls == ["POST", "DELETE"]
  grant = db.query(access.BrowserAccessGrant).filter_by(kind="account").one()
  assert grant.revoked_at is not None and grant.remote_status == "revoked"
  _denied(lambda: account.start(db, grant.id))


def _signed_account_session(db, grant):
  cookie, url = account.start(db, grant.id)
  state = parse_qs(urlsplit(url).query)["state"][0]
  pending, _ = account.pending_for_callback(db, state, cookie)
  account.mark_verified(db, pending.id,
    (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
  secret, session, _, _ = account.complete(db, pending.id)
  return secret, session


def test_managed_binding_pins_instance_id_and_issuer_for_jwt_and_refresh(db, monkeypatch):
  settings = get_settings()
  monkeypatch.setattr(settings, "frontend_origin", "https://managed.example")
  monkeypatch.setattr(settings, "mobius_sso_instance_id", "mob_original")
  monkeypatch.setattr(settings, "mobius_sso_issuer", "https://identity.example")
  owner = models.Owner(username="managed-owner", hashed_password="unused",
    sso_subject="user_owner", auth_mode="mobius")
  db.add(owner)
  db.commit()
  grant = access.BrowserAccessGrant(
    id="m" * 32, owner_id=owner.id, label="Managed", kind="account",
    issuer="https://identity.example", subject="user_guest",
    recipient_handle="guest", origin="https://managed.example",
    remote_status="active", epoch=0,
    grantor_binding=access.account_binding(db, owner.id),
  )
  db.add(grant)
  db.commit()
  secret, session = _signed_account_session(db, grant)
  assert access.validate_grant(db, grant.id, grant.epoch, owner.id).id == grant.id
  assert access.validate_session(db, session.id, grant.id, owner.id).id == session.id
  for setting, changed in (
    ("mobius_sso_instance_id", "mob_replacement"),
    ("mobius_sso_issuer", "https://replacement-identity.example"),
  ):
    original = getattr(settings, setting)
    monkeypatch.setattr(settings, setting, changed)
    _denied(lambda: access.validate_grant(db, grant.id, grant.epoch, owner.id))
    _denied(lambda: access.validate_session(db, session.id, grant.id, owner.id))
    _denied(lambda: access.renew_session(db, secret))
    monkeypatch.setattr(settings, setting, original)
    assert access.validate_session(db, session.id, grant.id, owner.id).id == session.id


def test_changed_runtime_origin_invalidates_existing_account_jwt_and_refresh(db, monkeypatch):
  owner, grant = _grant(db, monkeypatch)
  secret, session = _signed_account_session(db, grant)
  settings = get_settings()
  for setting, changed in (
    ("frontend_origin", "https://replacement.example"),
    ("mobius_account_origin", "https://replacement-identity.example"),
  ):
    original = getattr(settings, setting)
    monkeypatch.setattr(settings, setting, changed)
    _denied(lambda: access.validate_grant(db, grant.id, grant.epoch, owner.id))
    _denied(lambda: access.validate_session(db, session.id, grant.id, owner.id))
    _denied(lambda: access.renew_session(db, secret))
    _denied(lambda: account.start(db, grant.id))
    monkeypatch.setattr(settings, setting, original)
    assert access.validate_session(db, session.id, grant.id, owner.id).id == session.id


def test_shared_inbox_response_is_recipient_proxy_not_session_creation(client, auth, db, monkeypatch):
  from app.routes import browser_access as routes
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  headers = {**auth, "Origin": "https://shared.example"}
  row = {"origin": "https://other.example", "grant_id": "g" * 32,
         "name": "Other", "owner_handle": "owner", "status": "invited", "unread": False}
  calls = []
  async def remote(db, owner_id, method, suffix, payload=None):
    calls.append((owner_id, method, suffix, payload))
    row["status"] = "accepted" if payload["action"] == "accept" else row["status"]
    return httpx.Response(200, json={"instance": row})
  monkeypatch.setattr(routes, "_issuer_request", remote)
  before = db.query(access.BrowserAccessSession).count()
  for action, expected in (("later", "invited"), ("accept", "accepted"), ("accept", "accepted")):
    response = client.post("/api/connect/browser-access/shared/respond", headers=headers,
      json={"origin": row["origin"], "grant_id": row["grant_id"], "action": action})
    assert response.status_code == 200, response.text
    assert response.json()["instance"]["status"] == expected
    assert response.json()["instance"]["unread"] is False
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
  assert db.query(access.BrowserAccessSession).count() == before
  assert all(call[:3] == (db.query(models.Owner).one().id, "POST", "/shared/respond") for call in calls)
  assert client.post("/api/connect/browser-access/shared/respond", headers={**auth, "Origin": "https://evil.example"},
    json={"origin": row["origin"], "grant_id": row["grant_id"], "action": "accept"}).status_code == 403
  assert len(calls) == 3


@pytest.mark.parametrize("changes", [
  {"origin": "http://other.example"}, {"origin": "https://other.example/path"},
  {"origin": "https://other.example:invalid"}, {"action": "delete"},
  {"grant_id": "../other"}, {"subject": "different-person"},
])
def test_shared_inbox_rejects_bad_input_before_issuer(client, auth, monkeypatch, changes):
  from app.routes import browser_access as routes
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  async def remote(*args, **kwargs):
    pytest.fail("Invalid response must never reach the issuer")
  monkeypatch.setattr(routes, "_issuer_request", remote)
  body = {"origin": "https://other.example", "grant_id": "g" * 32, "action": "accept", **changes}
  response = client.post("/api/connect/browser-access/shared/respond", json=body,
    headers={**auth, "Origin": "https://shared.example"})
  assert response.status_code == 422, response.text


@pytest.mark.parametrize("changes", [
  {"origin": "https://different.example"}, {"grant_id": "x" * 32},
  {"status": "invited"}, {"unread": True}, {"unread": "false"},
])
def test_shared_inbox_rejects_mismatched_issuer_receipt(client, auth, monkeypatch, changes):
  from app.routes import browser_access as routes
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  async def remote(*args, **kwargs):
    return httpx.Response(200, json={"instance": {
      "origin": "https://other.example", "grant_id": "g" * 32, "name": "Other",
      "owner_handle": "owner", "status": "accepted", "unread": False, **changes}})
  monkeypatch.setattr(routes, "_issuer_request", remote)
  response = client.post("/api/connect/browser-access/shared/respond", headers={**auth, "Origin": "https://shared.example"},
    json={"origin": "https://other.example", "grant_id": "g" * 32, "action": "accept"})
  assert response.status_code == 502, response.text
