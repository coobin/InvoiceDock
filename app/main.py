from __future__ import annotations

import logging
import math
import re
import secrets
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.config import get_settings
from app.db import SessionLocal, get_db, init_db
from app.models import AuditLog, Invoice, JobLog, Mailbox, OAuthState, User, UserTitle, utcnow
from app.security import (
    bootstrap_admin,
    client_ip,
    csrf_token,
    current_user,
    encrypt_secret,
    hash_password,
    is_reserved_username,
    mark_login,
    password_policy_error,
    record_audit,
    rotate_user_sessions,
    start_user_session,
    throttle_limit,
    throttle_reset,
    validate_csrf,
    verify_password,
)
from app.services.export_service import make_invoice_workbook, make_preview, make_print_pdf
from app.services.ingestion import extract_zip_candidates, ingest_bytes
from app.services.mail_service import scan_all_mailboxes, sync_mailbox, test_mailbox
from app.services.network_security import validate_outbound_host
from app.services.notification_service import (
    get_notification_settings,
    notify_event_background,
    save_notification_settings,
    test_bark_notification,
)
from app.services.quota_service import (
    get_tax_verify_daily_limit,
    get_tax_verify_usage,
    set_tax_verify_daily_limit,
)
from app.services.settings_service import (
    INTEGRATION_KEYS,
    OIDC_TOGGLE_KEY,
    USER_CONFIGURABLE_INTEGRATIONS,
    as_bool,
    clear_user_integration,
    get_env_keys,
    get_integrations,
    get_user_tax_verify_enabled,
    get_value,
    oidc_enabled,
    set_user_tax_verify_enabled,
    set_value,
    update_integrations,
    user_custom_integrations,
)
from app.services.title_service import env_presets, user_titles
from app.services.verifier import process_invoice, test_kingdee, test_llm, test_piaozone

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
MAX_EXPORT_FILES = 500
MAX_EXPORT_BYTES = 512 * 1024 * 1024
scheduler = BackgroundScheduler(timezone=settings.tz)
oauth = OAuth()
if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_client_id:
    oauth.register(
        name="oidc",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": settings.oidc_scopes},
    )


def _notification_user_label(user: User) -> str:
    identifier = user.email or user.username
    if "@" in identifier:
        local, domain = identifier.rsplit("@", 1)
        identifier = f"{local[:1] or '*'}***@{domain}"
    display_name = (user.display_name or "").strip()
    return f"{display_name}（{identifier}）" if display_name else identifier


async def _oidc_set_state_data(session, state, data) -> None:  # type: ignore[no-untyped-def]
    with SessionLocal() as db:
        for row in db.scalars(select(OAuthState).where(OAuthState.expires_at < utcnow())).all():
            db.delete(row)
        row = db.get(OAuthState, state)
        if row:
            row.data = data
            row.expires_at = utcnow() + timedelta(minutes=15)
        else:
            db.add(OAuthState(state=state, data=data, expires_at=utcnow() + timedelta(minutes=15)))
        db.commit()


async def _oidc_get_state_data(session, state) -> dict | None:  # type: ignore[no-untyped-def]
    with SessionLocal() as db:
        row = db.get(OAuthState, state)
        if row and row.expires_at > utcnow():
            return row.data
        return None


async def _oidc_clear_state_data(session, state) -> None:  # type: ignore[no-untyped-def]
    with SessionLocal() as db:
        row = db.get(OAuthState, state)
        if row:
            db.delete(row)
            db.commit()


async def _oidc_userinfo(token: dict) -> dict:
    """Fetch claims from the provider's userinfo endpoint. authlib 1.7.2 only
    exposes parsed id_token claims (which Authelia keeps minimal), so we call
    the userinfo endpoint directly to get name/email/preferred_username."""
    access_token = str(token.get("access_token") or "")
    if not access_token:
        return {}
    try:
        issuer = settings.oidc_issuer.rstrip("/")
        async with httpx.AsyncClient(timeout=15.0, verify=True) as client:
            metadata = (await client.get(f"{issuer}/.well-known/openid-configuration")).json()
            endpoint = str(metadata.get("userinfo_endpoint") or f"{issuer}/api/oidc/userinfo")
            response = await client.get(
                endpoint, headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            return response.json()
    except Exception:
        return {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for directory in (settings.data_dir, settings.upload_dir, settings.preview_dir, settings.export_dir):
        directory.mkdir(parents=True, exist_ok=True)
    init_db()
    if settings.oidc_enabled and oauth.oidc:
        oauth.oidc.framework.set_state_data = _oidc_set_state_data
        oauth.oidc.framework.get_state_data = _oidc_get_state_data
        oauth.oidc.framework.clear_state_data = _oidc_clear_state_data
    with SessionLocal() as db:
        created = bootstrap_admin(db)
        if created:
            logger.warning("Created bootstrap administrator %s; change the password after first login", created.username)
    if settings.mail_scan_interval_minutes > 0 and not scheduler.running:
        scheduler.add_job(
            scan_all_mailboxes,
            "interval",
            minutes=settings.mail_scan_interval_minutes,
            id="mailbox-scan",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="InvoiceDock API",
    description="自托管发票收集、查验、复核与打印工作台",
    version=__version__,
    docs_url="/api/docs" if settings.enable_api_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.enable_api_docs else None,
    lifespan=lifespan,
)
app.state.mail_interval = settings.mail_scan_interval_minutes
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    session_cookie="invoicedock_session",
    max_age=12 * 60 * 60,
    same_site="lax",
    https_only=settings.session_https_only,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    if request.url.path != "/healthz" and not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if settings.session_https_only:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


STATUS_META = {
    "pending": ("待处理", "muted"),
    "processing": ("处理中", "progress"),
    "verified": ("税务已查验", "verified"),
    "consistent": ("双源一致", "consistent"),
    "review": ("待人工复核", "review"),
    "reviewed": ("人工已复核", "reviewed"),
    "duplicate": ("疑似重复", "duplicate"),
    "failed": ("处理失败", "failed"),
}


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def asset_version() -> str:
    """Static asset cache-buster based on file mtime, so JS/CSS changes
    invalidate browser caches even when the app version stays the same."""
    static_dir = Path(__file__).parent / "static"
    latest = 0.0
    for name in ("app.js", "app.css"):
        path = static_dir / name
        if path.exists():
            latest = max(latest, path.stat().st_mtime)
    return str(int(latest))


templates.env.globals.update(
    csrf_token=csrf_token,
    app_name=settings.app_name,
    app_version=__version__,
    asset_version=asset_version(),
    status_meta=STATUS_META,
    human_size=human_size,
)


def flash(request: Request, message: str, kind: str = "success") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def context(request: Request, user: User | None = None, **values):  # type: ignore[no-untyped-def]
    return {"request": request, "user": user, "flash": request.session.pop("flash", None), **values}


def require_page_user(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_page_admin(request: Request, db: Session) -> User:
    user = require_page_user(request, db)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def invoice_query(q: str = "", status: str = "", source: str = "", user: User | None = None):
    query = select(Invoice)
    if user and user.role != "admin":
        query = query.where(Invoice.owner_id == user.id)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                Invoice.invoice_number.ilike(pattern),
                Invoice.invoice_code.ilike(pattern),
                Invoice.seller_name.ilike(pattern),
                Invoice.buyer_name.ilike(pattern),
                Invoice.original_name.ilike(pattern),
            )
        )
    if status:
        query = query.where(Invoice.status == status)
    if source:
        query = query.where(Invoice.source == source)
    return query


def owned_invoice(request: Request, db: Session, user: User, invoice_id: str) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")
    if user.role != "admin" and invoice.owner_id != user.id:
        raise HTTPException(status_code=404, detail="发票不存在")
    return invoice


def owned_mailbox(request: Request, db: Session, user: User, mailbox_id: str) -> Mailbox:
    mailbox = db.get(Mailbox, mailbox_id)
    if not mailbox:
        raise HTTPException(status_code=404, detail="邮箱不存在")
    if user.role != "admin" and mailbox.created_by != user.id:
        raise HTTPException(status_code=404, detail="邮箱不存在")
    return mailbox


def sync_mailbox_task(mailbox_id: str) -> None:
    with SessionLocal() as db:
        mailbox = db.get(Mailbox, mailbox_id)
        if mailbox:
            sync_mailbox(db, mailbox)


@app.exception_handler(HTTPException)
async def friendly_http_errors(request: Request, exc: HTTPException):
    accepts_html = "text/html" in request.headers.get("accept", "")
    if exc.status_code == 401 and accepts_html:
        with SessionLocal() as db:
            if oidc_enabled(db) and oauth.oidc:
                return RedirectResponse(f"/auth/oidc/login?next={quote(request.url.path)}", status_code=303)
        return RedirectResponse(f"/admin?next={quote(request.url.path)}", status_code=303)
    if accepts_html and exc.status_code in {403, 404}:
        return templates.TemplateResponse(
            request, "error.html", context(request, title=str(exc.detail), status_code=exc.status_code), status_code=exc.status_code
        )
    return await http_exception_handler(request, exc)


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "version": __version__}


@app.get("/api/status")
def api_status(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    return {
        "status": "ok",
        "version": __version__,
        "user": user.username,
        "oidc": oidc_enabled(db),
        "mail_scheduler": scheduler.running,
    }


@app.get("/admin", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):  # noqa: A002
    if current_user(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        context(
            request,
            next_path=next if next.startswith("/") and not next.startswith("//") else "/",
            oidc_enabled=oidc_enabled(db),
            registration_enabled=settings.registration_enabled,
        ),
    )


@app.get("/login")
async def login_alias(request: Request, next: str = "/"):  # noqa: A002
    safe = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(f"/admin?next={quote(safe)}", status_code=303)


@app.post("/admin")
@app.post("/login")
async def login_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    ip = client_ip(request)
    username = str(form.get("username", "")).strip().lower()[:255]
    ip_limited = throttle_limit(f"login-ip:{ip}", 10, 900)
    account_limited = throttle_limit(f"login-account:{username}", 5, 900)
    if ip_limited or account_limited:
        record_audit(
            db,
            request,
            None,
            "auth.login_failed",
            details={"reason": "rate_limited"},
        )
        flash(request, "尝试次数过多，请 15 分钟后再试", "error")
        return RedirectResponse("/admin", status_code=303)
    password = str(form.get("password", ""))
    user = db.scalar(select(User).where(func.lower(User.username) == username))
    password_valid = (
        len(username) <= 255
        and len(password) <= settings.password_max_length
        and bool(user)
        and user.active
        and verify_password(password, user.password_hash)
    )
    if not password_valid:
        record_audit(
            db,
            request,
            user,
            "auth.login_failed",
            "user",
            str(user.id) if user else "",
            {"reason": "invalid_credentials"},
        )
        flash(request, "用户名或密码不正确", "error")
        return RedirectResponse("/admin", status_code=303)
    throttle_reset(f"login-ip:{ip}")
    throttle_reset(f"login-account:{username}")
    start_user_session(request, user)
    mark_login(user, db)
    record_audit(db, request, user, "auth.login")
    background_tasks.add_task(
        notify_event_background,
        "login",
        "InvoiceDock · 用户登录",
        f"账号：{_notification_user_label(user)}\n方式：邮箱 / 密码",
    )
    next_path = str(form.get("next", "/"))
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    return RedirectResponse(next_path, status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, next: str = "/", db: Session = Depends(get_db)):  # noqa: A002
    if not settings.registration_enabled:
        return RedirectResponse("/admin", status_code=303)
    if current_user(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "register.html",
        context(
            request,
            next_path=next if next.startswith("/") and not next.startswith("//") else "/",
            registration_enabled=settings.registration_enabled,
        ),
    )


@app.post("/register")
async def register_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if not settings.registration_enabled:
        raise HTTPException(status_code=404, detail="注册已关闭")
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    ip = client_ip(request)
    if throttle_limit(f"register:{ip}", 5, 3600):
        flash(request, "注册尝试过于频繁，请稍后再试", "error")
        return RedirectResponse("/register", status_code=303)
    email = str(form.get("email", "")).strip().lower()
    display_name = str(form.get("display_name", "")).strip()
    password = str(form.get("password", ""))
    confirm = str(form.get("password_confirm", ""))
    if len(email) > 255 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash(request, "请输入有效的邮箱地址", "error")
        return RedirectResponse("/register", status_code=303)
    if len(display_name) > 160:
        flash(request, "显示名称不能超过 160 个字符", "error")
        return RedirectResponse("/register", status_code=303)
    if is_reserved_username(email):
        flash(request, "该邮箱前缀为系统保留用户名，请更换邮箱", "error")
        return RedirectResponse("/register", status_code=303)
    if display_name and is_reserved_username(display_name):
        flash(request, "该显示名称为系统保留名称，请更换名称", "error")
        return RedirectResponse("/register", status_code=303)
    if len(password) > settings.password_max_length:
        flash(request, f"密码不能超过 {settings.password_max_length} 个字符", "error")
        return RedirectResponse("/register", status_code=303)
    password_error = password_policy_error(password, settings.registration_min_password_length)
    if password_error:
        flash(request, password_error, "error")
        return RedirectResponse("/register", status_code=303)
    if password != confirm:
        flash(request, "两次输入的密码不一致", "error")
        return RedirectResponse("/register", status_code=303)
    existing = db.scalar(
        select(User.id).where(or_(func.lower(User.username) == email, func.lower(User.email) == email))
    )
    if existing:
        flash(request, "该邮箱已注册，请直接登录", "error")
        return RedirectResponse("/register", status_code=303)
    user = User(
        username=email,
        email=email,
        display_name=display_name or email.rsplit("@", 1)[0],
        password_hash=hash_password(password),
        role="member",
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "该邮箱已注册，请直接登录", "error")
        return RedirectResponse("/register", status_code=303)
    record_audit(db, request, None, "auth.register", details={"username": email})
    background_tasks.add_task(
        notify_event_background,
        "register",
        "InvoiceDock · 新用户注册",
        f"账号：{_notification_user_label(user)}",
    )
    flash(request, "注册成功，请用邮箱和密码登录")
    return RedirectResponse("/admin", status_code=303)


class OIDCLoginDenied(Exception):
    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.message = message
        self.reason = reason


def _oidc_email(claims: dict) -> str:
    verified = claims.get("email_verified") is True or (
        isinstance(claims.get("email_verified"), str)
        and claims["email_verified"].strip().lower() == "true"
    )
    candidate = str(claims.get("email") or "").strip().lower()
    if (
        not verified
        or len(candidate) > 255
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate)
    ):
        return ""
    return candidate


def _oidc_groups(claims: dict) -> list[str]:
    value = claims.get(settings.oidc_group_claim, []) or []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value] if isinstance(value, (list, tuple, set)) else []


def _oidc_user_for_claims(db: Session, claims: dict) -> tuple[User, bool]:
    """Resolve an OIDC identity without ever linking it by email.

    The issuer + subject pair is the sole binding key. A verified email is
    retained as an attribute and checked for collisions, but an existing local
    account must be linked explicitly by an administrator outside this flow.
    """
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise OIDCLoginDenied("OIDC 未返回有效的身份标识", "missing_subject")
    oidc_subject = f"{settings.oidc_issuer}|{subject}"
    verified_email = _oidc_email(claims)
    if settings.oidc_domains:
        if not verified_email:
            raise OIDCLoginDenied("身份提供商尚未验证邮箱，无法完成登录", "email_unverified")
        if verified_email.rsplit("@", 1)[-1] not in settings.oidc_domains:
            raise OIDCLoginDenied("该邮箱域名未获授权", "domain_not_allowed")

    user = db.scalar(select(User).where(User.oidc_subject == oidc_subject))
    if verified_email:
        collision_query = select(User).where(
            or_(
                func.lower(User.email) == verified_email,
                func.lower(User.username) == verified_email,
            )
        )
        if user:
            collision_query = collision_query.where(User.id != user.id)
        collision = db.scalar(collision_query)
        if collision:
            raise OIDCLoginDenied(
                "该邮箱已属于本地账号，系统不会自动绑定；请联系管理员显式处理",
                "local_email_conflict",
            )

    groups = _oidc_groups(claims)
    desired_role = (
        "admin"
        if settings.oidc_admin_group and settings.oidc_admin_group in groups
        else "member"
    )
    if user:
        if not user.active:
            raise OIDCLoginDenied("该账号已停用，请联系管理员", "inactive")
        security_state_changed = False
        if user.role != desired_role:
            user.role = desired_role
            security_state_changed = True
        # OIDC-bound users are OIDC-only. Clearing a legacy local password
        # closes accounts silently bound by older vulnerable releases.
        if user.password_hash:
            user.password_hash = None
            security_state_changed = True
        if security_state_changed:
            rotate_user_sessions(user)
        if verified_email:
            user.email = verified_email
        display_name = str(
            claims.get("name") or claims.get("preferred_username") or user.display_name
        ).strip()
        if display_name:
            user.display_name = display_name[:160]
        db.commit()
        return user, False

    preferred = str(claims.get("preferred_username") or "").strip()
    raw_email = str(claims.get("email") or "").strip().lower()
    if not verified_email and ("@" in preferred or preferred.casefold() == raw_email.casefold()):
        preferred = ""
    display_name = str(claims.get("name") or preferred or "").strip()
    for value in (preferred, verified_email, display_name):
        if value and is_reserved_username(value):
            raise OIDCLoginDenied(
                "OIDC 返回了系统保留名称，请联系管理员处理",
                "reserved_username",
            )

    username_base = (preferred or verified_email or f"oidc-{secrets.token_hex(6)}")[:120]
    username = username_base
    suffix = 1
    while db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
        suffix += 1
        username = f"{username_base[: 119 - len(str(suffix))]}-{suffix}"
    user = User(
        username=username,
        email=verified_email,
        display_name=(display_name or username)[:160],
        oidc_subject=oidc_subject,
        role=desired_role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise OIDCLoginDenied(
            "OIDC 账号创建发生冲突，请联系管理员处理",
            "identity_conflict",
        ) from exc
    return user, True


@app.get("/auth/oidc/login")
async def oidc_login(request: Request, db: Session = Depends(get_db)):
    if not oidc_enabled(db) or not oauth.oidc:
        raise HTTPException(status_code=404, detail="OIDC 未启用")
    redirect_uri = f"{settings.app_base_url}/auth/oidc/callback"
    next_path = str(request.query_params.get("next", "/"))
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    response = await oauth.oidc.authorize_redirect(request, redirect_uri)
    match = re.search(r"state=([^&]+)", response.headers.get("location", ""))
    if match:
        with SessionLocal() as db:
            row = db.get(OAuthState, match.group(1))
            if row:
                row.data = {**row.data, "next": next_path}
                db.commit()
    return response


@app.get("/auth/oidc/callback")
async def oidc_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if not oidc_enabled(db) or not oauth.oidc:
        raise HTTPException(status_code=404, detail="OIDC 未启用")
    state = str(request.query_params.get("state", ""))
    next_path = "/"
    if state:
        row = db.get(OAuthState, state)
        if row:
            next_path = str(row.data.get("next", "/"))
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = "/"
    try:
        token = await oauth.oidc.authorize_access_token(request)
        claims = dict(token.get("userinfo") or {})
        userinfo = await _oidc_userinfo(token)
        if userinfo:
            claims.update(userinfo)
    except OAuthError as exc:
        record_audit(
            db,
            request,
            None,
            "auth.oidc_login_failed",
            details={"reason": "provider_error"},
        )
        flash(request, f"OIDC 登录失败：{exc.error}", "error")
        return RedirectResponse("/admin", status_code=303)
    try:
        user, created_user = _oidc_user_for_claims(db, claims)
    except OIDCLoginDenied as exc:
        record_audit(
            db,
            request,
            None,
            "auth.oidc_login_failed",
            details={"reason": exc.reason},
        )
        flash(request, exc.message, "error")
        return RedirectResponse("/admin", status_code=303)
    start_user_session(request, user)
    mark_login(user, db)
    record_audit(db, request, user, "auth.oidc_login")
    if created_user:
        background_tasks.add_task(
            notify_event_background,
            "register",
            "InvoiceDock · 新用户注册",
            f"账号：{_notification_user_label(user)}\n来源：OIDC",
        )
    background_tasks.add_task(
        notify_event_background,
        "login",
        "InvoiceDock · 用户登录",
        f"账号：{_notification_user_label(user)}\n方式：OIDC",
    )
    return RedirectResponse(next_path, status_code=303)


@app.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    user = current_user(request, db)
    if user:
        record_audit(db, request, user, "auth.logout")
    request.session.clear()
    return RedirectResponse("/admin", status_code=303)


@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    return templates.TemplateResponse(
        request,
        "profile.html",
        context(request, user, page="profile"),
    )


@app.post("/profile/password")
async def change_password(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    current_password = str(form.get("current_password", ""))
    new_password = str(form.get("new_password", ""))
    confirm_password = str(form.get("confirm_password", ""))
    if (
        len(current_password) > settings.password_max_length
        or not user.password_hash
        or not verify_password(current_password, user.password_hash)
    ):
        record_audit(db, request, user, "auth.password_change_failed", "user", str(user.id))
        flash(request, "当前密码不正确", "error")
        return RedirectResponse("/profile", status_code=303)
    if len(new_password) > settings.password_max_length:
        flash(request, f"新密码不能超过 {settings.password_max_length} 个字符", "error")
        return RedirectResponse("/profile", status_code=303)
    password_error = password_policy_error(new_password, settings.registration_min_password_length)
    if password_error:
        flash(request, password_error, "error")
        return RedirectResponse("/profile", status_code=303)
    if new_password != confirm_password:
        flash(request, "两次输入的新密码不一致", "error")
        return RedirectResponse("/profile", status_code=303)
    if verify_password(new_password, user.password_hash):
        flash(request, "新密码不能与当前密码相同", "error")
        return RedirectResponse("/profile", status_code=303)
    user.password_hash = hash_password(new_password)
    rotate_user_sessions(user)
    db.commit()
    record_audit(db, request, user, "auth.password_changed", "user", str(user.id))
    start_user_session(request, user)
    flash(request, "密码已更新")
    return RedirectResponse("/profile", status_code=303)


def _dashboard_job_logs(db: Session, user: User) -> list[JobLog]:
    query = select(JobLog)
    if user.role != "admin":
        query = query.where(JobLog.user_id == user.id)
    return list(db.scalars(query.order_by(JobLog.created_at.desc()).limit(7)).all())


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    owned = Invoice.owner_id == user.id if user.role != "admin" else None
    total = db.scalar(select(func.count()).select_from(Invoice).where(owned)) or 0
    total_amount = db.scalar(select(func.coalesce(func.sum(Invoice.total_amount), 0.0)).where(owned)) or 0.0
    verified = db.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.status.in_(["verified", "consistent", "reviewed"]), owned)
    ) or 0
    review = db.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.status.in_(["review", "failed", "duplicate"]), owned)
    ) or 0
    email_count = db.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.source.in_(["email", "email-link"]), owned)
    ) or 0
    recent = list(db.scalars(select(Invoice).where(owned).order_by(Invoice.created_at.desc()).limit(8)).all())
    logs = _dashboard_job_logs(db, user)
    by_category = list(
        db.execute(
            select(Invoice.category, func.count(Invoice.id), func.coalesce(func.sum(Invoice.total_amount), 0.0))
            .where(owned)
            .group_by(Invoice.category)
            .order_by(func.sum(Invoice.total_amount).desc())
            .limit(6)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context(
            request,
            user,
            page="dashboard",
            total=total,
            total_amount=total_amount,
            verified=verified,
            review=review,
            email_count=email_count,
            recent=recent,
            logs=logs,
            by_category=by_category,
        ),
    )


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(
    request: Request,
    q: str = "",
    status: str = "",
    source: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    user = require_page_user(request, db)
    page = max(page, 1)
    per_page = 25
    query = invoice_query(q, status, source, user)
    count = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    items = list(db.scalars(query.order_by(Invoice.created_at.desc()).offset((page - 1) * per_page).limit(per_page)).all())
    pages = max(1, (count + per_page - 1) // per_page)
    return templates.TemplateResponse(
        request,
        "invoices.html",
        context(request, user, page="invoices", items=items, q=q, filter_status=status, source=source, current_page=page, pages=pages, count=count),
    )


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    invoice = owned_invoice(request, db, user, invoice_id)
    duplicate = db.get(Invoice, invoice.duplicate_of) if invoice.duplicate_of else None
    if duplicate and user.role != "admin" and duplicate.owner_id != user.id:
        duplicate = None
    return templates.TemplateResponse(
        request,
        "invoice_detail.html",
        context(request, user, page="invoices", invoice=invoice, duplicate=duplicate, field_names={
            "invoice_type": "发票类型", "invoice_code": "发票代码", "invoice_number": "发票号码", "invoice_date": "开票日期",
            "check_code": "校验码", "seller_name": "销售方", "seller_tax_id": "销售方税号", "buyer_name": "购买方",
            "buyer_tax_id": "购买方税号", "amount": "不含税金额", "tax_amount": "税额", "total_amount": "价税合计", "category": "分类",
        }),
    )


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    return templates.TemplateResponse(request, "upload.html", context(request, user, page="upload", max_mb=settings.max_upload_mb))


@app.post("/upload")
async def upload_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_page_user(request, db)
    validate_csrf(request, csrf)
    created_ids: list[str] = []
    duplicates = 0
    errors: list[str] = []
    limit = settings.max_upload_mb * 1024 * 1024
    for upload in files[:50]:
        data = await upload.read(limit + 1)
        name = upload.filename or "invoice"
        if len(data) > limit:
            errors.append(f"{name} 超过 {settings.max_upload_mb} MB")
            continue
        candidates = [(name, data)]
        if Path(name).suffix.lower() == ".zip":
            try:
                candidates = extract_zip_candidates(data)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
        for candidate_name, candidate_data in candidates:
            try:
                invoice, created = ingest_bytes(db, candidate_data, candidate_name, owner_id=user.id)
                if created:
                    created_ids.append(invoice.id)
                else:
                    duplicates += 1
            except Exception as exc:
                errors.append(f"{candidate_name}: {exc}")
    for invoice_id in created_ids:
        background_tasks.add_task(process_invoice, invoice_id)
    record_audit(db, request, user, "invoice.upload", "invoice", details={"created": len(created_ids), "duplicates": duplicates})
    background_tasks.add_task(
        notify_event_background,
        "usage",
        "InvoiceDock · 发票上传",
        f"用户：{_notification_user_label(user)}\n新增：{len(created_ids)} 张；重复：{duplicates} 张",
    )
    if errors and not created_ids:
        flash(request, "；".join(errors[:3]), "error")
    else:
        message = f"已接收 {len(created_ids)} 张发票，正在后台查验"
        if duplicates:
            message += f"；跳过 {duplicates} 个相同文件"
        if errors:
            message += f"；{len(errors)} 个文件未导入"
        flash(request, message, "success")
    return RedirectResponse("/invoices", status_code=303)


@app.post("/invoices/{invoice_id}/process")
async def reprocess_invoice(invoice_id: str, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    invoice = owned_invoice(request, db, user, invoice_id)
    invoice.status = "pending"
    db.commit()
    background_tasks.add_task(process_invoice, invoice.id)
    record_audit(db, request, user, "invoice.reprocess", "invoice", invoice.id)
    flash(request, "已加入重新查验队列")
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)


@app.post("/invoices/{invoice_id}/save")
async def save_invoice(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    invoice = owned_invoice(request, db, user, invoice_id)
    provider_verified = invoice.verification_method in ("kingdee", "piaozone")
    if provider_verified:
        invoice.category = str(form.get("category", "")).strip() or "未分类"
        invoice.notes = str(form.get("notes", "")).strip()
    else:
        text_fields = [
            "invoice_type", "invoice_code", "invoice_number", "invoice_date", "check_code", "seller_name", "seller_tax_id",
            "buyer_name", "buyer_tax_id", "category", "notes",
        ]
        for field in text_fields:
            if field in form:
                setattr(invoice, field, str(form.get(field, "")).strip())
        for field in ("amount", "tax_amount", "total_amount"):
            raw = str(form.get(field, "")).strip()
            try:
                value = float(raw) if raw else None
            except ValueError:
                flash(request, "金额字段必须是有效数字", "error")
                return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)
            if value is not None and (not math.isfinite(value) or abs(value) > 1_000_000_000):
                flash(request, "金额超出有效范围", "error")
                return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)
            setattr(invoice, field, round(value, 2) if value is not None else None)
    invoice.status = "reviewed"
    invoice.verified_at = utcnow()
    db.commit()
    record_audit(db, request, user, "invoice.review", "invoice", invoice.id)
    flash(request, "人工复核结果已保存")
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)


@app.post("/invoices/{invoice_id}/delete")
async def delete_invoice(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    invoice = owned_invoice(request, db, user, invoice_id)
    original = settings.upload_dir / invoice.stored_name
    preview = settings.preview_dir / f"{invoice.id}.jpg"
    record_audit(db, request, user, "invoice.delete", "invoice", invoice.id, {"filename": invoice.original_name})
    for child in db.scalars(select(Invoice).where(Invoice.duplicate_of == invoice.id)).all():
        child.duplicate_of = None
        child.status = "verified" if child.verified_at else "review"
    db.delete(invoice)
    db.commit()
    original.unlink(missing_ok=True)
    preview.unlink(missing_ok=True)
    flash(request, "发票及其本地文件已删除")
    return RedirectResponse("/invoices", status_code=303)


@app.post("/invoices/batch-delete")
async def invoices_batch_delete(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    ids = [str(value) for value in form.getlist("invoice_ids")]
    query = select(Invoice).where(Invoice.id.in_(ids))
    if user.role != "admin":
        query = query.where(Invoice.owner_id == user.id)
    invoices = list(db.scalars(query).all())
    if not invoices:
        flash(request, "未找到可删除的发票", "error")
        return RedirectResponse("/invoices", status_code=303)
    selected = {invoice.id for invoice in invoices}
    children = db.scalars(select(Invoice).where(Invoice.duplicate_of.in_(selected))).all()
    for child in children:
        if child.id not in selected:
            child.duplicate_of = None
            child.status = "verified" if child.verified_at else "review"
    for invoice in invoices:
        original = settings.upload_dir / invoice.stored_name
        preview = settings.preview_dir / f"{invoice.id}.jpg"
        record_audit(db, request, user, "invoice.delete", "invoice", invoice.id, {"filename": invoice.original_name})
        db.delete(invoice)
        original.unlink(missing_ok=True)
        preview.unlink(missing_ok=True)
    db.commit()
    flash(request, f"已删除 {len(invoices)} 张发票及其本地文件")
    return RedirectResponse("/invoices", status_code=303)


@app.get("/export/files")
def export_invoice_files(
    request: Request,
    background_tasks: BackgroundTasks,
    ids: str = "",
    db: Session = Depends(get_db),
):
    user = require_page_user(request, db)
    selected_ids = [item for item in ids.split(",") if item]
    query = select(Invoice).where(Invoice.id.in_(selected_ids))
    if user.role != "admin":
        query = query.where(Invoice.owner_id == user.id)
    invoices = list(db.scalars(query).all())
    if not invoices:
        raise HTTPException(status_code=404, detail="没有可导出的发票")
    if len(invoices) > MAX_EXPORT_FILES:
        raise HTTPException(status_code=400, detail=f"单次最多导出 {MAX_EXPORT_FILES} 个文件")
    export_size = sum(max(0, invoice.file_size) for invoice in invoices)
    if export_size > MAX_EXPORT_BYTES:
        raise HTTPException(status_code=400, detail="单次导出文件总量不能超过 512 MB")
    buffer = BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for invoice in invoices:
            path = settings.upload_dir / invoice.stored_name
            if not path.exists():
                continue
            category = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", invoice.category or "未分类").strip() or "未分类"
            filename = invoice.original_name or invoice.stored_name
            arcname = f"{category}/{filename}"
            if arcname in used:
                base, ext = Path(filename).stem, Path(filename).suffix
                index = 2
                while f"{category}/{base}-{index}{ext}" in used:
                    index += 1
                arcname = f"{category}/{base}-{index}{ext}"
            used.add(arcname)
            archive.write(path, arcname)
    data = buffer.getvalue()
    if not used:
        raise HTTPException(status_code=404, detail="所选发票的原始文件不存在")
    record_audit(db, request, user, "invoice.export_files", details={"count": len(used)})
    background_tasks.add_task(
        notify_event_background,
        "usage",
        "InvoiceDock · 导出原始文件",
        f"用户：{_notification_user_label(user)}\n数量：{len(used)} 个",
    )
    filename = f"invoices-by-category-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/files/{invoice_id}/original")
def invoice_file(invoice_id: str, request: Request, download: bool = False, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    invoice = owned_invoice(request, db, user, invoice_id)
    path = settings.upload_dir / invoice.stored_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="原文件已丢失")
    disposition = "attachment" if download else "inline"
    return FileResponse(path, media_type=invoice.mime_type, filename=invoice.original_name, content_disposition_type=disposition)


@app.get("/files/{invoice_id}/preview")
def invoice_preview(invoice_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    invoice = owned_invoice(request, db, user, invoice_id)
    path = make_preview(invoice)
    if not path:
        raise HTTPException(status_code=404, detail="该格式暂无缩略图")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/mailboxes", response_class=HTMLResponse)
def mailboxes_page(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    query = select(Mailbox)
    if user.role != "admin":
        query = query.where(Mailbox.created_by == user.id)
    items = list(db.scalars(query.order_by(Mailbox.created_at.desc())).all())
    return templates.TemplateResponse(request, "mailboxes.html", context(request, user, page="mailboxes", items=items))


@app.post("/mailboxes")
async def create_mailbox(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    password = str(form.get("password", ""))
    if not password or len(password) > 1024:
        flash(request, "邮箱授权码不能为空且不能超过 1024 个字符", "error")
        return RedirectResponse("/mailboxes", status_code=303)
    name = str(form.get("name", "")).strip()[:120]
    host = str(form.get("host", "")).strip()[:255]
    username = str(form.get("username", "")).strip()[:255]
    folder = str(form.get("folder", "INBOX")).strip()[:255] or "INBOX"
    try:
        port = int(str(form.get("port", "993")))
        if port not in {143, 993}:
            raise ValueError("IMAP 端口仅允许 143 或 993")
        validate_outbound_host(host, port)
    except ValueError as exc:
        flash(request, f"邮箱服务器配置无效：{exc}", "error")
        return RedirectResponse("/mailboxes", status_code=303)
    if not name or not username:
        flash(request, "邮箱名称和账号不能为空", "error")
        return RedirectResponse("/mailboxes", status_code=303)
    mailbox = Mailbox(
        name=name,
        host=host,
        port=port,
        username=username,
        password_encrypted=encrypt_secret(password),
        folder=folder,
        use_ssl=str(form.get("use_ssl", "")) == "on",
        enabled=True,
        created_by=user.id,
    )
    db.add(mailbox)
    db.commit()
    record_audit(db, request, user, "mailbox.create", "mailbox", mailbox.id)
    flash(request, "邮箱已保存，可先测试连接再手动收取")
    return RedirectResponse("/mailboxes", status_code=303)


@app.post("/mailboxes/{mailbox_id}/test")
async def mailbox_test(mailbox_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    mailbox = owned_mailbox(request, db, user, mailbox_id)
    if throttle_limit(f"mailbox-test:{user.id}", 10, 900):
        flash(request, "邮箱连接测试过于频繁，请稍后再试", "error")
        return RedirectResponse("/mailboxes", status_code=303)
    try:
        result = test_mailbox(mailbox)
        flash(request, f"连接成功：{result}")
        record_audit(db, request, user, "mailbox.test", "mailbox", mailbox.id, {"success": True})
    except Exception as exc:
        flash(request, f"连接失败：{exc}", "error")
    return RedirectResponse("/mailboxes", status_code=303)


@app.post("/mailboxes/{mailbox_id}/sync")
async def mailbox_sync(mailbox_id: str, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    mailbox = owned_mailbox(request, db, user, mailbox_id)
    if throttle_limit(f"mailbox-sync:{user.id}", 10, 900):
        flash(request, "邮箱收取过于频繁，请稍后再试", "error")
        return RedirectResponse("/mailboxes", status_code=303)
    background_tasks.add_task(sync_mailbox_task, mailbox_id)
    background_tasks.add_task(
        notify_event_background,
        "usage",
        "InvoiceDock · 邮箱收票",
        f"用户：{_notification_user_label(user)}\n邮箱：{mailbox.name}\n操作：立即收取",
    )
    record_audit(db, request, user, "mailbox.sync", "mailbox", mailbox_id)
    flash(request, "已开始收取邮件，结果会显示在运行记录中")
    return RedirectResponse("/mailboxes", status_code=303)


@app.post("/mailboxes/{mailbox_id}/toggle")
async def mailbox_toggle(mailbox_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    mailbox = owned_mailbox(request, db, user, mailbox_id)
    mailbox.enabled = not mailbox.enabled
    db.commit()
    record_audit(db, request, user, "mailbox.toggle", "mailbox", mailbox.id, {"enabled": mailbox.enabled})
    flash(request, "自动收取已" + ("启用" if mailbox.enabled else "暂停"))
    return RedirectResponse("/mailboxes", status_code=303)


@app.post("/mailboxes/{mailbox_id}/delete")
async def mailbox_delete(mailbox_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    mailbox = owned_mailbox(request, db, user, mailbox_id)
    record_audit(db, request, user, "mailbox.delete", "mailbox", mailbox.id, {"name": mailbox.name})
    db.delete(mailbox)
    db.commit()
    flash(request, "邮箱配置已删除，已导入发票不受影响")
    return RedirectResponse("/mailboxes", status_code=303)


@app.get("/integrations", response_class=HTMLResponse)
def integrations_page(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    is_admin = user.role == "admin"
    values = get_integrations(db, user_id=None if is_admin else user.id, mask_secrets=True)
    own_values = get_integrations(db, user_id=user.id, mask_secrets=True) if not is_admin else {}
    custom = user_custom_integrations(db, user.id) if not is_admin else set()
    all_env_keys = get_env_keys()
    env_keys = all_env_keys if is_admin else all_env_keys.intersection(INTEGRATION_KEYS["llm"])
    if is_admin:
        field_values = values
    else:
        field_values = {}
        for integration in USER_CONFIGURABLE_INTEGRATIONS:
            keys = INTEGRATION_KEYS[integration]
            for key in keys:
                field_values[key] = own_values.get(key, "") if integration in custom else ""
    mode_parts = []
    if as_bool(values.get("verify_provider", "true")):
        mode_parts.append("发票云")
    if as_bool(values.get("verify_ocr", "true")):
        mode_parts.append("本地 OCR")
    if as_bool(values.get("verify_llm", "true")):
        mode_parts.append("LLM 双源复核")
    verify_mode_text = " + ".join(mode_parts) if mode_parts else "未启用任何查验方式"
    bark = get_notification_settings(db, mask_secret=True) if is_admin else {}
    tax_verify_daily_limit = get_tax_verify_daily_limit(db) if is_admin else 0
    user_tax_verify_enabled = get_user_tax_verify_enabled(db, user.id) if not is_admin else True
    return templates.TemplateResponse(
        request,
        "integrations.html",
        context(request, user, page="integrations", values=values, field_values=field_values,
                custom=custom, env_keys=env_keys, is_admin=is_admin, verify_mode_text=verify_mode_text,
                bark=bark, tax_verify_daily_limit=tax_verify_daily_limit,
                user_tax_verify_enabled=user_tax_verify_enabled, oidc={
            "enabled": oidc_enabled(db), "toggle": as_bool(get_value(db, OIDC_TOGGLE_KEY, "true" if settings.oidc_enabled else "false")),
            "issuer": settings.oidc_issuer, "client_id": settings.oidc_client_id,
            "callback": f"{settings.app_base_url}/auth/oidc/callback",
        }),
    )


@app.post("/integrations/tax-verification-limit")
async def integrations_tax_verification_limit(request: Request, db: Session = Depends(get_db)):
    user = require_page_admin(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    try:
        limit = set_tax_verify_daily_limit(db, str(form.get("tax_verify_daily_limit", "")))
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/integrations", status_code=303)
    record_audit(
        db,
        request,
        user,
        "integrations.update",
        "tax_verification_limit",
        details={"daily_limit": limit},
    )
    flash(request, f"每个用户每日税务验票上限已设为 {limit} 次")
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations/oidc")
async def integrations_oidc_toggle(request: Request, db: Session = Depends(get_db)):
    user = require_page_admin(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    enabled = form.get("oidc_enabled") == "1"
    set_value(db, OIDC_TOGGLE_KEY, "true" if enabled else "false")
    record_audit(db, request, user, "integrations.update", "oidc", "", {"enabled": enabled})
    flash(request, f"OIDC 登录已{'启用' if enabled else '关闭'}")
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations/bark")
async def integrations_bark_save(request: Request, db: Session = Depends(get_db)):
    user = require_page_admin(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    try:
        save_notification_settings(
            db,
            bark_url=str(form.get("bark_url", "")),
            enabled=form.get("bark_enabled") == "on",
            register=form.get("bark_notify_register") == "on",
            login=form.get("bark_notify_login") == "on",
            usage=form.get("bark_notify_usage") == "on",
        )
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/integrations", status_code=303)
    record_audit(
        db,
        request,
        user,
        "integrations.update",
        "bark",
        details={
            "enabled": form.get("bark_enabled") == "on",
            "events": [
                event
                for event in ("register", "login", "usage")
                if form.get(f"bark_notify_{event}") == "on"
            ],
        },
    )
    flash(request, "Bark 推送设置已加密保存")
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations/bark/test")
async def integrations_bark_test(request: Request, db: Session = Depends(get_db)):
    user = require_page_admin(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    try:
        test_bark_notification(db)
        flash(request, "Bark 测试消息已发送")
        record_audit(db, request, user, "integrations.test", "bark", details={"success": True})
    except Exception as exc:
        flash(request, f"Bark 测试失败：{exc}", "error")
        record_audit(db, request, user, "integrations.test", "bark", details={"success": False})
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations")
async def integrations_save(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    if user.role == "admin":
        values = {key: str(value) for key, value in form.items() if key != "csrf_token"}
        values["kingdee_enabled"] = "true" if form.get("kingdee_enabled") == "on" else "false"
        values["piaozone_enabled"] = "true" if form.get("piaozone_enabled") == "on" else "false"
        values["llm_enabled"] = "true" if form.get("llm_enabled") == "on" else "false"
        values["llm_vision"] = "true" if form.get("llm_vision") == "on" else "false"
        values["verify_provider"] = "true" if form.get("verify_provider") == "on" else "false"
        values["verify_ocr"] = "true" if form.get("verify_ocr") == "on" else "false"
        values["verify_llm"] = "true" if form.get("verify_llm") == "on" else "false"
        update_integrations(db, values)
        record_audit(db, request, user, "integrations.update", details={"keys": sorted(values)})
        flash(request, "全局集成配置已加密保存")
    else:
        tax_verify_enabled = form.get("tax_verify_enabled") == "on"
        set_user_tax_verify_enabled(db, user.id, tax_verify_enabled)
        record_audit(
            db,
            request,
            user,
            "integrations.update",
            "user",
            user.id,
            {"tax_verify_enabled": tax_verify_enabled},
        )
        for integration in USER_CONFIGURABLE_INTEGRATIONS:
            keys = INTEGRATION_KEYS[integration]
            if str(form.get(f"{integration}_custom", "")) != "1":
                clear_user_integration(db, user.id, integration)
                continue
            values = {key: str(form.get(key, "")) for key in keys}
            values[f"{integration}_enabled"] = "true" if form.get(f"{integration}_enabled") == "on" else "false"
            if integration == "llm":
                values["llm_vision"] = "true" if form.get("llm_vision") == "on" else "false"
            update_integrations(db, values, user_id=user.id)
            record_audit(db, request, user, "integrations.update", "user", user.id,
                         {"integration": integration, "keys": sorted(values)})
        flash(request, "个人集成配置已保存；未自定义的集成回退到管理员配置")
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations/test/{provider}")
async def integration_test(provider: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    if throttle_limit(f"integration-test:{user.id}", 10, 900):
        flash(request, "集成测试过于频繁，请稍后再试", "error")
        return RedirectResponse("/integrations", status_code=303)
    if user.role != "admin" and provider != "llm":
        raise HTTPException(status_code=403, detail="普通用户只能测试自己的 LLM 配置")
    config = get_integrations(db, user_id=None if user.role == "admin" else user.id)
    try:
        result = (
            test_kingdee(config) if provider == "kingdee"
            else test_piaozone(config) if provider == "piaozone"
            else test_llm(config) if provider == "llm"
            else None
        )
        if result is None:
            raise ValueError("未知集成")
        flash(request, result)
        record_audit(db, request, user, "integrations.test", provider, details={"success": True})
    except Exception as exc:
        flash(request, f"测试失败：{exc}", "error")
    return RedirectResponse("/integrations", status_code=303)


@app.get("/titles", response_class=HTMLResponse)
def titles_page(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    return templates.TemplateResponse(
        request,
        "titles.html",
        context(request, user, page="titles", presets=env_presets(), items=user_titles(db, user.id)),
    )


@app.post("/titles")
async def titles_add(request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    name = str(form.get("name", "")).strip()
    if not name:
        flash(request, "单位名称不能为空", "error")
        return RedirectResponse("/titles", status_code=303)
    title = UserTitle(
        user_id=user.id,
        name=name,
        tax_id=str(form.get("tax_id", "")).strip(),
        address=str(form.get("address", "")).strip(),
        phone=str(form.get("phone", "")).strip(),
        bank_name=str(form.get("bank_name", "")).strip(),
        bank_account=str(form.get("bank_account", "")).strip(),
        bank_code=str(form.get("bank_code", "")).strip(),
    )
    db.add(title)
    db.commit()
    record_audit(db, request, user, "title.add", "user_title", str(title.id), {"name": name})
    flash(request, "收票抬头已新增")
    return RedirectResponse("/titles", status_code=303)


@app.post("/titles/{title_id}/delete")
async def titles_delete(title_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    title = db.get(UserTitle, title_id)
    if not title or title.user_id != user.id:
        raise HTTPException(status_code=404, detail="抬头不存在")
    record_audit(db, request, user, "title.delete", "user_title", title_id, {"name": title.name})
    db.delete(title)
    db.commit()
    flash(request, "收票抬头已删除")
    return RedirectResponse("/titles", status_code=303)


@app.get("/print", response_class=HTMLResponse)
def print_page(request: Request, ids: str = "", db: Session = Depends(get_db)):
    user = require_page_user(request, db)
    selected_ids = [item for item in ids.split(",") if item]
    query = select(Invoice).where(Invoice.mime_type.in_(["application/pdf", "image/png", "image/jpeg"]))
    if user.role != "admin":
        query = query.where(Invoice.owner_id == user.id)
    if selected_ids:
        query = query.where(Invoice.id.in_(selected_ids))
    items = list(db.scalars(query.order_by(Invoice.created_at.desc()).limit(100)).all())
    return templates.TemplateResponse(request, "print.html", context(request, user, page="print", items=items, selected_ids=set(selected_ids)))


@app.post("/print/generate")
async def print_generate(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = require_page_user(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    ids = [str(value) for value in form.getlist("invoice_ids")]
    try:
        per_page = int(str(form.get("per_page", "2")))
    except ValueError:
        per_page = 0
    if per_page not in {1, 2, 4}:
        flash(request, "每页数量只支持 1、2 或 4", "error")
        return RedirectResponse("/print", status_code=303)
    query = select(Invoice).where(Invoice.id.in_(ids))
    if user.role != "admin":
        query = query.where(Invoice.owner_id == user.id)
    items = list(db.scalars(query).all())
    order = {value: index for index, value in enumerate(ids)}
    items.sort(key=lambda item: order.get(item.id, 9999))
    try:
        output = make_print_pdf(items, per_page)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/print", status_code=303)
    record_audit(db, request, user, "print.generate", details={"count": len(items), "per_page": per_page})
    background_tasks.add_task(
        notify_event_background,
        "usage",
        "InvoiceDock · 生成打印 PDF",
        f"用户：{_notification_user_label(user)}\n数量：{len(items)} 张；版式：每页 {per_page} 张",
    )
    filename = f"invoices-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.pdf"
    return Response(output, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/export.xlsx")
def export_excel(
    request: Request,
    background_tasks: BackgroundTasks,
    q: str = "",
    status: str = "",
    source: str = "",
    db: Session = Depends(get_db),
):
    user = require_page_user(request, db)
    items = list(db.scalars(invoice_query(q, status, source, user).order_by(Invoice.created_at.desc()).limit(10000)).all())
    output = make_invoice_workbook(items)
    record_audit(db, request, user, "invoice.export", details={"count": len(items)})
    background_tasks.add_task(
        notify_event_background,
        "usage",
        "InvoiceDock · 导出发票台账",
        f"用户：{_notification_user_label(user)}\n数量：{len(items)} 张",
    )
    filename = f"invoice-ledger-{datetime.now(UTC).strftime('%Y%m%d')}.xlsx"
    return Response(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    user = require_page_admin(request, db)
    items = list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)).all())
    users = {item.id: item for item in db.scalars(select(User)).all()}
    return templates.TemplateResponse(request, "audit.html", context(request, user, page="audit", items=items, users=users))


def _admin_target(db: Session, user_id: str) -> User:
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    return target


def _active_admin_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(User).where(
                User.role == "admin",
                User.active.is_(True),
            )
        )
        or 0
    )


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, db: Session = Depends(get_db)):
    user = require_page_admin(request, db)
    users = list(db.scalars(select(User).order_by(User.created_at.desc())).all())
    daily_limit = get_tax_verify_daily_limit(db)
    usage = {item.id: get_tax_verify_usage(db, item.id) for item in users}
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        context(
            request,
            user,
            page="admin_users",
            users=users,
            usage=usage,
            daily_limit=daily_limit,
            password_max_length=settings.password_max_length,
        ),
    )


@app.post("/admin/users/{user_id}/active")
async def admin_user_toggle_active(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_page_admin(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    target = _admin_target(db, user_id)
    if target.id == actor.id:
        flash(request, "不能停用自己的管理员账号", "error")
        return RedirectResponse("/admin/users", status_code=303)
    if target.active and target.role == "admin" and _active_admin_count(db) <= 1:
        flash(request, "必须保留至少一个可用管理员", "error")
        return RedirectResponse("/admin/users", status_code=303)
    target.active = not target.active
    rotate_user_sessions(target)
    db.commit()
    record_audit(
        db,
        request,
        actor,
        "user.active_changed",
        "user",
        target.id,
        {"active": target.active},
    )
    flash(request, f"已{'启用' if target.active else '停用'}用户 {target.username}")
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/role")
async def admin_user_change_role(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_page_admin(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    target = _admin_target(db, user_id)
    desired_role = str(form.get("role", ""))
    if desired_role not in {"admin", "member"}:
        raise HTTPException(status_code=400, detail="无效的用户角色")
    if target.id == actor.id:
        flash(request, "不能修改自己的管理员角色", "error")
        return RedirectResponse("/admin/users", status_code=303)
    if target.oidc_subject:
        flash(request, "OIDC 用户角色由身份提供商的管理员组同步", "error")
        return RedirectResponse("/admin/users", status_code=303)
    if (
        target.role == "admin"
        and desired_role == "member"
        and target.active
        and _active_admin_count(db) <= 1
    ):
        flash(request, "必须保留至少一个可用管理员", "error")
        return RedirectResponse("/admin/users", status_code=303)
    previous_role = target.role
    if previous_role != desired_role:
        target.role = desired_role
        rotate_user_sessions(target)
        db.commit()
        record_audit(
            db,
            request,
            actor,
            "user.role_changed",
            "user",
            target.id,
            {"from": previous_role, "to": desired_role},
        )
    flash(request, f"已将 {target.username} 设为{'管理员' if desired_role == 'admin' else '成员'}")
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/password")
async def admin_user_reset_password(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_page_admin(request, db)
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    target = _admin_target(db, user_id)
    if target.id == actor.id:
        flash(request, "请在“账户安全”页面修改自己的密码", "error")
        return RedirectResponse("/admin/users", status_code=303)
    if target.oidc_subject:
        flash(request, "OIDC 用户不使用本地密码，请在身份提供商中重置", "error")
        return RedirectResponse("/admin/users", status_code=303)
    password = str(form.get("password", ""))
    confirm = str(form.get("password_confirm", ""))
    if len(password) > settings.password_max_length:
        flash(request, f"密码不能超过 {settings.password_max_length} 个字符", "error")
        return RedirectResponse("/admin/users", status_code=303)
    password_error = password_policy_error(password, settings.registration_min_password_length)
    if password_error:
        flash(request, password_error, "error")
        return RedirectResponse("/admin/users", status_code=303)
    if password != confirm:
        flash(request, "两次输入的新密码不一致", "error")
        return RedirectResponse("/admin/users", status_code=303)
    target.password_hash = hash_password(password)
    rotate_user_sessions(target)
    db.commit()
    record_audit(db, request, actor, "user.password_reset", "user", target.id)
    flash(request, f"已重置 {target.username} 的本地密码，旧会话已全部失效")
    return RedirectResponse("/admin/users", status_code=303)
