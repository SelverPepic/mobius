"""Account-bound shared browser sign-in; issuer is discovery and proof, never local authority."""

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import browser_access as access
from app.config import get_settings
from app.timeutil import now_naive_utc

PENDING_TTL = timedelta(minutes=10)


def issuer_origin() -> str:
  settings = get_settings()
  return (settings.mobius_sso_issuer if settings.mobius_sso_enabled else settings.mobius_account_origin).rstrip('/')


def runtime_origin() -> str:
  from app.routes.browser_access import _origin
  return _origin()


def digest(value: str) -> str:
  return hashlib.sha256(value.encode()).hexdigest()


def _invalid() -> HTTPException:
  return HTTPException(401, "Account sign-in unavailable. Start again from the shared link.")


def start(db: Session, grant_id: str):
  grant = db.get(access.BrowserAccessGrant, grant_id)
  if (not grant or grant.kind != 'account' or grant.remote_status != 'active'
      or grant.revoked_at is not None or grant.issuer != issuer_origin()
      or grant.origin != runtime_origin() or not grant.subject):
    raise _invalid()
  access._check_account_binding(db, grant)
  owner = access._owner(db, grant.owner_id)
  state, cookie, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
  challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
  pending = access.BrowserAccountPending(
    id=secrets.token_urlsafe(24), grant_id=grant.id, state_hash=digest(state),
    cookie_hash=digest(cookie), verifier=verifier, nonce=nonce,
    grant_epoch=grant.epoch, owner_token_epoch=owner.token_epoch,
    issuer=grant.issuer, subject=grant.subject,
    expires_at=now_naive_utc() + PENDING_TTL,
  )
  db.query(access.BrowserAccountPending).filter(
    access.BrowserAccountPending.expires_at < now_naive_utc() - timedelta(days=1),
  ).delete(synchronize_session=False)
  db.add(pending)
  db.commit()
  url = issuer_origin() + '/shared-access/authorize?' + urlencode({
    'origin': grant.origin, 'grant_id': grant.id, 'state': state,
    'code_challenge': challenge, 'nonce': nonce,
  })
  return cookie, url


def pending_for_callback(db: Session, state: str, cookie: str):
  if not isinstance(state, str) or not 20 <= len(state) <= 256 or not isinstance(cookie, str) or not 20 <= len(cookie) <= 256:
    raise _invalid()
  pending = db.query(access.BrowserAccountPending).filter_by(state_hash=digest(state)).first()
  if (pending is None or pending.consumed_at is not None or pending.expires_at <= now_naive_utc()
      or not hmac.compare_digest(pending.cookie_hash, digest(cookie))):
    raise _invalid()
  grant = db.get(access.BrowserAccessGrant, pending.grant_id)
  if (not grant or grant.kind != 'account' or grant.remote_status != 'active'
      or grant.revoked_at is not None or grant.epoch != pending.grant_epoch
      or grant.issuer != issuer_origin() or grant.issuer != pending.issuer
      or grant.subject != pending.subject or grant.origin != runtime_origin()):
    raise _invalid()
  access._check_account_binding(db, grant)
  owner = access._owner(db, grant.owner_id)
  if owner.token_epoch != pending.owner_token_epoch:
    raise _invalid()
  return pending, grant


async def exchange_code(pending, grant, code: str) -> dict:
  if not isinstance(code, str) or not 20 <= len(code) <= 256:
    raise _invalid()
  try:
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
      response = await client.post(issuer_origin() + '/shared-access/token', json={
        'code': code, 'code_verifier': pending.verifier,
        'origin': grant.origin, 'grant_id': grant.id,
      }, headers={'Accept': 'application/json'})
  except httpx.HTTPError as exc:
    raise HTTPException(502, 'Account sign-in could not be verified. Try again.') from exc
  if response.status_code != 200:
    raise _invalid() if response.status_code in (400, 401, 403, 404, 409) else HTTPException(502, 'Account sign-in could not be verified. Try again.')
  try:
    proof = response.json()
  except ValueError as exc:
    raise HTTPException(502, 'Account sign-in returned invalid proof.') from exc
  if not isinstance(proof, dict):
    raise _invalid()
  expires = proof.get('expires_at')
  from datetime import datetime, timezone
  try:
    expiry = datetime.fromisoformat(expires.replace('Z', '+00:00'))
  except (AttributeError, ValueError):
    raise _invalid()
  if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc) or expiry > datetime.now(timezone.utc) + timedelta(minutes=2):
    raise _invalid()
  expected = {'iss': pending.issuer, 'sub': pending.subject,
              'aud': 'mobius-shared-browser', 'origin': grant.origin,
              'grant_id': grant.id, 'nonce': pending.nonce}
  if any(proof.get(key) != value for key, value in expected.items()):
    raise _invalid()
  return proof


def complete(db: Session, pending_id: str, previous_session_secret: str | None = None):
  try:
    now = now_naive_utc()
    pending = db.get(access.BrowserAccountPending, pending_id)
    if (pending is None or pending.verified_at is None
        or pending.verified_expires_at is None or pending.verified_expires_at <= now):
      raise _invalid()
    consumed = db.execute(update(access.BrowserAccountPending).where(
      access.BrowserAccountPending.id == pending_id,
      access.BrowserAccountPending.consumed_at.is_(None),
      access.BrowserAccountPending.expires_at > now,
    ).values(consumed_at=now)).rowcount
    if consumed != 1:
      raise _invalid()
    grant = access._lock_active_grant(db, pending.grant_id)
    owner = access._owner(db, grant.owner_id)
    if (grant.kind != 'account' or grant.remote_status != 'active'
        or grant.epoch != pending.grant_epoch or owner.token_epoch != pending.owner_token_epoch
        or grant.issuer != issuer_origin() or grant.issuer != pending.issuer
        or grant.subject != pending.subject or grant.origin != runtime_origin()):
      raise _invalid()
    secret = secrets.token_urlsafe(32)
    session = access.BrowserAccessSession(
      id=secrets.token_urlsafe(24), grant_id=grant.id,
      secret_hash=digest(secret), owner_token_epoch=owner.token_epoch,
      created_at=now, idle_expires_at=now + access.SESSION_IDLE_TTL,
    )
    db.add(session)
    if previous_session_secret:
      db.execute(update(access.BrowserAccessSession).where(
        access.BrowserAccessSession.secret_hash == digest(previous_session_secret),
        access.BrowserAccessSession.revoked_at.is_(None),
      ).values(revoked_at=now))
    db.commit()
    return secret, session, grant, owner
  except Exception:
    db.rollback()
    raise


def mark_verified(db: Session, pending_id: str, proof_expires_at: str) -> None:
  from datetime import datetime, timezone
  now = now_naive_utc()
  try:
    proof_expiry = datetime.fromisoformat(proof_expires_at.replace('Z', '+00:00'))
  except (AttributeError, ValueError) as exc:
    raise _invalid() from exc
  if proof_expiry.tzinfo is None:
    raise _invalid()
  expiry = min(proof_expiry.astimezone(timezone.utc).replace(tzinfo=None), now + timedelta(seconds=60))
  if expiry <= now:
    raise _invalid()
  changed = db.execute(update(access.BrowserAccountPending).where(
    access.BrowserAccountPending.id == pending_id,
    access.BrowserAccountPending.consumed_at.is_(None),
    access.BrowserAccountPending.verified_at.is_(None),
    access.BrowserAccountPending.expires_at > now,
  ).values(verified_at=now, verified_expires_at=expiry)).rowcount
  if changed != 1:
    db.rollback()
    raise _invalid()
  db.commit()


def verified_for_cookie(db: Session, cookie: str) -> access.BrowserAccountPending:
  if not isinstance(cookie, str) or not 20 <= len(cookie) <= 256:
    raise _invalid()
  pending = db.query(access.BrowserAccountPending).filter_by(cookie_hash=digest(cookie)).first()
  if (pending is None or pending.verified_at is None or pending.consumed_at is not None
      or pending.expires_at <= now_naive_utc()
      or pending.verified_expires_at is None or pending.verified_expires_at <= now_naive_utc()):
    raise _invalid()
  return pending
