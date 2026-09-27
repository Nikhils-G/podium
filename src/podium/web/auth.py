from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.errors import NotFound, ValidationFailed
from podium.models import Session, User
from podium.security.csrf import verify_csrf
from podium.security.deps import current_user
from podium.security.ratelimit import ip_hash, limiter
from podium.security.sessions import (
    COOKIE_NAME,
    DEMO_PREFIXES,
    create_session,
    demo_session_token,
    hash_token,
    revoke_session,
)
from podium.services import auth as auth_service
from podium.services import navigation
from podium.web.rendering import render

router = APIRouter(include_in_schema=False)
DEMO_ROLES = ["organizer", "judge_a", "judge_b", "participant"]


def safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def set_session_cookie(response: RedirectResponse, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=settings.session_days * 86400,
        path="/",
    )


def _login_context(settings: Settings, **extra):
    return {
        "demo_roles": DEMO_ROLES if settings.demo_accounts else [],
        "demo_password": settings.demo_password,
        **extra,
    }


@router.get("/login")
def login_page(
    request: Request,
    next: str = "",
    user: User | None = Depends(current_user),
    settings: Settings = Depends(get_settings),
):
    if user is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(
        request,
        "auth/login.html",
        title="Sign in",
        nav="login",
        **_login_context(settings, next=safe_next(next) if next else "", email="", error=""),
    )


@router.post("/login", dependencies=[Depends(verify_csrf), Depends(limiter("login", 10, 60))])
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    user = auth_service.authenticate(db, email=email, password=password, ip_hash=ip_hash(request))
    if user is None:
        return render(
            request,
            "auth/login.html",
            status_code=401,
            title="Sign in",
            nav="login",
            **_login_context(
                settings,
                next=next,
                email=email,
                error="That email and password don't match. Try again.",
            ),
        )
    token = create_session(db, user, days=settings.session_days)
    db.commit()
    target = safe_next(next) if next else navigation.landing_for(db, user)
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, token, settings)
    return response


@router.post("/login/demo/{role}", dependencies=[Depends(verify_csrf)])
def login_demo(
    role: str, db: DbSession = Depends(get_db), settings: Settings = Depends(get_settings)
):
    if not settings.demo_accounts or role not in DEMO_PREFIXES:
        raise NotFound("Demo accounts are not enabled on this install.")
    token = demo_session_token(settings.secret_key, role)
    row = db.get(Session, hash_token(token))
    if row is None:
        raise NotFound("Demo accounts have not been seeded yet.")
    response = RedirectResponse(
        navigation.landing_for(db, db.get(User, row.user_id)), status_code=303
    )
    set_session_cookie(response, token, settings)
    return response


@router.get("/register")
def register_page(
    request: Request,
    next: str = "",
    user: User | None = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    if user is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(
        request,
        "auth/register.html",
        title="Create account",
        next=safe_next(next) if next else "",
        values={"name": "", "email": ""},
        errors={},
        error="",
        bootstrap=auth_service.instance_is_empty(db),
    )


@router.post("/register", dependencies=[Depends(verify_csrf), Depends(limiter("register", 5, 60))])
def register_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        user = auth_service.register(
            db, email=email, name=name, password=password, ip_hash=ip_hash(request)
        )
    except ValidationFailed as exc:
        return render(
            request,
            "auth/register.html",
            status_code=422,
            title="Create account",
            next=next,
            values={"name": name, "email": email},
            errors=exc.errors,
            error="",
        )
    token = create_session(db, user, days=settings.session_days)
    db.commit()
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(response, token, settings)
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, db: DbSession = Depends(get_db)):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        revoke_session(db, token)
        db.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# keep the import used for type checkers / future use
_ = select
