"""Contract tests for the unregistered browser-access persistence foundation."""

from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.browser_access import (
  BrowserAccessGrant, BrowserAccessInvite, BrowserAccessSession,
  INVITATION_TTL, SESSION_IDLE_TTL, create_invitation,
  logout_session, redeem_invitation, reissue_invitation, renew_session,
  revoke_grant, validate_grant, validate_session,
)
from app.models import Owner
from app.database import SessionLocal
from app.timeutil import now_naive_utc


def _owner(db, name):
  owner = Owner(username=name, hashed_password="unused")
  db.add(owner)
  db.commit()
  return owner


def _denied(action):
  with pytest.raises(HTTPException) as error:
    action()
  assert error.value.status_code == 401


def test_new_grants_are_recipient_isolated_and_secrets_are_never_stored(db):
  owner = _owner(db, "owner")
  other = _owner(db, "other")
  grant_a, invitation_a = create_invitation(db, owner, " laptop ")
  grant_b, invitation_b = create_invitation(db, owner, "phone")
  assert grant_a.id != grant_b.id
  assert grant_a.label == "laptop"
  assert grant_a.revoked_at is None
  assert grant_a.created_at is not None
  assert grant_a.epoch == 0
  assert grant_a.id != invitation_a
  assert invitation_a != invitation_b
  invites = db.query(BrowserAccessInvite).all()
  assert len(invites) == 2
  assert all(invite.secret_hash not in (invitation_a, invitation_b) for invite in invites)
  assert all(invite.expires_at - invite.created_at == INVITATION_TTL for invite in invites)
  assert all(invite.owner_token_epoch == owner.token_epoch for invite in invites)
  assert validate_grant(db, grant_a.id, 0, owner.id).id == grant_a.id
  _denied(lambda: validate_grant(db, grant_a.id, 0, other.id))
  _denied(lambda: validate_grant(db, grant_a.id, 1, owner.id))
  _denied(lambda: validate_grant(db, grant_b.id, 0, other.id))


def test_invitation_is_one_use_and_session_is_idle_expiring_not_grant_expiring(db):
  owner = _owner(db, "owner")
  grant, invitation = create_invitation(db, owner, "recipient")
  secret, session, received_grant, received_owner = redeem_invitation(db, invitation)
  assert received_grant.id == grant.id
  assert received_owner.id == owner.id
  assert session.secret_hash != secret
  assert session.idle_expires_at - session.created_at == SESSION_IDLE_TTL
  _denied(lambda: redeem_invitation(db, invitation))
  db.query(BrowserAccessInvite).filter_by(grant_id=grant.id).update({
    BrowserAccessInvite.expires_at: now_naive_utc() - timedelta(days=2),
  })
  db.commit()
  assert renew_session(db, secret)[0].id == grant.id
  assert validate_grant(db, grant.id, 0, owner.id).id == grant.id
  session.idle_expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  _denied(lambda: renew_session(db, secret))
  assert validate_grant(db, grant.id, 0, owner.id).id == grant.id


def test_expired_invitation_never_consumes_or_issues_session(db):
  owner = _owner(db, "owner")
  grant, invitation = create_invitation(db, owner, "recipient")
  invite = db.query(BrowserAccessInvite).filter_by(grant_id=grant.id).one()
  invite.expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  _denied(lambda: redeem_invitation(db, invitation))
  db.refresh(invite)
  assert invite.consumed_at is None
  assert db.query(BrowserAccessSession).count() == 0


def test_revocation_is_idempotent_and_cannot_affect_another_grant(db):
  owner = _owner(db, "owner")
  grant_a, invite_a = create_invitation(db, owner, "a")
  grant_b, invite_b = create_invitation(db, owner, "b")
  secret_a, _, _, _ = redeem_invitation(db, invite_a)
  secret_b, _, _, _ = redeem_invitation(db, invite_b)
  assert revoke_grant(db, grant_a.id, owner.id).epoch == 1
  assert revoke_grant(db, grant_a.id, owner.id).epoch == 1
  _denied(lambda: renew_session(db, secret_a))
  _denied(lambda: validate_grant(db, grant_a.id, 0, owner.id))
  _denied(lambda: validate_grant(db, grant_a.id, 1, owner.id))
  assert renew_session(db, secret_b)[0].id == grant_b.id
  assert validate_grant(db, grant_b.id, 0, owner.id).id == grant_b.id


def test_owner_epoch_change_invalidates_session_but_not_other_permission(db):
  owner = _owner(db, "owner")
  grant, invitation = create_invitation(db, owner, "recipient")
  secret, _, _, _ = redeem_invitation(db, invitation)
  owner.token_epoch += 1
  db.commit()
  _denied(lambda: renew_session(db, secret))
  assert validate_grant(db, grant.id, 0, owner.id).id == grant.id


def test_owner_epoch_change_invalidates_unredeemed_invitation(db):
  owner = _owner(db, "owner")
  grant, invitation = create_invitation(db, owner, "recipient")
  owner.token_epoch += 1
  db.commit()
  _denied(lambda: redeem_invitation(db, invitation))
  assert db.query(BrowserAccessSession).count() == 0
  invite = db.query(BrowserAccessInvite).filter_by(grant_id=grant.id).one()
  assert invite.consumed_at is None


def test_validate_session_checks_lineage_expiry_epoch_and_logout(db):
  owner = _owner(db, "owner")
  other = _owner(db, "other")
  grant, invitation = create_invitation(db, owner, "recipient")
  other_grant, _ = create_invitation(db, owner, "other recipient")
  secret, session, _, _ = redeem_invitation(db, invitation)
  assert validate_session(db, session.id, grant.id, owner.id).id == session.id
  _denied(lambda: validate_session(db, session.id, other_grant.id, owner.id))
  _denied(lambda: validate_session(db, session.id, grant.id, other.id))
  _denied(lambda: validate_session(db, "missing", grant.id, owner.id))
  logout_session(db, secret)
  logout_session(db, secret)
  logout_session(db, "unknown-secret-with-adequate-length")
  logout_session(db, None)
  _denied(lambda: validate_session(db, session.id, grant.id, owner.id))
  _denied(lambda: renew_session(db, secret))
  assert validate_grant(db, grant.id, 0, owner.id).id == grant.id


def test_validate_session_denies_idle_expiry_and_owner_epoch_change(db):
  owner = _owner(db, "owner")
  grant, invitation = create_invitation(db, owner, "recipient")
  _, session, _, _ = redeem_invitation(db, invitation)
  session.idle_expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  _denied(lambda: validate_session(db, session.id, grant.id, owner.id))
  session.idle_expires_at = now_naive_utc() + SESSION_IDLE_TTL
  owner.token_epoch += 1
  db.commit()
  _denied(lambda: validate_session(db, session.id, grant.id, owner.id))


def test_reissue_invitation_keeps_grant_and_session_but_expires_old_invite(db):
  owner = _owner(db, "owner")
  other = _owner(db, "other")
  grant, old_invitation = create_invitation(db, owner, "recipient")
  _denied(lambda: reissue_invitation(db, other, grant.id))
  new_invitation = reissue_invitation(db, owner, grant.id)
  assert new_invitation != old_invitation
  _denied(lambda: redeem_invitation(db, old_invitation))
  secret, session, received_grant, _ = redeem_invitation(db, new_invitation)
  assert received_grant.id == grant.id
  assert validate_session(db, session.id, grant.id, owner.id).id == session.id
  again = reissue_invitation(db, owner, grant.id)
  assert again != new_invitation
  assert renew_session(db, secret)[0].id == grant.id
  revoke_grant(db, grant.id, owner.id)
  _denied(lambda: reissue_invitation(db, owner, grant.id))
  _denied(lambda: redeem_invitation(db, again))
  _denied(lambda: validate_session(db, session.id, grant.id, owner.id))


@pytest.mark.parametrize("claim", [None, "", 123, [], {}, True])
def test_malformed_grant_id_claim_fails_closed(db, claim):
  owner = _owner(db, "owner")
  grant, _ = create_invitation(db, owner, "recipient")
  _denied(lambda: validate_grant(db, claim, 0, owner.id))


@pytest.mark.parametrize("claim", [None, "0", True, -1, 0.0, [], {}])
def test_malformed_epoch_claim_fails_closed(db, claim):
  owner = _owner(db, "owner")
  grant, _ = create_invitation(db, owner, "recipient")
  _denied(lambda: validate_grant(db, grant.id, claim, owner.id))


def test_wrong_owner_cannot_revoke_and_invite_remains_redeemable(db):
  owner = _owner(db, "owner")
  other = _owner(db, "other")
  grant, invitation = create_invitation(db, owner, "recipient")
  _denied(lambda: revoke_grant(db, grant.id, other.id))
  assert db.get(BrowserAccessGrant, grant.id).revoked_at is None
  assert redeem_invitation(db, invitation)[2].id == grant.id


def test_two_sessions_preloading_same_invite_still_only_redeem_once(db):
  owner = _owner(db, "owner")
  grant, invitation = create_invitation(db, owner, "recipient")
  first = SessionLocal()
  second = SessionLocal()
  try:
    assert first.query(BrowserAccessInvite).filter_by(grant_id=grant.id).one()
    assert second.query(BrowserAccessInvite).filter_by(grant_id=grant.id).one()
    assert redeem_invitation(first, invitation)[2].id == grant.id
    _denied(lambda: redeem_invitation(second, invitation))
    assert db.query(BrowserAccessSession).count() == 1
  finally:
    first.close()
    second.close()


def test_browser_tables_migrate_without_current_model_metadata(tmp_path):
  from sqlalchemy import create_engine, text
  from app.schema_migrations import _add_browser_access_tables
  engine = create_engine(f"sqlite:///{tmp_path / 'bare-browser.db'}")
  with engine.begin() as connection:
    connection.execute(text("CREATE TABLE owner (id INTEGER PRIMARY KEY, username VARCHAR, token_epoch INTEGER NOT NULL)"))
    connection.execute(text("INSERT INTO owner VALUES (1, 'old-owner', 0)"))
  _add_browser_access_tables(engine)
  _add_browser_access_tables(engine)
  with engine.begin() as connection:
    connection.execute(text("INSERT INTO browser_access_grants (id,owner_id,label,epoch,created_at) VALUES ('guest',1,'Alice',0,CURRENT_TIMESTAMP)"))
    assert connection.execute(text("SELECT label FROM browser_access_grants")).scalar_one() == "Alice"
