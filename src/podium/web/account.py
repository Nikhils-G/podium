import contextlib

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.errors import PodiumError, ValidationFailed
from podium.models import User
from podium.security.csrf import verify_csrf
from podium.security.deps import require_user
from podium.security.passwords import hash_password, verify_password
from podium.services import audit
from podium.services import tokens as tokens_service
from podium.web.rendering import render

router = APIRouter(include_in_schema=False)


def _ctx(db, user, **extra):
    return {
        "user": user,
        "tokens": tokens_service.list_tokens(db, user),
        "errors": {},
        "new_token": None,
        "saved": "",
        **extra,
    }


@router.get("/account")
def account_page(
    request: Request,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
    saved: str = "",
):
    return render(request, "account/account.html", title="Account", **_ctx(db, user, saved=saved))


@router.post("/account/profile", dependencies=[Depends(verify_csrf)])
def profile_save(
    request: Request,
    name: str = Form(""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    name = name.strip()
    if not name or len(name) > 120:
        return render(
            request,
            "account/account.html",
            status_code=422,
            title="Account",
            **_ctx(db, user, errors={"name": "Enter a name up to 120 characters."}),
        )
    user.name = name
    audit.record(db, "user.profile_updated", "user", user.public_id, actor_id=user.id)
    db.commit()
    return RedirectResponse("/account?saved=profile", status_code=303)


@router.post("/account/password", dependencies=[Depends(verify_csrf)])
def password_change(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    errors = {}
    if not verify_password(user.password_hash, current):
        errors["current"] = "That isn't your current password."
    if len(new) < 8:
        errors["new"] = "Use at least 8 characters."
    if errors:
        return render(
            request,
            "account/account.html",
            status_code=422,
            title="Account",
            **_ctx(db, user, errors=errors),
        )
    user.password_hash = hash_password(new)
    audit.record(db, "user.password_changed", "user", user.public_id, actor_id=user.id)
    db.commit()
    return RedirectResponse("/account?saved=password", status_code=303)


@router.post("/account/tokens", dependencies=[Depends(verify_csrf)])
def token_create(
    request: Request,
    name: str = Form(""),
    scope: str = Form("read"),
    expires_in_days: str = Form("90"),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    try:
        token, raw = tokens_service.create_token(
            db,
            user,
            name,
            scope=scope if scope in tokens_service.SCOPES else "read",
            expires_in_days=int(expires_in_days) if expires_in_days.strip().isdigit() else None,
        )
    except ValidationFailed as exc:
        return render(
            request,
            "account/account.html",
            status_code=422,
            title="Account",
            **_ctx(db, user, errors=exc.errors),
        )
    return render(request, "account/account.html", title="Account", **_ctx(db, user, new_token=raw))


@router.post("/account/tokens/{token_id}/revoke", dependencies=[Depends(verify_csrf)])
def token_revoke(
    token_id: int, user: User = Depends(require_user), db: DbSession = Depends(get_db)
):
    with contextlib.suppress(PodiumError):
        tokens_service.revoke_token(db, user, token_id)
    return RedirectResponse("/account?saved=token", status_code=303)
