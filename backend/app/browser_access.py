"""Isolated, owner-authorized browser grants for a remote Möbius instance.

Callers must authenticate the granting owner, set an HttpOnly refresh cookie,
and validate every browser JWT's ``browser_grant``, ``browser_grant_epoch``,
and ``browser_session`` claims. A browser logout revokes only its session;
grant revocation also stops descendant work that carries grant lineage.
Functions commit their own transitions; a failed transition rolls back.
"""

import hashlib
import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, update
from sqlalchemy.orm import Session

from app.database import Base
from app.timeutil import now_naive_utc


INVITATION_TTL = timedelta(days=1)
SESSION_IDLE_TTL = timedelta(days=30)


class BrowserAccessGrant(Base):
  """Lasting permission for one labeled recipient; no grant expiry."""

  __tablename__ = "browser_access_grants"

  id = Column(String(64), primary_key=True)
  owner_id = Column(Integer, ForeignKey("owner.id"), nullable=False, index=True)
  label = Column(String(128), nullable=False)
  epoch = Column(Integer, nullable=False, default=0)
  created_at = Column(DateTime, nullable=False, default=now_naive_utc)
  revoked_at = Column(DateTime, nullable=True, default=None)


class BrowserAccessInvite(Base):
  """One-use, short-lived invitation; only SHA-256 of its secret persists."""

  __tablename__ = "browser_access_invites"

  id = Column(String(64), primary_key=True)
  grant_id = Column(String(64), ForeignKey("browser_access_grants.id"), nullable=False, index=True)
  secret_hash = Column(String(64), nullable=False, unique=True, index=True)
  owner_token_epoch = Column(Integer, nullable=False)
  created_at = Column(DateTime, nullable=False, default=now_naive_utc)
  expires_at = Column(DateTime, nullable=False)
  consumed_at = Column(DateTime, nullable=True, default=None)


class BrowserAccessSession(Base):
  """Server-checked, idle-expiring refresh authority, not a public OAuth token."""

  __tablename__ = "browser_access_sessions"

  id = Column(String(64), primary_key=True)
  grant_id = Column(String(64), ForeignKey("browser_access_grants.id"), nullable=False, index=True)
  secret_hash = Column(String(64), nullable=False, unique=True, index=True)
  owner_token_epoch = Column(Integer, nullable=False)
  created_at = Column(DateTime, nullable=False, default=now_naive_utc)
  idle_expires_at = Column(DateTime, nullable=False)
  revoked_at = Column(DateTime, nullable=True, default=None)


def _unauthorized() -> HTTPException:
  return HTTPException(status_code=401, detail="Browser access unavailable.")


def _hash_secret(secret: str) -> str:
  if not isinstance(secret, str) or not 20 <= len(secret) <= 256:
    raise _unauthorized()
  return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _lock_active_grant(db: Session, grant_id: str) -> BrowserAccessGrant:
  # A conditional write serializes redemption/renewal with revocation even on
  # SQLite, where SELECT FOR UPDATE has no effect. Never rely on a stale ORM row.
  changed = db.execute(update(BrowserAccessGrant).where(
    BrowserAccessGrant.id == grant_id,
    BrowserAccessGrant.revoked_at.is_(None),
  ).values(epoch=BrowserAccessGrant.epoch)).rowcount
  if changed != 1:
    raise _unauthorized()
  return db.query(BrowserAccessGrant).execution_options(
    populate_existing=True,
  ).filter_by(id=grant_id).one()


def _owner(db: Session, owner_id: int):
  # Lazy import avoids an app.models / model-registration cycle.
  from app.models import Owner
  owner = db.query(Owner).execution_options(
    populate_existing=True,
  ).filter_by(id=owner_id).first()
  if owner is None:
    raise _unauthorized()
  return owner


def create_invitation(db: Session, owner, label: str) -> tuple[BrowserAccessGrant, str]:
  """Create one recipient grant and return its invitation secret exactly once.

  ``owner`` must already be authenticated and authorized by the caller.
  """
  if not isinstance(label, str) or not 1 <= len(label.strip()) <= 128:
    raise ValueError("Recipient label must contain 1–128 characters.")
  if owner is None or type(owner.id) is not int:
    raise ValueError("An authenticated owner is required.")
  now = now_naive_utc()
  current_owner = _owner(db, owner.id)
  secret = secrets.token_urlsafe(32)
  grant = BrowserAccessGrant(
    id=secrets.token_urlsafe(24), owner_id=owner.id,
    label=label.strip(), created_at=now, epoch=0,
  )
  db.add(grant)
  db.add(BrowserAccessInvite(
    id=secrets.token_urlsafe(24), grant_id=grant.id,
    secret_hash=_hash_secret(secret), owner_token_epoch=current_owner.token_epoch,
    created_at=now,
    expires_at=now + INVITATION_TTL,
  ))
  db.commit()
  return grant, secret


def reissue_invitation(db: Session, owner, grant_id: str) -> str:
  """Issue a new one-day invite for an existing active recipient grant.

  Prior unused invites for that grant expire atomically. Existing sessions and
  the lasting grant remain unchanged. Caller must authorize ``owner`` first.
  """
  if owner is None or type(owner.id) is not int or not isinstance(grant_id, str):
    raise _unauthorized()
  try:
    now = now_naive_utc()
    grant = db.query(BrowserAccessGrant).filter_by(id=grant_id).first()
    if grant is None or grant.owner_id != owner.id or grant.revoked_at is not None:
      raise _unauthorized()
    # Match redemption's lock order: unused invite rows before the grant.
    # If revocation wins meanwhile, rollback restores all old invitations.
    db.execute(update(BrowserAccessInvite).where(
      BrowserAccessInvite.grant_id == grant_id,
      BrowserAccessInvite.consumed_at.is_(None),
      BrowserAccessInvite.expires_at > now,
    ).values(expires_at=now))
    grant = _lock_active_grant(db, grant_id)
    current_owner = _owner(db, grant.owner_id)
    secret = secrets.token_urlsafe(32)
    db.add(BrowserAccessInvite(
      id=secrets.token_urlsafe(24), grant_id=grant_id,
      secret_hash=_hash_secret(secret),
      owner_token_epoch=current_owner.token_epoch,
      created_at=now, expires_at=now + INVITATION_TTL,
    ))
    db.commit()
    return secret
  except Exception:
    db.rollback()
    raise


def redeem_invitation(db: Session, secret: str, *, previous_session_secret: str | None = None):
  """Atomically consume an invite; return (session_secret, session, grant, owner)."""
  try:
    now = now_naive_utc()
    digest = _hash_secret(secret)
    invite = db.query(BrowserAccessInvite).filter_by(secret_hash=digest).first()
    if invite is None:
      raise _unauthorized()
    consumed = db.execute(update(BrowserAccessInvite).where(
      BrowserAccessInvite.id == invite.id,
      BrowserAccessInvite.consumed_at.is_(None),
      BrowserAccessInvite.expires_at > now,
    ).values(consumed_at=now)).rowcount
    if consumed != 1:
      raise _unauthorized()
    grant = _lock_active_grant(db, invite.grant_id)
    owner = _owner(db, grant.owner_id)
    if owner.token_epoch != invite.owner_token_epoch:
      raise _unauthorized()
    session_secret = secrets.token_urlsafe(32)
    session = BrowserAccessSession(
      id=secrets.token_urlsafe(24), grant_id=grant.id,
      secret_hash=_hash_secret(session_secret),
      owner_token_epoch=owner.token_epoch,
      created_at=now, idle_expires_at=now + SESSION_IDLE_TTL,
    )
    db.add(session)
    # One cookie slot per browser: switching recipient atomically retires its
    # previous session, never another browser or the installation owner.
    if previous_session_secret:
      db.execute(update(BrowserAccessSession).where(
        BrowserAccessSession.secret_hash == _hash_secret(previous_session_secret),
        BrowserAccessSession.revoked_at.is_(None),
      ).values(revoked_at=now))
    db.commit()
    return session_secret, session, grant, owner
  except Exception:
    db.rollback()
    raise


def renew_session(db: Session, secret: str):
  """Touch valid refresh session; return (grant, session, owner) for JWT minting."""
  try:
    now = now_naive_utc()
    session = db.query(BrowserAccessSession).filter_by(
      secret_hash=_hash_secret(secret),
    ).first()
    if session is None:
      raise _unauthorized()
    touched = db.execute(update(BrowserAccessSession).where(
      BrowserAccessSession.id == session.id,
      BrowserAccessSession.revoked_at.is_(None),
      BrowserAccessSession.idle_expires_at > now,
    ).values(idle_expires_at=now + SESSION_IDLE_TTL)).rowcount
    if touched != 1:
      raise _unauthorized()
    grant = _lock_active_grant(db, session.grant_id)
    owner = _owner(db, grant.owner_id)
    if owner.token_epoch != session.owner_token_epoch:
      raise _unauthorized()
    db.commit()
    return grant, session, owner
  except Exception:
    db.rollback()
    raise


def validate_session(
  db: Session, session_id: str, grant_id: str, owner_id: int,
) -> BrowserAccessSession:
  """Validate browser JWT session lineage without extending idle lifetime."""
  if (
    not isinstance(session_id, str) or not 1 <= len(session_id) <= 64
    or not isinstance(grant_id, str) or not 1 <= len(grant_id) <= 64
    or type(owner_id) is not int
  ):
    raise _unauthorized()
  session = db.query(BrowserAccessSession).execution_options(
    populate_existing=True,
  ).filter_by(id=session_id).first()
  if (
    session is None or session.grant_id != grant_id
    or session.revoked_at is not None
    or session.idle_expires_at <= now_naive_utc()
  ):
    raise _unauthorized()
  grant = db.query(BrowserAccessGrant).execution_options(
    populate_existing=True,
  ).filter_by(id=grant_id).first()
  if grant is None or grant.owner_id != owner_id or grant.revoked_at is not None:
    raise _unauthorized()
  if _owner(db, grant.owner_id).token_epoch != session.owner_token_epoch:
    raise _unauthorized()
  return session


def logout_session(db: Session, secret: str, *, grant_id: str | None = None) -> None:
  """Idempotently revoke a refresh session; unknown secrets reveal nothing."""
  if not isinstance(secret, str) or not 20 <= len(secret) <= 256:
    return
  try:
    if grant_id is not None:
      current = db.query(BrowserAccessSession).filter_by(secret_hash=_hash_secret(secret)).first()
      if current is not None and current.grant_id != grant_id:
        raise HTTPException(409, "Another shared session is active in this browser.")
    db.execute(update(BrowserAccessSession).where(
      BrowserAccessSession.secret_hash == _hash_secret(secret),
      BrowserAccessSession.revoked_at.is_(None),
    ).values(revoked_at=now_naive_utc()))
    db.commit()
  except Exception:
    db.rollback()
    raise


def revoke_grant(db: Session, grant_id: str, owner_id: int) -> BrowserAccessGrant:
  """Idempotently revoke an owner's grant and bump its JWT validity epoch."""
  if not isinstance(grant_id, str) or type(owner_id) is not int:
    raise _unauthorized()
  try:
    now = now_naive_utc()
    changed = db.execute(update(BrowserAccessGrant).where(
      BrowserAccessGrant.id == grant_id,
      BrowserAccessGrant.owner_id == owner_id,
      BrowserAccessGrant.revoked_at.is_(None),
    ).values(
      revoked_at=now, epoch=BrowserAccessGrant.epoch + 1,
    )).rowcount
    grant = db.get(BrowserAccessGrant, grant_id)
    if grant is None or grant.owner_id != owner_id:
      raise _unauthorized()
    if changed not in (0, 1):
      raise RuntimeError("Unexpected grant revocation cardinality")
    db.commit()
    return grant
  except Exception:
    db.rollback()
    raise


def validate_grant(db: Session, grant_id: str, epoch: int, owner_id: int) -> BrowserAccessGrant:
  """Fail closed on malformed JWT claims, missing grant, wrong owner, or epoch."""
  if (
    not isinstance(grant_id, str) or not 1 <= len(grant_id) <= 64
    or type(epoch) is not int or epoch < 0
    or type(owner_id) is not int
  ):
    raise _unauthorized()
  grant = db.query(BrowserAccessGrant).execution_options(
    populate_existing=True,
  ).filter_by(id=grant_id).first()
  if (
    grant is None or grant.owner_id != owner_id
    or grant.revoked_at is not None or grant.epoch != epoch
  ):
    raise _unauthorized()
  return grant
