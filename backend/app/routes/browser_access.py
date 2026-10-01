"""Connect-owned invitations and independently revocable shared-browser sessions."""

from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import auth, models, browser_access as access
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, get_principal, get_owner_or_app_with_connect_manage,
  require_installation_owner_control, reject_cross_site,
)
from app.timeutil import now_naive_utc

router = APIRouter(prefix="/api/connect/browser-access", tags=["connect"])
_limiter = Limiter(key_func=get_remote_address)
_COOKIE = "mobius_shared_browser"
_COOKIE_PATH = "/api/connect/browser-access/session"
_ACCESS_TTL = timedelta(minutes=15)


class InviteRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  label: str = Field(min_length=1, max_length=128)


class LogoutRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  grant_id: str = Field(min_length=1, max_length=64)


class RedeemRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  invite: str = Field(min_length=20, max_length=256)


def _origin() -> str:
  origin = get_settings().frontend_origin.rstrip("/")
  parsed = urlsplit(origin)
  if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
    raise HTTPException(409, "Browser sharing requires a directly reachable HTTPS address.")
  return origin


def _cookie_request(request: Request) -> None:
  # Cookie-bearing session renewal must be same ORIGIN, not merely same site.
  # The app's opaque-frame bearer exception does not authorize these cookies.
  if request.headers.get("origin") != _origin():
    raise HTTPException(403, "Open this invitation on the receiving Möbius address.")


def _manager(
  principal: Principal = Depends(get_principal),
  owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> models.Owner:
  require_installation_owner_control(principal)
  return owner


def _grant_view(db: Session, grant) -> dict:
  accepted = db.query(access.BrowserAccessInvite.id).filter(
    access.BrowserAccessInvite.grant_id == grant.id,
    access.BrowserAccessInvite.consumed_at.isnot(None),
  ).first() is not None
  stop_pending = False
  if grant.revoked_at:
    from app.chat import browser_grant_active_chat_ids
    from app.app_services import browser_grant_has_active_calls
    from app.routes.connect import browser_grant_pending_commands
    stop_pending = bool(
      browser_grant_active_chat_ids(db, grant.id)
      or browser_grant_has_active_calls(grant.id)
      or browser_grant_pending_commands(grant.id)
    )
  return {
    "stop_pending": stop_pending,
    "id": grant.id, "label": grant.label,
    "status": "revoked" if grant.revoked_at else ("active" if accepted else "invited"),
    "created_at": grant.created_at.isoformat() + "Z",
  }


def _response(db: Session, grant, session, owner, secret: str) -> JSONResponse:
  token = auth.create_access_token(
    {"sub": owner.username},
    expires_delta=_ACCESS_TTL, token_epoch=owner.token_epoch,
    browser_grant_id=grant.id, browser_grant_epoch=grant.epoch,
    browser_session_id=session.id,
  )
  response = JSONResponse({
    "access_token": token, "token_type": "bearer",
    "expires_in": int(_ACCESS_TTL.total_seconds()),
    "grant": _grant_view(db, grant),
  }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
  response.set_cookie(
    _COOKIE, secret, httponly=True, secure=True, samesite="strict",
    path=_COOKIE_PATH, max_age=int(access.SESSION_IDLE_TTL.total_seconds()),
  )
  return response


@router.get("")
async def list_browser_grants(owner: models.Owner = Depends(_manager), db: Session = Depends(get_db)):
  rows = db.query(access.BrowserAccessGrant).filter_by(owner_id=owner.id).order_by(
    access.BrowserAccessGrant.created_at.asc(),
  ).all()
  return {"grants": [_grant_view(db, row) for row in rows]}


@router.post("", dependencies=[Depends(reject_cross_site)])
def invite_browser_recipient(
  body: InviteRequest, owner: models.Owner = Depends(_manager), db: Session = Depends(get_db),
):
  origin = _origin()
  label = body.label.strip()
  if not label:
    raise HTTPException(422, "Enter a name for this recipient.")
  grant, secret = access.create_invitation(db, owner, label)
  return JSONResponse({
    "grant": _grant_view(db, grant),
    "join_url": origin + "/shell/shared#invite=" + secret,
    "invite_expires_at": (grant.created_at + access.INVITATION_TTL).isoformat() + "Z",
  }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/{grant_id}/invitation", dependencies=[Depends(reject_cross_site)])
def reissue_browser_invitation(
  grant_id: str, owner: models.Owner = Depends(_manager), db: Session = Depends(get_db),
):
  origin = _origin()
  secret = access.reissue_invitation(db, owner, grant_id)
  grant = db.get(access.BrowserAccessGrant, grant_id)
  return JSONResponse({
    "grant": _grant_view(db, grant),
    "join_url": origin + "/shell/shared#invite=" + secret,
    "invite_expires_at": (now_naive_utc() + access.INVITATION_TTL).isoformat() + "Z",
  }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/session/redeem")
@_limiter.limit("10/minute")
def redeem_browser_invitation(body: RedeemRequest, request: Request, db: Session = Depends(get_db)):
  _cookie_request(request)
  secret, session, grant, owner = access.redeem_invitation(
    db, body.invite, previous_session_secret=request.cookies.get(_COOKIE),
  )
  return _response(db, grant, session, owner, secret)


@router.post("/session")
@_limiter.limit("60/minute")
def browser_session(request: Request, db: Session = Depends(get_db)):
  _cookie_request(request)
  secret = request.cookies.get(_COOKIE, "")
  grant, session, owner = access.renew_session(db, secret)
  return _response(db, grant, session, owner, secret)


@router.post("/session/logout", status_code=204)
def logout_browser_session(body: LogoutRequest, request: Request, db: Session = Depends(get_db)):
  _cookie_request(request)
  access.logout_session(db, request.cookies.get(_COOKIE, ""), grant_id=body.grant_id)
  response = Response(status_code=204, headers={"Cache-Control": "no-store"})
  response.delete_cookie(_COOKIE, path=_COOKIE_PATH, secure=True, httponly=True, samesite="strict")
  return response


@router.delete("/{grant_id}", status_code=204, dependencies=[Depends(reject_cross_site)])
async def revoke_browser_access(
  grant_id: str, owner: models.Owner = Depends(_manager), db: Session = Depends(get_db),
):
  from app.chat import stop_browser_grant_runs
  access.revoke_grant(db, grant_id, owner.id)
  from app.app_services import cancel_browser_grant_calls
  from app.routes.connect import cancel_browser_grant_commands
  pending_commands = cancel_browser_grant_commands(grant_id)
  await cancel_browser_grant_calls(grant_id)
  pending_chats = []
  try:
    await stop_browser_grant_runs(grant_id, db)
  except HTTPException as exc:
    if not isinstance(exc.detail, dict) or exc.detail.get("code") != "browser_grant_stop_incomplete":
      raise
    pending_chats = exc.detail["chat_ids"]
  if pending_commands or pending_chats:
    return JSONResponse({
      "revoked": True, "pending_commands": pending_commands,
      "pending_chat_ids": pending_chats,
    }, status_code=202, headers={"Cache-Control": "no-store"})
  return Response(status_code=204)
