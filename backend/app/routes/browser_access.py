"""Connect-owned invitations and independently revocable shared-browser sessions."""

from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit, quote
import re
import secrets
import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import auth, models, browser_access as access, account_browser_access as account_access
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


class AccountRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  recipient_handle: str = Field(min_length=3, max_length=31)
  instance_name: str | None = Field(default=None, max_length=128)


class SharedInvitationResponse(BaseModel):
  model_config = ConfigDict(extra="forbid")
  origin: str = Field(max_length=255)
  grant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{20,64}$")
  action: Literal["accept", "later"]


class FinalizeAccountRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  pending_id: str = Field(min_length=1, max_length=64)


async def _issuer_request(db: Session, owner_id: int, method: str, suffix: str, payload: dict | None = None) -> httpx.Response:
  """Only the configured account service receives a scoped local credential."""
  from app.routes import identity
  settings = get_settings()
  if settings.mobius_sso_enabled:
    return await identity._managed_response(
      method, "/api/instance/v1/browser-access" + suffix,
      **({"json": payload} if payload is not None else {}),
    )
  link = identity._linked_row(db, owner_id)
  if link is None:
    raise HTTPException(409, "Link your mobius.you account in Identity first.")
  try:
    token = identity._open(link.access_token_encrypted)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
      return await client.request(
        method, settings.mobius_account_origin + "/api/account/v1/browser-access" + suffix,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        **({"json": payload} if payload is not None else {}),
      )
  except (httpx.HTTPError, OSError) as exc:
    raise HTTPException(502, "The account directory could not be reached. Retry this operation.") from exc


def _remote_json(response: httpx.Response, *, allowed=(200, 201)) -> dict:
  if response.status_code not in allowed:
    status = response.status_code if response.status_code in (400, 401, 403, 404, 409, 422) else 502
    raise HTTPException(status, "The account directory rejected the request. Retry or check your account link.")
  try:
    value = response.json()
  except ValueError as exc:
    raise HTTPException(502, "The account directory returned an invalid response.") from exc
  if not isinstance(value, dict):
    raise HTTPException(502, "The account directory returned an invalid response.")
  return value


def _origin() -> str:
  try:
    return _canonical_https_origin(get_settings().frontend_origin)
  except ValueError as exc:
    raise HTTPException(409, "Browser sharing requires a directly reachable HTTPS address.") from exc


def _canonical_https_origin(value: str) -> str:
  """Pin browser grants to the same serialized HTTPS origin as browsers use."""
  if (not isinstance(value, str) or value != value.strip()
      or any(ord(char) <= 32 for char in value)
      or any(char in value for char in ("?", "#", "\\", "%"))):
    raise ValueError("invalid origin")
  parsed = urlsplit(value)
  if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
      or parsed.password is not None or parsed.path not in ("", "/")
      or parsed.query or parsed.fragment):
    raise ValueError("invalid origin")
  port = parsed.port  # Also rejects malformed or out-of-range ports.
  if port == 0 or parsed.netloc.endswith(":"):
    raise ValueError("invalid port")
  host = parsed.hostname
  if ":" in host:
    host = "[" + host + "]"
  return "https://" + host + (f":{port}" if port and port != 443 else "")


def _cookie_request(request: Request) -> None:
  # Cookie-bearing session renewal must be same ORIGIN, not merely same site.
  # The app's opaque-frame bearer exception does not authorize these cookies.
  try:
    same_origin = _canonical_https_origin(request.headers.get("origin")) == _origin()
  except ValueError:
    same_origin = False
  if not same_origin:
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
    "status": "revoked" if grant.revoked_at else (
      grant.remote_status if grant.kind == "account" else ("active" if accepted else "invited")
    ),
    "created_at": grant.created_at.isoformat() + "Z",
    "kind": grant.kind,
    "recipient_handle": grant.recipient_handle,
    "directory_cleanup_pending": grant.remote_status == "cleanup_pending",
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


@router.post("/accounts", dependencies=[Depends(reject_cross_site)])
async def add_account_recipient(
  body: AccountRequest, owner: models.Owner = Depends(_manager), db: Session = Depends(get_db),
):
  handle = body.recipient_handle.strip().lower().lstrip("@")
  if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,28}[a-z0-9]", handle):
    raise HTTPException(422, "Enter a valid mobius.you handle.")
  name = (body.instance_name or "This Möbius").strip()
  if not name:
    raise HTTPException(422, "Enter an instance name.")
  origin = _origin()
  issuer = account_access.issuer_origin()
  binding = access.account_binding(db, owner.id)
  owner_epoch = owner.token_epoch
  if binding is None:
    raise HTTPException(409, "Link your mobius.you account in Identity first.")
  pending = db.query(access.BrowserAccessGrant).filter_by(
    owner_id=owner.id, kind="account", recipient_handle=handle,
    label=name, revoked_at=None,
  ).order_by(access.BrowserAccessGrant.created_at.asc()).first()
  if pending is not None and (pending.issuer != issuer or pending.origin != origin
                              or pending.grantor_binding != binding):
    raise HTTPException(409, "This account grant belongs to a different configured address.")
  if pending is not None and pending.remote_status == "active":
    return JSONResponse({"grant": _grant_view(db, pending)}, headers={"Cache-Control": "no-store"})
  if pending is None:
    pending = access.BrowserAccessGrant(
      id=secrets.token_urlsafe(24), owner_id=owner.id, label=name,
      kind="account", issuer=issuer, recipient_handle=handle,
      origin=origin, remote_status="pending", epoch=0,
      grantor_binding=binding,
    )
    db.add(pending)
    db.commit()
  grant_id = pending.id
  # Keep this reserved ID on failure: the issuer may have committed before its
  # response was lost, so a fresh ID could leave a real grant behind.
  value = _remote_json(await _issuer_request(db, owner.id, "POST", "/grants", {
    "grant_id": grant_id, "recipient_handle": handle, "instance_name": name,
  }))
  if (value.get("issuer") != issuer or value.get("grant_id") != grant_id
      or value.get("origin") != origin or value.get("handle") != handle
      or not isinstance(value.get("subject"), str) or not value["subject"]):
    raise HTTPException(502, "The account directory returned mismatched grant identity. Retry this operation.")
  db.refresh(pending)
  db.refresh(owner)
  if (pending.revoked_at is not None or pending.grantor_binding != binding
      or access.account_binding(db, owner.id) != binding
      or pending.origin != origin or pending.issuer != issuer
      or owner.token_epoch != owner_epoch):
    # A remote registration may have committed just as local permission ended.
    # Clean it if possible, but never activate the now-stale local grant.
    try:
      cleanup = await _issuer_request(db, owner.id, "DELETE", "/grants/" + grant_id)
    except HTTPException:
      cleanup = None
    pending.remote_status = "revoked" if cleanup is not None and cleanup.status_code in (200, 204) else "cleanup_pending"
    if pending.revoked_at is None:
      access.revoke_grant(db, pending.id, owner.id)
    db.commit()
    raise HTTPException(409, "This grant changed while the account directory responded. Local access ended; check directory cleanup status.")
  pending.subject = value["subject"]
  pending.remote_status = "active"
  db.commit()
  return JSONResponse({"grant": _grant_view(db, pending)}, headers={"Cache-Control": "no-store"})


def _shared_origin(origin) -> str:
  try:
    parsed = urlsplit(origin) if isinstance(origin, str) else None
    if (not parsed or parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
        or not parsed.netloc or len(origin) > 255):
      raise ValueError("unsafe origin")
    parsed.port  # Reject malformed ports rather than forwarding them.
  except ValueError as exc:
    raise HTTPException(502, "The account directory returned an unsafe shared link.") from exc
  return origin


def _shared_instance(row) -> dict:
  """Validate discovery once; neither a notification nor its URL grants access."""
  if not isinstance(row, dict):
    raise HTTPException(502, "The account directory returned an invalid response.")
  origin = _shared_origin(row.get("origin"))
  gid = row.get("grant_id")
  if not isinstance(gid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", gid):
    raise HTTPException(502, "The account directory returned an unsafe shared link.")
  name, owner_handle = row.get("name"), row.get("owner_handle")
  if (not isinstance(name, str) or len(name) > 128
      or not isinstance(owner_handle, str) or len(owner_handle) > 128
      or row.get("status") not in ("invited", "accepted")
      or type(row.get("unread")) is not bool):
    raise HTTPException(502, "The account directory returned invalid shared metadata.")
  return {"grant_id": gid, "name": name, "origin": origin, "owner_handle": owner_handle,
          "status": row["status"], "unread": row["unread"],
          "open_url": origin + "/api/connect/browser-access/session/account/start?grant_id=" + quote(gid, safe="")}


@router.get("/shared")
async def shared_with_me(owner: models.Owner = Depends(_manager), db: Session = Depends(get_db)):
  value = _remote_json(await _issuer_request(db, owner.id, "GET", "/shared"))
  rows = value.get("instances")
  if not isinstance(rows, list):
    raise HTTPException(502, "The account directory returned an invalid response.")
  return JSONResponse({"instances": [_shared_instance(row) for row in rows]}, headers={"Cache-Control": "no-store"})


@router.post("/shared/respond", dependencies=[Depends(reject_cross_site)])
async def respond_to_shared_invitation(
  body: SharedInvitationResponse, owner: models.Owner = Depends(_manager),
  db: Session = Depends(get_db),
):
  try:
    _shared_origin(body.origin)
  except HTTPException as exc:
    raise HTTPException(422, "The invitation address is invalid.") from exc
  # Only the fixed issuer is contacted. It authenticates the recipient from the
  # existing account credential; the submitted instance address is never fetched.
  value = _remote_json(await _issuer_request(db, owner.id, "POST", "/shared/respond", body.model_dump()))
  instance = _shared_instance(value.get("instance"))
  if (instance["origin"] != body.origin or instance["grant_id"] != body.grant_id
      or instance["unread"] or (body.action == "accept" and instance["status"] != "accepted")):
    raise HTTPException(502, "The account directory returned a mismatched invitation response.")
  return JSONResponse({"instance": instance}, headers={"Cache-Control": "no-store"})


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


_PENDING_COOKIE = "mobius_shared_account_pending"
_PENDING_PATH = "/api/connect/browser-access/session/account"


@router.get("/session/account/start")
@_limiter.limit("10/minute")
def start_account_session(request: Request, grant_id: str, db: Session = Depends(get_db)):
  cookie, url = account_access.start(db, grant_id)
  response = RedirectResponse(url, status_code=303, headers={
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
  })
  response.set_cookie(_PENDING_COOKIE, cookie, httponly=True, secure=True,
                      samesite="lax", path=_PENDING_PATH, max_age=600)
  return response


@router.get("/session/account/callback")
@_limiter.limit("10/minute")
async def complete_account_session(
  request: Request, code: str, state: str, db: Session = Depends(get_db),
):
  pending, grant = account_access.pending_for_callback(
    db, state, request.cookies.get(_PENDING_COOKIE, ""),
  )
  proof = await account_access.exchange_code(pending, grant, code)
  account_access.mark_verified(db, pending.id, proof["expires_at"])
  response = RedirectResponse("/shell/shared#account-finalize=" + pending.id, status_code=303, headers={
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
  })
  return response


@router.post("/session/account/finalize")
@_limiter.limit("10/minute")
def finalize_account_session(
  body: FinalizeAccountRequest, request: Request, db: Session = Depends(get_db),
):
  _cookie_request(request)
  pending = account_access.verified_for_cookie(db, request.cookies.get(_PENDING_COOKIE, ""))
  if pending.id != body.pending_id:
    raise HTTPException(401, "Account sign-in unavailable. Start again from the shared link.")
  secret, session, grant, owner = account_access.complete(
    db, pending.id, request.cookies.get(_COOKIE),
  )
  response = _response(db, grant, session, owner, secret)
  response.delete_cookie(_PENDING_COOKIE, path=_PENDING_PATH,
                         secure=True, httponly=True, samesite="lax")
  return response


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
  grant = db.get(access.BrowserAccessGrant, grant_id)
  if grant.kind == "account" and grant.remote_status != "revoked":
    grant.remote_status = "cleanup_pending"
    db.commit()
  from app.app_services import cancel_browser_grant_calls
  from app.routes.connect import cancel_browser_grant_commands
  pending_commands = cancel_browser_grant_commands(grant_id)
  await cancel_browser_grant_calls(grant_id)
  pending_chats = []
  stop_error = None
  try:
    await stop_browser_grant_runs(grant_id, db)
  except HTTPException as exc:
    if not isinstance(exc.detail, dict) or exc.detail.get("code") != "browser_grant_stop_incomplete":
      if grant.kind != "account":
        raise
      stop_error = exc
    else:
      pending_chats = exc.detail["chat_ids"]
  except Exception as exc:
    if grant.kind != "account":
      raise
    stop_error = exc
  cleanup_pending = False
  if grant.kind == "account" and grant.remote_status != "revoked":
    try:
      response = await _issuer_request(db, owner.id, "DELETE", "/grants/" + quote(grant.id, safe=""))
      if response.status_code not in (200, 204):
        cleanup_pending = True
    except HTTPException:
      cleanup_pending = True
    grant.remote_status = "cleanup_pending" if cleanup_pending else "revoked"
    db.commit()
  if stop_error is not None:
    raise stop_error
  if pending_commands or pending_chats or cleanup_pending:
    body = {
      "revoked": True, "pending_commands": pending_commands,
      "pending_chat_ids": pending_chats,
    }
    if grant.kind == "account":
      body["directory_cleanup_pending"] = cleanup_pending
    return JSONResponse(body, status_code=202, headers={"Cache-Control": "no-store"})
  return Response(status_code=204)
